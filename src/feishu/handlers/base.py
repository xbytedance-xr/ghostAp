"""Base handler providing shared messaging, reaction, and context utilities.

Every concrete handler (Coco, Claude, Deep, Project, System, Diagnostics)
inherits from ``BaseHandler`` so that it can reply to messages, add reactions,
manage streaming cards, and interact with the unified context system without
duplicating the underlying Feishu API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Callable, Optional

from ..im_client import FeishuIMClient
from ..message_formatter import FeishuMessageFormatter as fmt
from ...utils.engine_identity import resolve_engine_identity
from ...utils.errors import get_error_detail

if TYPE_CHECKING:
    from ...card.streaming import StreamingCardManager
    from ...project import ContextSourceMode, ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class BaseHandler:
    """Shared utilities available to every handler."""

    def __init__(self, ctx: "HandlerContext") -> None:
        self.ctx = ctx
        self.im_client = FeishuIMClient(ctx.api_client_factory, ctx.settings)
        # Throttling state: message_id -> (content, task)
        self._pending_patches: dict[str, str] = {}
        self._patch_tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------
    @property
    def settings(self):
        return self.ctx.settings

    @property
    def project_manager(self):
        return self.ctx.project_manager

    @property
    def mode_manager(self):
        return self.ctx.mode_manager

    @property
    def context_manager(self):
        return self.ctx.context_manager

    @property
    def scheduler(self):
        return self.ctx.scheduler

    def get_handler(self, key: str) -> Optional[Any]:
        """Get a handler from the registry."""
        return self.ctx.handlers.get(key)

    def get_manager(self, key: str) -> Optional["ACPSessionManager"]:
        """Get a manager from the registry."""
        return self.ctx.managers.get(key)

    def send_error_card(
        self,
        chat_id: str,
        exc: Exception | str,
        title: str = "操作失败",
        origin_message_id: Optional[str] = None,
        reply_in_thread: Optional[bool] = None,
    ):
        """Send a structured error card (schema 2.0) with QuickActions if available."""
        from ...card import CardBuilder

        try:
            _, card_json_str = CardBuilder.build_error_card(exc, title=title)

            if origin_message_id:
                self.reply_message(
                    origin_message_id, card_json_str, msg_type="interactive", reply_in_thread=reply_in_thread
                )
            else:
                self.send_message(chat_id, card_json_str, msg_type="interactive")
        except Exception as e:
            logger.error("发送错误卡片失败: %s", e, exc_info=True)
            # Fallback to simple text reply
            from ...utils.errors import get_error_detail
            fallback_detail = get_error_detail(exc)
            if origin_message_id:
                self.reply_message(origin_message_id, f"❌ {title}: {fallback_detail}")
            else:
                self.send_message(chat_id, f"❌ {title}: {fallback_detail}")

    def reply_error(
        self,
        message_id: str,
        exc: Exception | str,
        title: str = "操作失败",
        chat_id: Optional[str] = None,
    ):
        """Convenience wrapper for send_error_card to reply to a message."""
        self.send_error_card(chat_id=chat_id or "unknown", exc=exc, title=title, origin_message_id=message_id)

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------
    async def _execute_throttled_patch(self, message_id: str, delay: float = 0.5):
        """Async worker to execute delayed patch."""
        try:
            if delay > 0:
                await asyncio.sleep(delay)

            # Remove task ref first to allow new tasks to be scheduled if this one is slow
            # AND to avoid self-cancellation when calling patch_message below
            if self._patch_tasks.get(message_id) == asyncio.current_task():
                self._patch_tasks.pop(message_id, None)

            # Pop content to send
            content = self._pending_patches.pop(message_id, None)
            if content:
                self.patch_message(message_id, content, throttle=False)
        except asyncio.CancelledError:
            # Task cancelled, means likely superseded or immediate flush
            pass
        except Exception as e:
            logger.error("异步更新消息异常: %s", e, exc_info=True)
        finally:
            # Cleanup task reference if it's still us
            if self._patch_tasks.get(message_id) == asyncio.current_task():
                self._patch_tasks.pop(message_id, None)

    def reply_message(
        self,
        message_id: str,
        content,
        msg_type: str = "text",
        *,
        origin_message_id: Optional[str] = None,
        request_id: Optional[str] = None,
        run_id: Optional[str] = None,
        is_smart_mode: bool = False,
        reply_in_thread: Optional[bool] = None,
        max_retries: Optional[int] = None,
    ):
        """Reply to *message_id*.  Thin wrapper that auto-resolves origin & request."""
        if origin_message_id is None:
            try:
                origin_message_id = self.ctx.message_linker.resolve_origin(reply_message_id=message_id)
            except Exception as e:
                logger.debug("Failed to resolve origin for message %s: %s", message_id, get_error_detail(e))
                origin_message_id = None
        origin_message_id = origin_message_id or message_id
        request_id = request_id or self.ensure_request_id(origin_message_id)
        return self.reply_message_with_id(
            message_id,
            content,
            msg_type=msg_type,
            origin_message_id=origin_message_id,
            request_id=request_id,
            run_id=run_id,
            is_smart_mode=is_smart_mode,
            reply_in_thread=reply_in_thread,
            max_retries=max_retries,
        )

    def patch_message(
        self, message_id: str, content: str, max_retries: Optional[int] = None, throttle: bool = False
    ) -> bool:
        """Update an existing message's content (e.g. updating a card).

        Args:
            message_id: ID of the message to patch
            content: New content (usually JSON string)
            max_retries: Retry count for API failures
            throttle: If True, delay sending to merge rapid updates (default 500ms window)
        """
        if throttle:
            # 1. Update pending content
            self._pending_patches[message_id] = content

            # 2. If task exists, do nothing (it will pick up latest content)
            if message_id in self._patch_tasks:
                return True

            # 3. Schedule new task
            try:
                # We need an event loop. If called from sync context, this might fail unless we have one.
                # Assuming BaseHandler is running in a process with an event loop (e.g. main loop).
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._execute_throttled_patch(message_id))
                self._patch_tasks[message_id] = task
                return True
            except RuntimeError:
                logger.debug("No running loop for throttling, falling back to immediate patch")
                pass

        # If immediate (throttle=False), cancel any pending task to avoid race
        if message_id in self._patch_tasks:
            t = self._patch_tasks.pop(message_id)
            t.cancel()
        # Also clean pending patches since we are sending now
        self._pending_patches.pop(message_id, None)

        try:
            response = self.im_client.patch_message(message_id, content, max_retries=max_retries)

            if response and response.success():
                return True
            return False
        except Exception as e:
            logger.error("更新消息不可恢复异常: %s", e, exc_info=True)
            return False

    def reply_message_with_id(
        self,
        message_id: str,
        content,
        msg_type: str = "text",
        *,
        origin_message_id: Optional[str] = None,
        request_id: Optional[str] = None,
        run_id: Optional[str] = None,
        is_smart_mode: bool = False,
        reply_in_thread: Optional[bool] = None,
        max_retries: Optional[int] = None,
    ) -> Optional[str]:
        """Reply and return the response message_id (or None on failure)."""
        try:
            if origin_message_id is None:
                try:
                    origin_message_id = self.ctx.message_linker.resolve_origin(reply_message_id=message_id)
                except Exception as e:
                    logger.debug("Failed to resolve origin inside reply_message_with_id for %s: %s", message_id, get_error_detail(e))
                    origin_message_id = None
            origin_message_id = origin_message_id or message_id
            request_id = request_id or self.ensure_request_id(origin_message_id)
            ref_note = self.format_ref_note(origin_message_id, request_id, run_id)

            # Normalize content to (msg_type, content_str)
            if isinstance(content, tuple) and len(content) == 2:
                msg_type = content[0]
                content_str = content[1]
            elif fmt.is_post_format(content):
                msg_type = content[0]
                content_str = content[1]
            elif msg_type == "text":
                text_val = str(content)
                if ref_note:
                    text_val = f"{text_val}\n\n{ref_note}"
                content_str = json.dumps({"text": text_val})
            else:
                content_str = content

            # Best-effort inject ref into interactive/post card JSON
            content_str = self._inject_ref_note(content_str, msg_type, ref_note)

            # 根据配置和模式决定是否使用话题回复
            # 优先使用显式传入的 reply_in_thread 参数
            if reply_in_thread is None:
                if is_smart_mode:
                    reply_in_thread = self.settings.smart_reply_mode == "thread"
                else:
                    reply_in_thread = self.settings.default_reply_mode == "thread"

            response = self.im_client.reply_message(
                message_id, content_str, msg_type=msg_type, reply_in_thread=reply_in_thread, max_retries=max_retries
            )

            if response and response.success() and response.data and response.data.message_id:
                reply_id = response.data.message_id
                try:
                    self.ctx.message_linker.link_reply(origin_message_id, reply_id)
                except Exception as e:
                    logger.warning(
                        "Failed to link reply %s to origin %s: %s", reply_id, origin_message_id, e, exc_info=True
                    )
                return reply_id
            return None
        except Exception as e:
            logger.error("回复消息异常: %s", e, exc_info=True)
            return None

    def send_message(
        self,
        chat_id: str,
        content: str,
        msg_type: str = "text",
        *,
        origin_message_id: Optional[str] = None,
        request_id: Optional[str] = None,
        run_id: Optional[str] = None,
        max_retries: Optional[int] = None,
    ) -> Optional[str]:
        """Send a new message to *chat_id* (not a reply)."""
        try:
            ref_note = self.format_ref_note(origin_message_id, request_id, run_id)
            content_str = content
            if msg_type == "text" and ref_note:
                try:
                    obj = json.loads(content)
                    if isinstance(obj, dict) and "text" in obj:
                        obj["text"] = f"{obj['text']}\n\n{ref_note}"
                        content_str = json.dumps(obj, ensure_ascii=False)
                    else:
                        content_str = json.dumps({"text": f"{content}\n\n{ref_note}"}, ensure_ascii=False)
                except Exception:
                    content_str = json.dumps({"text": f"{content}\n\n{ref_note}"}, ensure_ascii=False)
            else:
                content_str = self._inject_ref_note(content_str, msg_type, ref_note)

            response = self.im_client.send_message(
                "chat_id", chat_id, content_str, msg_type=msg_type, max_retries=max_retries
            )

            if response and response.success() and response.data and response.data.message_id:
                mid = response.data.message_id
                if origin_message_id:
                    try:
                        self.ctx.message_linker.link_reply(origin_message_id, mid)
                    except Exception as e:
                        logger.warning(
                            "Failed to link new message %s to origin %s: %s", mid, origin_message_id, e, exc_info=True
                        )
                return mid
            return None
        except Exception as e:
            logger.error("发送消息异常: %s", e, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Ref-note injection (shared by reply_message_with_id and send_message)
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_interactive_card_content(content_str: str) -> str:
        """Normalize outgoing interactive card JSON to Feishu-accepted shape."""
        if not isinstance(content_str, str):
            return content_str
        try:
            card = json.loads(content_str)
        except Exception:
            return content_str

        if not isinstance(card, dict):
            return content_str
        if str(card.get("schema") or "").strip() != "2.0":
            return content_str

        root_elements = card.get("elements")
        if not isinstance(root_elements, list):
            return content_str

        body = card.get("body")
        if not isinstance(body, dict):
            body = {}
            card["body"] = body
        if not isinstance(body.get("elements"), list):
            body["elements"] = root_elements

        card.pop("elements", None)
        return json.dumps(card, ensure_ascii=False)

    @staticmethod
    def _inject_ref_note(content_str: str, msg_type: str, ref_note: str) -> str:
        """Best-effort inject ref_note into interactive/post content. Returns modified content_str."""
        if msg_type == "interactive":
            content_str = BaseHandler._normalize_interactive_card_content(content_str)

        if not ref_note:
            return content_str

        if msg_type == "interactive" and isinstance(content_str, str):
            try:
                card = json.loads(content_str)
                body = card.get("body") if isinstance(card, dict) else None
                if isinstance(body, dict) and isinstance(body.get("elements"), list):
                    body["elements"].append(
                        {
                            "tag": "markdown",
                            "text_size": "notation",
                            "content": ref_note,
                        }
                    )
                    return json.dumps(card, ensure_ascii=False)
            except Exception as e:
                logger.warning("Failed to inject ref note into interactive card: %s", e, exc_info=True)
        elif msg_type == "post" and isinstance(content_str, str):
            try:
                post = json.loads(content_str)
                if isinstance(post, dict):
                    lang = post.get("zh_cn")
                    if isinstance(lang, dict) and isinstance(lang.get("content"), list):
                        blocks = lang.get("content")
                        injected = False
                        for row in reversed(blocks):
                            if not isinstance(row, list):
                                continue
                            for node in reversed(row):
                                if (
                                    isinstance(node, dict)
                                    and node.get("tag") == "md"
                                    and isinstance(node.get("text"), str)
                                ):
                                    node["text"] = f"{node['text']}\n\n---\n{ref_note}"
                                    injected = True
                                    break
                            if injected:
                                break
                            if not injected:
                                blocks.append([{"tag": "md", "text": f"---\n{ref_note}"}])
                            return json.dumps(post, ensure_ascii=False)
            except Exception as e:
                logger.warning("Failed to inject ref note into post content: %s", e, exc_info=True)

        return content_str

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------
    def add_reaction(self, message_id: str, emoji_type: str):
        try:
            self.im_client.add_reaction(message_id, emoji_type)
        except Exception as e:
            logger.warning("添加表情异常: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # Card Action Dispatcher (Template Method)
    # ------------------------------------------------------------------
    def _dispatch_standard_card_action(
        self,
        open_message_id: str,
        open_chat_id: str,
        action_type: str,
        value: dict,
        prefix: str,
        action_map: dict[str, Callable],
        toggle_log_method: Optional[Callable] = None,
        toggle_ac_method: Optional[Callable] = None,
        switch_mode_method: Optional[Callable] = None,
        project: Optional["ProjectContext"] = None,
    ) -> bool:
        """
        Dispatch standard card actions (pause, resume, stop, expand, collapse, mode_full, mode_compact).
        Returns True if action was handled, False otherwise.
        """
        # 1. Lifecycle actions (pause, resume, stop)
        if action_type in action_map:
            action_map[action_type](open_message_id, open_chat_id, project=project)
            return True

        # Common extraction for UI state actions
        # Note: 'deep_project_id' is the convention used in card buttons for both Deep and Loop engines
        engine_project_id = value.get("deep_project_id", "")

        # 2. Log expansion
        if action_type in (f"{prefix}_expand", f"{prefix}_collapse"):
            if toggle_log_method:
                expanded = action_type == f"{prefix}_expand"
                # Call with positional args to support varying param names (deep_project_id vs loop_project_id)
                # Signature expected: (message_id, chat_id, project, engine_project_id, expanded)
                toggle_log_method(open_message_id, open_chat_id, project, engine_project_id, expanded)
                return True

        # 3. View mode
        if action_type in (f"{prefix}_mode_full", f"{prefix}_mode_compact"):
            if switch_mode_method:
                compact = action_type == f"{prefix}_mode_compact"
                # Signature expected: (message_id, chat_id, project, engine_project_id, compact)
                switch_mode_method(open_message_id, open_chat_id, project, engine_project_id, compact)
                return True

        # 4. Acceptance-criteria expansion (AC)
        if action_type in (f"{prefix}_expand_ac", f"{prefix}_collapse_ac"):
            if toggle_ac_method:
                expand_ac = action_type == f"{prefix}_expand_ac"
                # Signature expected: (message_id, chat_id, project, engine_project_id, expand_ac)
                toggle_ac_method(open_message_id, open_chat_id, project, engine_project_id, expand_ac)
                return True

        return False

    # ------------------------------------------------------------------
    # Project ↔ message registration
    # ------------------------------------------------------------------
    def register_message_project(self, message_id: str, project: "ProjectContext"):
        self.ctx.message_mapper.register(message_id, project.project_id)

    # ------------------------------------------------------------------
    # Working directory
    # ------------------------------------------------------------------
    def get_working_dir(self, chat_id: str) -> str:
        with self.ctx.working_dir_lock:
            return self.ctx.working_dirs.get(chat_id, os.getcwd())

    def set_working_dir(self, chat_id: str, path: str) -> tuple[bool, str]:
        expanded_path = os.path.expanduser(path)
        if not os.path.isabs(expanded_path):
            current_dir = self.get_working_dir(chat_id)
            expanded_path = os.path.normpath(os.path.join(current_dir, expanded_path))
        if os.path.isdir(expanded_path):
            with self.ctx.working_dir_lock:
                self.ctx.working_dirs[chat_id] = expanded_path
            return True, expanded_path
        else:
            return False, f"目录不存在: {expanded_path}"

    # ------------------------------------------------------------------
    # Streaming manager (lazy)
    # ------------------------------------------------------------------
    def get_streaming_manager(self) -> "StreamingCardManager":
        return self.ctx.streaming_manager_factory()

    # ------------------------------------------------------------------
    # Request-id / ref-note helpers
    # ------------------------------------------------------------------
    def ensure_request_id(
        self, message_id: Optional[str], chat_id: Optional[str] = None, project_id: Optional[str] = None
    ) -> Optional[str]:
        if not message_id:
            return None
        rid = None
        try:
            rid = self.ctx.message_linker.get_request_id(message_id)
        except Exception as e:
            logger.debug("Failed to get existing request_id for message %s: %s", message_id, get_error_detail(e))
            rid = None
        if rid:
            return rid
        rid = uuid.uuid4().hex[:10]
        try:
            self.ctx.message_linker.register_origin(message_id, request_id=rid, chat_id=chat_id, project_id=project_id)
        except Exception as e:
            logger.warning("Failed to register origin for message %s: %s", message_id, e, exc_info=True)
        return rid

    def format_ref_note(
        self, origin_message_id: Optional[str], request_id: Optional[str], run_id: Optional[str] = None
    ) -> str:
        if not self.settings.ref_note_enabled:
            return ""
        origin_message_id = origin_message_id or ""
        request_id = request_id or ""
        run_id = run_id or ""
        parts = []
        if origin_message_id:
            parts.append(f"origin={origin_message_id}")
        if request_id:
            parts.append(f"req={request_id}")
        if run_id:
            parts.append(f"run={run_id}")
        if not parts:
            return ""
        return "🔗 关联：" + " • ".join(parts)

    # ------------------------------------------------------------------
    # Unified context utilities
    # ------------------------------------------------------------------
    @staticmethod
    def mode_to_context_source(mode) -> "ContextSourceMode":
        """Map InteractionMode → ContextSourceMode."""
        from ...mode import InteractionMode
        from ...project import ContextSourceMode

        mapping = {
            InteractionMode.SMART: ContextSourceMode.SMART,
            InteractionMode.COCO: ContextSourceMode.COCO,
            InteractionMode.CLAUDE: ContextSourceMode.CLAUDE,
            InteractionMode.AIDEN: ContextSourceMode.AIDEN,
            InteractionMode.CODEX: ContextSourceMode.CODEX,
            InteractionMode.GEMINI: ContextSourceMode.GEMINI,
            InteractionMode.TTADK: ContextSourceMode.TTADK,
        }
        return mapping.get(mode, ContextSourceMode.SMART)

    def record_mode_transition(self, project_id: str, from_mode, to_mode, reason: str = ""):
        """Record a mode switch into the unified context and build a bridge summary."""
        from_source = self.mode_to_context_source(from_mode)
        to_source = self.mode_to_context_source(to_mode)
        logger.info(
            "[模式切换] project=%s: %s -> %s, reason=%s", project_id, from_source.value, to_source.value, reason
        )
        self.ctx.context_manager.update_context(
            project_id,
            mode_transition={
                "from_mode": from_source.value,
                "to_mode": to_source.value,
                "reason": reason,
            },
        )
        ctx = self.ctx.context_manager.store.get(project_id)
        if ctx:
            ctx.build_bridge_summary(from_source, to_source)
            ctx.create_version(
                reason=f"mode_transition: {from_source.value} -> {to_source.value}",
                source_mode=from_source,
            )

    def inject_bridge_context(self, text: str, project: Optional["ProjectContext"]) -> str:
        """Consume and prepend bridge summary to the user prompt (one-shot)."""
        if not project:
            return text
        ctx = self.ctx.context_manager.store.get(project.project_id)
        if not ctx:
            return text
        bridge = ctx.consume_bridge_summary()
        if not bridge:
            return text
        injection = bridge.to_injection_prompt()
        if not injection:
            return text
        logger.info(
            "[Bridge注入] project=%s: %s -> %s, 注入 %d 字符到 prompt",
            project.project_name,
            bridge.from_mode.value,
            bridge.to_mode.value,
            len(injection),
        )
        return f"{injection}\n\n{text}"

    # ------------------------------------------------------------------
    # Shared engine callback factories
    # ------------------------------------------------------------------
    def create_rate_limit_callback(
        self,
        chat_id: str,
        message_id: str,
        project: Optional["ProjectContext"],
        engine_name: str,
        request_id: Optional[str] = None,
    ):
        """Create a reusable _on_rate_limit callback for any engine handler."""
        from ...card import CardBuilder

        def _on_rate_limit(wait_seconds: int):
            try:
                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    title="⏸️ 限速等待",
                    content=f"🔄 API 限速触发，自动等待 {wait_seconds} 秒后恢复...\n\n无需操作，任务将自动继续。",
                    engine_name=engine_name,
                    show_buttons=False,
                )
                self.send_message(chat_id, card_content, msg_type, origin_message_id=message_id, request_id=request_id)
            except Exception as e:
                logger.error("Failed to send rate limit notification: %s", e, exc_info=True)

        return _on_rate_limit

    # ------------------------------------------------------------------
    # Engine name helper
    # ------------------------------------------------------------------
    def get_engine_name(self, chat_id: str, project_id: str | None = None) -> str:
        """Return engine display name based on unified identity mapping."""
        current_mode = self.ctx.mode_manager.get_mode(chat_id, project_id=project_id)
        project = None
        if project_id:
            try:
                project = self.project_manager.get_project(project_id)
            except Exception:
                project = None
        identity = resolve_engine_identity(
            mode=current_mode,
            ttadk_tool_name=getattr(project, "ttadk_tool_name", None) if project else None,
            ttadk_model_name=getattr(project, "ttadk_model_name", None) if project else None,
            acp_tool_name=getattr(project, "acp_tool_name", None) if project else None,
            acp_model_name=getattr(project, "acp_model_name", None) if project else None,
        )
        return identity.engine_name
