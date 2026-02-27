"""Base handler providing shared messaging, reaction, and context utilities.

Every concrete handler (Coco, Claude, Deep, Project, System, Diagnostics)
inherits from ``BaseHandler`` so that it can reply to messages, add reactions,
manage streaming cards, and interact with the unified context system without
duplicating the underlying Feishu API calls.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Optional

from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    Emoji,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt

if TYPE_CHECKING:
    from ..handler_context import HandlerContext
    from ...card.streaming import StreamingCardManager
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class BaseHandler:
    """Shared utilities available to every handler."""

    def __init__(self, ctx: "HandlerContext") -> None:
        self.ctx = ctx

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

    # ------------------------------------------------------------------
    # Message sending
    # ------------------------------------------------------------------
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
    ):
        """Reply to *message_id*.  Thin wrapper that auto-resolves origin & request."""
        if origin_message_id is None:
            try:
                origin_message_id = self.ctx.message_linker.resolve_origin(reply_message_id=message_id)
            except Exception:
                origin_message_id = None
        origin_message_id = origin_message_id or message_id
        request_id = request_id or self.ensure_request_id(origin_message_id)
        return self.reply_message_with_id(
            message_id, content, msg_type=msg_type,
            origin_message_id=origin_message_id,
            request_id=request_id, run_id=run_id,
            is_smart_mode=is_smart_mode,
            reply_in_thread=reply_in_thread,
        )

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
    ) -> Optional[str]:
        """Reply and return the response message_id (or None on failure)."""
        try:
            client = self.ctx.api_client_factory()

            if origin_message_id is None:
                try:
                    origin_message_id = self.ctx.message_linker.resolve_origin(reply_message_id=message_id)
                except Exception:
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
            request = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(ReplyMessageRequestBody.builder()
                    .content(content_str)
                    .msg_type(msg_type)
                    .reply_in_thread(reply_in_thread)
                    .build()) \
                .build()

            for attempt in range(3):
                response = client.im.v1.message.reply(request)
                if response.success() and response.data and response.data.message_id:
                    reply_id = response.data.message_id
                    try:
                        self.ctx.message_linker.link_reply(origin_message_id, reply_id)
                    except Exception:
                        pass
                    return reply_id
                logger.warning("回复消息失败(尝试%d/3): %s - %s", attempt + 1, response.code, response.msg)
                time.sleep(0.3 * (2 ** attempt))
            return None
        except Exception as e:
            logger.error("回复消息异常: %s", e)
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
    ) -> Optional[str]:
        """Send a new message to *chat_id* (not a reply)."""
        try:
            client = self.ctx.api_client_factory()

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

            request = CreateMessageRequest.builder() \
                .receive_id_type("chat_id") \
                .request_body(CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .content(content_str)
                    .msg_type(msg_type)
                    .build()) \
                .build()

            for attempt in range(3):
                response = client.im.v1.message.create(request)
                if response.success() and response.data and response.data.message_id:
                    mid = response.data.message_id
                    if origin_message_id:
                        try:
                            self.ctx.message_linker.link_reply(origin_message_id, mid)
                        except Exception:
                            pass
                    return mid
                logger.warning("发送消息失败(尝试%d/3): %s - %s", attempt + 1, response.code, response.msg)
                time.sleep(0.3 * (2 ** attempt))
            return None
        except Exception as e:
            logger.error("发送消息异常: %s", e)
            return None

    # ------------------------------------------------------------------
    # Ref-note injection (shared by reply_message_with_id and send_message)
    # ------------------------------------------------------------------
    @staticmethod
    def _inject_ref_note(content_str: str, msg_type: str, ref_note: str) -> str:
        """Best-effort inject ref_note into interactive/post content. Returns modified content_str."""
        if not ref_note:
            return content_str

        if msg_type == "interactive" and isinstance(content_str, str):
            try:
                card = json.loads(content_str)
                body = card.get("body") if isinstance(card, dict) else None
                if isinstance(body, dict) and isinstance(body.get("elements"), list):
                    body["elements"].append({
                        "tag": "markdown",
                        "text_size": "notation",
                        "content": ref_note,
                    })
                    return json.dumps(card, ensure_ascii=False)
            except Exception:
                pass
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
                                if isinstance(node, dict) and node.get("tag") == "md" and isinstance(node.get("text"), str):
                                    node["text"] = f"{node['text']}\n\n---\n{ref_note}"
                                    injected = True
                                    break
                            if injected:
                                break
                        if not injected:
                            blocks.append([{"tag": "md", "text": f"---\n{ref_note}"}])
                        return json.dumps(post, ensure_ascii=False)
            except Exception:
                pass

        return content_str

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------
    def add_reaction(self, message_id: str, emoji_type: str):
        try:
            client = self.ctx.api_client_factory()
            request = CreateMessageReactionRequest.builder() \
                .message_id(message_id) \
                .request_body(CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder()
                        .emoji_type(emoji_type)
                        .build())
                    .build()) \
                .build()

            response = client.im.v1.message_reaction.create(request)
            if not response.success():
                logger.warning("添加表情失败: %s - %s", response.code, response.msg)
        except Exception as e:
            logger.warning("添加表情异常: %s", e)

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
    def ensure_request_id(self, message_id: Optional[str], chat_id: Optional[str] = None, project_id: Optional[str] = None) -> Optional[str]:
        if not message_id:
            return None
        rid = None
        try:
            rid = self.ctx.message_linker.get_request_id(message_id)
        except Exception:
            rid = None
        if rid:
            return rid
        rid = uuid.uuid4().hex[:10]
        try:
            self.ctx.message_linker.register_origin(message_id, request_id=rid, chat_id=chat_id, project_id=project_id)
        except Exception:
            pass
        return rid

    def format_ref_note(self, origin_message_id: Optional[str], request_id: Optional[str], run_id: Optional[str] = None) -> str:
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
        }
        return mapping.get(mode, ContextSourceMode.SMART)

    def record_mode_transition(self, project_id: str, from_mode, to_mode, reason: str = ""):
        """Record a mode switch into the unified context and build a bridge summary."""
        from ...project import ContextSourceMode
        from_source = self.mode_to_context_source(from_mode)
        to_source = self.mode_to_context_source(to_mode)
        logger.info("[模式切换] project=%s: %s -> %s, reason=%s",
                     project_id, from_source.value, to_source.value, reason)
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
        logger.info("[Bridge注入] project=%s: %s -> %s, 注入 %d 字符到 prompt",
                     project.project_name, bridge.from_mode.value, bridge.to_mode.value, len(injection))
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
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project,
                    title="⏸️ 限速等待",
                    content=f"🔄 API 限速触发，自动等待 {wait_seconds} 秒后恢复...\n\n无需操作，任务将自动继续。",
                    engine_name=engine_name,
                    show_buttons=False,
                )
                self.send_message(chat_id, card_content, msg_type, origin_message_id=message_id, request_id=request_id)
            except Exception:
                pass

        return _on_rate_limit

    # ------------------------------------------------------------------
    # Engine name helper
    # ------------------------------------------------------------------
    def get_engine_name(self, chat_id: str, project_id: str | None = None) -> str:
        """Return 'Coco' or 'Claude' based on current interaction mode."""
        from ...mode import InteractionMode
        current_mode = self.ctx.mode_manager.get_mode(chat_id, project_id=project_id)
        if current_mode == InteractionMode.CLAUDE:
            return "Claude"
        return "Coco"
