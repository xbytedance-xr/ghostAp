"""Programming mode handlers — shared template for Coco and Claude.

The ``ProgrammingModeHandler`` captures the 90 %+ duplicated logic between the
two programming backends.  ``CocoModeHandler`` and ``ClaudeModeHandler`` are thin
subclasses that supply mode-specific attributes (name, emoji, session manager, …).
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING, Optional

from ...acp import ACPEventRenderer
from ...acp.manager import ACPSessionManager
from ...agent_session import SyncSession
from ...card import CardBuilder
from ...project import ContextSourceMode
from ...utils.errors import log_exception
from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt
from ...mode import InteractionMode
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class ProgrammingModeHandler(BaseHandler):
    """Template-method base for Coco / Claude programming modes."""

    # Subclass must set these
    mode_name: str  # "Coco" / "Claude"
    mode_emoji: str  # "🤖" / "🔮"
    is_coco: bool  # True for Coco, False for Claude
    context_source: ContextSourceMode
    thinking_text: str  # "🤔 Coco 正在思考..." / "🔮 Claude 正在思考..."
    _PROGRAMMING_MODE_ENTRIES = (
        (InteractionMode.COCO, "is_coco_mode", "_coco_handler"),
        (InteractionMode.CLAUDE, "is_claude_mode", "_claude_handler"),
        (InteractionMode.AIDEN, "is_aiden_mode", "_aiden_handler"),
        (InteractionMode.CODEX, "is_codex_mode", "_codex_handler"),
        (InteractionMode.GEMINI, "is_gemini_mode", "_gemini_handler"),
        (InteractionMode.TTADK, "is_ttadk_mode", "_ttadk_handler"),
    )

    # ------------------------------------------------------------------
    # Hooks — subclass implements
    # ------------------------------------------------------------------
    @abstractmethod
    def _get_session_manager(self) -> ACPSessionManager: ...

    @abstractmethod
    def _is_in_this_mode(self, chat_id: str) -> bool: ...

    @abstractmethod
    def _is_in_opposite_mode(self, chat_id: str) -> bool: ...

    @abstractmethod
    def _exit_opposite_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"]):
        """Exit the *other* programming mode (mutual exclusion)."""
        ...

    @abstractmethod
    def _enter_mode_on_manager(self, chat_id: str, project_id: Optional[str] = None):
        """Call mode_manager.enter_xxx_mode(chat_id, project_id)."""
        ...

    @abstractmethod
    def _get_interaction_mode(self):
        """Return the ``InteractionMode`` enum member."""
        ...

    @abstractmethod
    def _get_snapshot(self, project: "ProjectContext"):
        """Return project.coco_session_snapshot or project.claude_session_snapshot."""
        ...

    @abstractmethod
    def _set_mode_on_project(self, project: "ProjectContext", active: bool, session_id: str = "", count: int = 0):
        """Call project.set_coco_mode / set_claude_mode."""
        ...

    @abstractmethod
    def _update_snapshot_on_project(self, project: "ProjectContext", query: str, count: int, session_id: str = ""):
        """Call project.update_coco_snapshot / update_claude_snapshot."""
        ...

    @abstractmethod
    def _clear_snapshot_on_project(self, project: "ProjectContext"):
        """Clear the snapshot for a new-session card action."""
        ...

    # ------------------------------------------------------------------
    # dynamic agent overrides (for TTADK, etc.)
    # ------------------------------------------------------------------
    def _get_agent_type_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return None

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return None

    def _get_ttadk_tool_display(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return None

    def _get_ttadk_model_display(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return None

    def _uses_claude_cli(self) -> bool:
        return False

    def _deactivate_other_project_modes(self, project: Optional["ProjectContext"]) -> None:
        if not project:
            return
        current = self._get_interaction_mode()
        if current != InteractionMode.COCO:
            project.set_coco_mode(False)
        if current != InteractionMode.CLAUDE:
            project.set_claude_mode(False)
        if current != InteractionMode.AIDEN:
            project.set_aiden_mode(False)
        if current != InteractionMode.CODEX:
            project.set_codex_mode(False)
        if current != InteractionMode.GEMINI:
            project.set_gemini_mode(False)
        if current != InteractionMode.TTADK:
            project.set_ttadk_mode(False)

    def _iter_other_programming_mode_entries(self):
        current = self._get_interaction_mode()
        for mode, predicate_name, handler_attr in self._PROGRAMMING_MODE_ENTRIES:
            if mode != current:
                yield mode, predicate_name, handler_attr

    def _is_any_other_programming_mode(self, chat_id: str) -> bool:
        for _mode, predicate_name, _handler_attr in self._iter_other_programming_mode_entries():
            predicate = getattr(self.mode_manager, predicate_name, None)
            if callable(predicate) and predicate(chat_id):
                return True
        return False

    def _exit_other_programming_modes(self, message_id: str, chat_id: str, project: Optional["ProjectContext"]):
        for mode, predicate_name, handler_attr in self._iter_other_programming_mode_entries():
            predicate = getattr(self.mode_manager, predicate_name, None)
            if not callable(predicate) or not predicate(chat_id):
                continue
            handler = getattr(self, handler_attr, None)
            if handler is None and mode in (InteractionMode.COCO, InteractionMode.CLAUDE):
                handler = getattr(self, "_opposite_handler", None)
            if handler and handler is not self and hasattr(handler, "exit_mode"):
                handler.exit_mode(message_id, chat_id, project=project)

    # ------------------------------------------------------------------
    # enter_mode
    # ------------------------------------------------------------------
    def enter_mode(
        self, message_id: str, chat_id: str, silent: bool = False, project: Optional["ProjectContext"] = None,
        thread_id: Optional[str] = None,
    ):
        from ...thread import get_current_thread_id

        project_id = project.project_id if project else None

        if thread_id is None:
            thread_id = get_current_thread_id()
        _thread_enabled = self.settings.thread_programming_enabled and not thread_id

        if not thread_id and self._is_in_this_mode(chat_id):
            if not silent:
                if _thread_enabled:
                    self.reply_message(
                        message_id,
                        fmt.format_warning(
                            f"已开启{self.mode_name}编程模式\n\n"
                            "直接发送你的编程需求，将自动创建编程话题\n\n"
                            "说「退出模式」或发送 /exit 退出"
                        ),
                    )
                else:
                    info = self._get_session_manager().get_session_info(chat_id, project_id=project_id)
                    self.reply_message(
                        message_id,
                        fmt.format_warning(
                            f"已经在{self.mode_name}编程模式中\n\n{info}\n\n说「退出模式」或发送 /exit 退出"
                        ),
                    )
            return

        previous_mode = self.mode_manager.get_mode(chat_id)

        if not thread_id and self._is_in_opposite_mode(chat_id):
            self._exit_opposite_mode(message_id, chat_id, project=project)

        if not project:
            working_dir = self.get_working_dir(chat_id)
            try:
                project, is_new = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    logger.info("自动创建项目: %s @ %s", project.project_name, project.root_path)
                project_id = project.project_id
            except Exception as e:
                log_exception(logger, "自动创建项目失败", e)

        working_dir = self.get_working_dir(chat_id)
        cwd = project.root_path if project else working_dir

        if project:
            valid, path_msg = self.project_manager.validate_project_path(project.project_id)
            if not valid:
                if not silent:
                    self.reply_message(message_id, f"⚠️ {path_msg}\n\n请切换到有效目录后重试")
                return

        if _thread_enabled:
            self._enter_mode_on_manager(chat_id, project_id=project_id)
            self.add_reaction(message_id, EmojiReaction.on_coco_enter())
            if project:
                self._deactivate_other_project_modes(project)
                self._set_mode_on_project(project, True)
            if not silent:
                content = (
                    f"{self.mode_emoji} 已开启{self.mode_name}编程模式\n\n"
                    "📝 发送你的编程需求，将自动创建编程话题\n\n"
                    "说「退出模式」或发送 `/exit` 退出"
                )
                if self.mode_name == "TTADK":
                    content += "\n\n可点击「切换TTADK工具」重新选择工具链"
                if project:
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project,
                        f"{self.mode_emoji} {self.mode_name}编程模式已开启",
                        content,
                        show_buttons=True,
                        footer=f"📂 项目目录: {project.root_path}",
                    )
                    self.reply_message(message_id, card_content, msg_type=msg_type)
                else:
                    self.reply_message(message_id, content)
            if project:
                self.record_mode_transition(
                    project.project_id,
                    previous_mode,
                    self._get_interaction_mode(),
                    reason=f"enter_{self.mode_name.lower()}_mode(thread_pending)",
                )
            return

        target_session_id = None
        snapshot = self._get_snapshot(project) if project else None
        if snapshot and snapshot.is_resumable and not thread_id:
            target_session_id = snapshot.session_id

        startup_timeout = getattr(self.settings, "acp_startup_timeout", 20)
        try:
            agent_type_override = self._get_agent_type_override(project)
            model_name = self._get_model_name_override(project)
            session = self._get_session_manager().ensure_session(
                chat_id,
                cwd=cwd,
                session_id=target_session_id,
                startup_timeout=startup_timeout,
                project_id=project_id,
                agent_type_override=agent_type_override,
                model_name=model_name,
                thread_id=thread_id,
            )
        except TimeoutError as e:
            if not silent:
                if self.mode_name == "TTADK":
                    project_id = project.project_id if project else None
                    msg_type, card_content = CardBuilder.build_ttadk_soft_failure_card_for(
                        "TTADK 会话启动超时",
                        project_id=project_id,
                    )
                    self.reply_message(message_id, card_content, msg_type=msg_type)
                else:
                    self.send_error_card(
                        chat_id,
                        e,
                        title=f"启动 {self.mode_name} 会话超时",
                        origin_message_id=message_id,
                    )
            return
        except Exception as e:
            if not silent:
                if self.mode_name == "TTADK":
                    project_id = project.project_id if project else None
                    msg_type, card_content = CardBuilder.build_ttadk_soft_failure_card_for(
                        "TTADK 会话暂不可用",
                        project_id=project_id,
                    )
                    self.reply_message(message_id, card_content, msg_type=msg_type)
                else:
                    self.send_error_card(
                        chat_id,
                        e,
                        title=f"启动 {self.mode_name} 会话失败",
                        origin_message_id=message_id,
                    )
            return

        try:
            if (
                agent_type_override
                and str(agent_type_override).lower().startswith("ttadk_")
                and getattr(session, "_degraded_to", "")
            ):
                degraded_to = getattr(session, "_degraded_to", "")
                reason = getattr(session, "_degraded_reason", "")
                if not silent:
                    self.reply_message(
                        message_id,
                        fmt.format_warning(
                            f"⚠️ TTADK 后端暂不可用，已自动降级到 `{degraded_to}` 继续使用。\n\n"
                            f"原因摘要：{reason or '(empty)'}"
                        ),
                    )
        except Exception:
            pass

        if not thread_id:
            self._enter_mode_on_manager(chat_id, project_id=project_id)
        self.add_reaction(message_id, EmojiReaction.on_coco_enter())

        if project and snapshot and snapshot.is_resumable:
            if not thread_id:
                self._deactivate_other_project_modes(project)
                self._set_mode_on_project(project, True, snapshot.session_id, snapshot.query_count)
            if not silent:
                mode_hint = "继续之前的对话吧！"
                if self.mode_name == "TTADK":
                    mode_hint = "当前模式：🎮 TTADK（可点「切换TTADK工具」，或发送 `/exit` 退回智能模式）"
                content = (
                    f"🔄 已恢复 {self.mode_name} 会话\n\n"
                    f"• 会话 ID: `{session.session_id}`\n"
                    f"• 历史对话: {snapshot.query_count} 条\n\n"
                    f"{mode_hint}"
                )
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    f"{self.mode_name} 会话已恢复",
                    content,
                    show_buttons=True,
                    footer=f"📂 项目目录: {project.root_path}",
                )
                response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self.register_message_project(response_id, project)
        elif project:
            if not thread_id:
                self._deactivate_other_project_modes(project)
                self._set_mode_on_project(project, True, session.session_id)
            if not silent:
                content = (
                    f"{self.mode_emoji} 已进入{self.mode_name}编程模式\n\n"
                    "现在可以用自然语言描述你的需求\n\n"
                    "说「退出模式」或发送 `/exit` 退出"
                )
                if self.mode_name == "TTADK":
                    content += "\n\n可点击「切换TTADK工具」重新选择工具链"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    f"{self.mode_emoji} {self.mode_name}编程模式",
                    content,
                    show_buttons=True,
                    footer=f"📂 项目目录: {project.root_path}",
                )
                response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self.register_message_project(response_id, project)
        else:
            if not silent:
                if self.is_coco:
                    self.reply_message(message_id, fmt.format_coco_enter())
                else:
                    self.reply_message(
                        message_id,
                        f"{self.mode_emoji} 已进入 {self.mode_name} 编程模式\n\n现在可以用自然语言描述你的需求\n\n说「退出模式」或发送 `/exit` 退出",
                    )

        if project:
            self.record_mode_transition(
                project.project_id,
                previous_mode,
                self._get_interaction_mode(),
                reason=f"enter_{self.mode_name.lower()}_mode",
            )

    # ------------------------------------------------------------------
    # Thread context registration
    # ------------------------------------------------------------------
    def _register_thread_context(
        self,
        thread_root_id: str,
        chat_id: str,
        project: Optional["ProjectContext"],
        session: SyncSession,
    ) -> None:
        try:
            from ...thread import get_thread_manager

            mode_name = self._get_interaction_mode().value
            tool_name = None
            model_name = None
            if project:
                project_id = project.project_id
            else:
                active = self.project_manager.get_active_project(chat_id)
                project_id = active.project_id if active else (session.session_id or "unknown")
                if active:
                    project = active
            if project and project.ttadk_tool_name:
                tool_name = project.ttadk_tool_name
            if project and project.ttadk_model_name:
                model_name = project.ttadk_model_name

            get_thread_manager().register(
                thread_root_id=thread_root_id,
                chat_id=chat_id,
                project_id=project_id,
                mode=mode_name,
                tool_name=tool_name,
                model_name=model_name,
            )
        except Exception as e:
            logger.warning("[Thread] Failed to register context: %s", e, exc_info=True)

    # ------------------------------------------------------------------
    # exit_mode
    # ------------------------------------------------------------------
    def exit_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        from ...thread import get_current_thread_id, get_thread_manager

        project_id = project.project_id if project else None
        thread_id = get_current_thread_id()
        session = self._get_session_manager().get_session(chat_id, project_id=project_id, thread_id=thread_id)

        is_pending_slot = (
            not thread_id
            and not session
            and self.settings.thread_programming_enabled
            and self._is_in_this_mode(chat_id)
        )

        if project:
            if session:
                self._update_snapshot_on_project(
                    project,
                    query=session.last_query,
                    count=session.message_count,
                    session_id=session.session_id,
                )
                self.context_manager.update_context(
                    project.project_id,
                    session_snapshot={
                        "data": session.to_snapshot(),
                        "source_mode": self.context_source.value,
                    },
                )
            if not thread_id:
                self._set_mode_on_project(project, False)

        if not thread_id:
            self.mode_manager.exit_to_smart(chat_id, project_id=project_id)

        try:
            has_session = self._get_session_manager().end_session(chat_id, project_id=project_id, thread_id=thread_id)
            if has_session or is_pending_slot:
                self.add_reaction(message_id, EmojiReaction.on_coco_exit())

                if project:
                    content = f"👋 已退出{self.mode_name}编程模式\n\n会话已保存，下次可以恢复\n\n当前为 🧠 智能模式"
                    if is_pending_slot:
                        content = f"👋 已退出{self.mode_name}编程模式\n\n当前为 🧠 智能模式"
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project,
                        f"已退出{self.mode_name}编程模式",
                        content,
                        show_buttons=True,
                    )
                    response_id = self.reply_message_with_id(
                        message_id, card_content, msg_type,
                        reply_in_thread=True if thread_id else None,
                    )
                    if response_id:
                        self.register_message_project(response_id, project)
                else:
                    self.reply_message(
                        message_id,
                        f"👋 已退出{self.mode_name}编程模式\n\n当前为 🧠 智能模式",
                        reply_in_thread=True if thread_id else None,
                    )
            else:
                self.reply_message(
                    message_id,
                    fmt.format_warning(f"当前不在 {self.mode_name} 模式中"),
                    reply_in_thread=True if thread_id else None,
                )
        finally:
            if thread_id is not None:
                get_thread_manager().remove(thread_id)

    # ------------------------------------------------------------------
    # handle_message
    # ------------------------------------------------------------------
    def handle_message(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        from ...thread import get_current_thread_id

        project_id = project.project_id if project else None
        thread_id = get_current_thread_id()
        session = self._get_session_manager().get_session(chat_id, project_id=project_id, thread_id=thread_id)

        if not session:
            self.enter_mode(message_id, chat_id, project=project, thread_id=thread_id)
            if not project:
                working_dir = self.get_working_dir(chat_id)
                try:
                    project, _ = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                    project_id = project.project_id
                except Exception:
                    active_project = self.project_manager.get_active_project(chat_id)
                    if active_project:
                        project = active_project
                        project_id = active_project.project_id
            session = self._get_session_manager().get_session(chat_id, project_id=project_id, thread_id=thread_id)
            if not session and thread_id:
                active_project = self.project_manager.get_active_project(chat_id)
                if active_project and active_project is not project:
                    project = active_project
                    project_id = active_project.project_id
                    session = self._get_session_manager().get_session(
                        chat_id,
                        project_id=project_id,
                        thread_id=thread_id,
                    )
            if not session:
                self.reply_message(
                    message_id,
                    fmt.format_warning(
                        f"{self.mode_name} 会话启动失败，请重新发送 /{self.mode_name.lower()} 开始"
                    ),
                    reply_in_thread=True if thread_id else None,
                )
                return

        text = self.inject_bridge_context(text, project)
        global_working_dir = self.get_working_dir(chat_id)
        cwd = project.root_path if project else global_working_dir
        self.handle_response(message_id, chat_id, text, session, project, cwd, global_working_dir)

    # ------------------------------------------------------------------
    # handle_response (streaming / non-streaming)
    # ------------------------------------------------------------------
    def handle_response(
        self, message_id: str, chat_id: str, text: str, session: SyncSession, project, cwd: str, global_working_dir: str
    ):
        from ...acp.models import ACPEvent

        streaming_manager = self.get_streaming_manager()

        project_name = project.project_name if project else None
        project_path = project.root_path if project else global_working_dir
        project_id = project.project_id if project else None
        with self.ctx.pending_image_lock:
            image_keys = self.ctx.pending_image_keys.get(message_id)

        logger.info("开始 %s 输出: project=%s, path=%s", self.mode_name, project_name, project_path)

        from ...thread import get_current_thread_id as _get_tid
        _thread_id = _get_tid()

        streaming_card = streaming_manager.create_streaming_card(
            chat_id=chat_id,
            project_name=project_name,
            project_path=project_path,
            project_id=project_id,
            initial_content=self.thinking_text,
            is_coco_mode=self.is_coco,
            is_claude_mode=not self.is_coco and self.mode_name != "TTADK",
            is_ttadk_mode=self.mode_name == "TTADK",
            reply_to_message_id=message_id,
            image_keys=image_keys,
            reply_in_thread=True if _thread_id else None,
            thread_root_id=_thread_id,
            ttadk_tool_name=self._get_ttadk_tool_display(project) if self.mode_name == "TTADK" else None,
            ttadk_model_name=self._get_ttadk_model_display(project) if self.mode_name == "TTADK" else None,
        )

        card_message_id = None
        if streaming_card:
            card_message_id = streaming_manager.send_streaming_card(streaming_card)

        if card_message_id:
            try:
                rid = self.ensure_request_id(message_id, chat_id=chat_id, project_id=project_id)
                self.ctx.message_linker.register_origin(
                    message_id, request_id=rid, chat_id=chat_id, project_id=project_id
                )
                self.ctx.message_linker.link_reply(message_id, card_message_id)
            except Exception as e:
                logger.debug(
                    "link消息失败(programming): message_id=%s, card_message_id=%s, err=%s",
                    message_id,
                    card_message_id,
                    e,
                )

        # Event-driven rendering (ACP backend emits rich events; CLI backend emits TEXT_CHUNK only)
        renderer = ACPEventRenderer()
        timeout = self.settings.coco_execution_timeout if self.is_coco else self.settings.claude_execution_timeout

        if not streaming_card or not card_message_id:
            logger.warning("创建流式卡片失败，回退到纯文本模式")
            try:
                result = session.send_prompt(text, on_event=None, timeout=timeout)
                final_response = renderer.get_final_content() or "✅ 执行完成"
                response_with_dir = f"{final_response}\n\n---\n📁 工作目录: `{global_working_dir}`"
                self.reply_message(message_id, response_with_dir)
            except Exception as e:
                msg_type, content = CardBuilder.build_error_card(e, title="执行异常", project=project)
                self.reply_message(message_id, content, msg_type)
        else:
            update_count = [0]

            def on_event(event: ACPEvent):
                update_count[0] += 1
                rendered = renderer.process_event(event)
                if rendered and streaming_card:
                    streaming_manager.update_content(streaming_card, rendered)

            try:
                result = session.send_prompt(text, on_event=on_event, timeout=timeout)
                final_response = renderer.get_final_content()
                # Fallback: renderer may return "" (e.g. only THOUGHT_CHUNKs, empty tool titles, backend crash)
                if not final_response and result and result.text:
                    final_response = result.text
                if not final_response:
                    final_response = "✅ 执行完成"
            except Exception as e:
                final_response = f"❌ 执行异常: {e}"
                log_exception(logger, f"{self.mode_name} ACP执行异常", e)
                # If exception has quick actions, send a separate error card
                from ...utils.errors import GhostAPError

                if isinstance(e, GhostAPError) and e.quick_actions:
                    self.send_error_card(chat_id, e, title="执行异常", origin_message_id=message_id)

            # Append completion summary (tool calls / modified files)
            summary = renderer.render_summary()
            if summary:
                final_response += f"\n\n---\n{summary}"

            logger.info("%s ACP输出完成: 事件数=%d, 最终长度=%d", self.mode_name, update_count[0], len(final_response))
            streaming_manager.close_streaming(streaming_card, final_content=final_response)

        # Post-processing: record context, add reaction
        if project:
            self._update_snapshot_on_project(project, text, session.message_count, session.session_id)
            project.add_conversation("user", text, message_id)
            project.add_conversation("assistant", final_response)
            source = self.mode_name.lower()
            self.context_manager.update_context(
                project.project_id,
                conversation={"role": "user", "content": text, "source_mode": source, "message_id": message_id},
            )
            self.context_manager.update_context(
                project.project_id, conversation={"role": "assistant", "content": final_response, "source_mode": source}
            )

        self.add_reaction(message_id, EmojiReaction.on_coco_response())

        if card_message_id and project:
            self.register_message_project(card_message_id, project)

    # ------------------------------------------------------------------
    # show_info
    # ------------------------------------------------------------------
    def show_info(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        from ...thread import get_current_thread_id

        project_id = project.project_id if project else None
        thread_id = get_current_thread_id()
        info = self._get_session_manager().get_session_info(chat_id, project_id=project_id, thread_id=thread_id)
        if info:
            if project:
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project,
                    f"{self.mode_name} 会话信息",
                    info,
                    show_buttons=True,
                )
                response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self.register_message_project(response_id, project)
            else:
                self.reply_message(message_id, info)
        else:
            self.reply_message(message_id, fmt.format_warning(f"当前不在 {self.mode_name} 模式中"))

    # ------------------------------------------------------------------
    # Card actions
    # ------------------------------------------------------------------
    def handle_card_enter(self, message_id: str, chat_id: str, project_id: str, value: Optional[dict] = None):
        if project_id:
            project = self.project_manager.get_project(project_id)
            if project:
                self.project_manager.set_active_project(chat_id, project_id)

                snapshot = self._get_snapshot(project)
                if snapshot and snapshot.is_resumable:
                    if self.is_coco:
                        msg_type, card_content = CardBuilder.build_coco_resume_card(project)
                    elif self.mode_name == "Claude":
                        msg_type, card_content = CardBuilder.build_claude_resume_card(project)
                    else:
                        msg_type, card_content = CardBuilder.build_ttadk_resume_card(project)
                    response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                    if response_id:
                        self.register_message_project(response_id, project)
                    return

                self.enter_mode(message_id, chat_id, project=project)
                return

        self.enter_mode(message_id, chat_id)

    def handle_card_exit(self, message_id: str, chat_id: str, project_id: str, value: Optional[dict] = None):
        from ...thread import get_current_thread_id
        if project_id:
            project = self.project_manager.get_project(project_id)
            if project and not get_current_thread_id():
                self._set_mode_on_project(project, False)
            self.exit_mode(message_id, chat_id, project=project)
            return
        self.exit_mode(message_id, chat_id)

    def handle_card_resume(self, message_id: str, chat_id: str, project_id: str, session_id: str):
        from ...thread import get_current_thread_id

        thread_id = get_current_thread_id()
        project = self.project_manager.get_project(project_id) if project_id else None
        pid = project.project_id if project else None
        if project:
            self.project_manager.set_active_project(chat_id, project_id)

        self.add_reaction(message_id, EmojiReaction.on_coco_enter())

        previous_mode = self.mode_manager.get_mode(chat_id)

        cwd = project.root_path if project else self.get_working_dir(chat_id)
        if self._uses_claude_cli():
            # Claude resume: start_session with session_id, set resumed
            try:
                agent_type_override = self._get_agent_type_override(project)
                model_name = self._get_model_name_override(project)
                session = self.ctx.claude_manager.start_session(
                    chat_id,
                    cwd=cwd,
                    session_id=session_id,
                    project_id=pid,
                    agent_type_override=agent_type_override,
                    model_name=model_name,
                    thread_id=thread_id,
                )
            except Exception as e:
                self.send_error_card(
                    chat_id,
                    e,
                    title="恢复 Claude 会话失败",
                    origin_message_id=message_id,
                )
                return
            session.is_resumed = True

            if thread_id and project:
                self._register_thread_context(thread_id, chat_id, project, session)
            if not thread_id:
                self._enter_mode_on_manager(chat_id, project_id=pid)
        else:
            try:
                agent_type_override = self._get_agent_type_override(project)
                model_name = self._get_model_name_override(project)
                session = self._get_session_manager().start_session(
                    chat_id,
                    cwd=cwd,
                    session_id=session_id,
                    project_id=pid,
                    agent_type_override=agent_type_override,
                    model_name=model_name,
                    thread_id=thread_id,
                )
            except Exception as e:
                self.send_error_card(
                    chat_id,
                    e,
                    title=f"恢复 {self.mode_name} 会话失败",
                    origin_message_id=message_id,
                )
                return
            if thread_id and project:
                self._register_thread_context(thread_id, chat_id, project, session)
            if not thread_id:
                self._enter_mode_on_manager(chat_id, project_id=pid)

        if project:
            if not thread_id:
                self._deactivate_other_project_modes(project)
                self._set_mode_on_project(project, True, session_id)
            self.record_mode_transition(
                project.project_id,
                previous_mode,
                self._get_interaction_mode(),
                reason=f"resume_{self.mode_name.lower()}_session",
            )
            content = f"🔄 已恢复 {self.mode_name} 会话\n\n会话 ID: `{session_id}`\n\n现在可以继续之前的对话了"
            msg_type, card_content = CardBuilder.build_project_response_card(
                project,
                f"{self.mode_name} 会话已恢复",
                content,
                show_buttons=True,
            )
            response_id = self.reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self.register_message_project(response_id, project)
        else:
            self.reply_message(message_id, f"🔄 已恢复 {self.mode_name} 会话: `{session_id}`")

    def handle_card_new(self, message_id: str, chat_id: str, project_id: str, value: Optional[dict] = None):
        project = self.project_manager.get_project(project_id) if project_id else None
        if project:
            self.project_manager.set_active_project(chat_id, project_id)
            self._clear_snapshot_on_project(project)
            self.enter_mode(message_id, chat_id, project=project)
            return
        self.enter_mode(message_id, chat_id)


# ======================================================================
# Concrete subclasses
# ======================================================================


class CocoModeHandler(ProgrammingModeHandler):
    mode_name = "Coco"
    mode_emoji = "🤖"
    is_coco = True
    context_source = ContextSourceMode.COCO
    thinking_text = "🤔 Coco 正在思考..."

    def __init__(self, ctx):
        super().__init__(ctx)
        self._current_model: Optional[str] = None

    def _get_session_manager(self):
        return self.ctx.coco_manager

    def _is_in_this_mode(self, chat_id):
        return self.mode_manager.is_coco_mode(chat_id)

    def _is_in_opposite_mode(self, chat_id):
        return self._is_any_other_programming_mode(chat_id)

    def _exit_opposite_mode(self, message_id, chat_id, project=None):
        self._exit_other_programming_modes(message_id, chat_id, project=project)

    def _enter_mode_on_manager(self, chat_id, project_id=None):
        self.mode_manager.enter_coco_mode(chat_id, project_id=project_id)

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        if project and getattr(project, "acp_tool_name", "") == "coco":
            return getattr(project, "acp_model_name", None)
        return self._current_model

    def _get_interaction_mode(self):
        from ...mode import InteractionMode

        return InteractionMode.COCO

    def _get_snapshot(self, project):
        return project.coco_session_snapshot

    def _set_mode_on_project(self, project, active, session_id="", count=0):
        if active:
            project.set_coco_mode(True, session_id, count)
        else:
            project.set_coco_mode(False)

    def _update_snapshot_on_project(self, project, query, count, session_id=""):
        project.update_coco_snapshot(query=query, query_count=count)

    def _clear_snapshot_on_project(self, project):
        project.coco_session_snapshot = None

    @property
    def current_model(self) -> Optional[str]:
        return self._current_model

    @current_model.setter
    def current_model(self, value: Optional[str]):
        self._current_model = value


class ClaudeModeHandler(ProgrammingModeHandler):
    mode_name = "Claude"
    mode_emoji = "🔮"
    is_coco = False
    context_source = ContextSourceMode.CLAUDE
    thinking_text = "🔮 Claude 正在思考..."

    def __init__(self, ctx):
        super().__init__(ctx)
        self._current_model: Optional[str] = None

    def _get_session_manager(self):
        return self.ctx.claude_manager

    def _is_in_this_mode(self, chat_id):
        return self.mode_manager.is_claude_mode(chat_id)

    def _is_in_opposite_mode(self, chat_id):
        return self._is_any_other_programming_mode(chat_id)

    def _exit_opposite_mode(self, message_id, chat_id, project=None):
        self._exit_other_programming_modes(message_id, chat_id, project=project)

    def _enter_mode_on_manager(self, chat_id, project_id=None):
        self.mode_manager.enter_claude_mode(chat_id, project_id=project_id)

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        if project and getattr(project, "acp_tool_name", "") == "claude":
            return getattr(project, "acp_model_name", None)
        return self._current_model

    def _get_interaction_mode(self):
        from ...mode import InteractionMode

        return InteractionMode.CLAUDE

    def _get_snapshot(self, project):
        return project.claude_session_snapshot

    def _set_mode_on_project(self, project, active, session_id="", count=0):
        if active:
            project.set_claude_mode(True, session_id, count)
        else:
            project.set_claude_mode(False)

    def _update_snapshot_on_project(self, project, query, count, session_id=""):
        project.update_claude_snapshot(query=query, query_count=count, session_id=session_id)

    def _clear_snapshot_on_project(self, project):
        project.claude_session_snapshot = None

    def _uses_claude_cli(self) -> bool:
        return True

    @property
    def current_model(self) -> Optional[str]:
        return self._current_model

    @current_model.setter
    def current_model(self, value: Optional[str]):
        self._current_model = value


class AidenModeHandler(ProgrammingModeHandler):
    mode_name = "Aiden"
    mode_emoji = "🎯"
    is_coco = False
    context_source = ContextSourceMode.AIDEN
    thinking_text = "🎯 Aiden 正在思考..."

    def __init__(self, ctx):
        super().__init__(ctx)
        self._current_model: Optional[str] = None

    def _get_session_manager(self):
        return self.ctx.aiden_manager

    def _is_in_this_mode(self, chat_id):
        return self.mode_manager.is_aiden_mode(chat_id)

    def _is_in_opposite_mode(self, chat_id):
        return self._is_any_other_programming_mode(chat_id)

    def _exit_opposite_mode(self, message_id, chat_id, project=None):
        self._exit_other_programming_modes(message_id, chat_id, project=project)

    def _enter_mode_on_manager(self, chat_id, project_id=None):
        self.mode_manager.enter_aiden_mode(chat_id, project_id=project_id)

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        if project and getattr(project, "acp_tool_name", "") == "aiden":
            return getattr(project, "acp_model_name", None)
        return self._current_model

    def _get_interaction_mode(self):
        from ...mode import InteractionMode

        return InteractionMode.AIDEN

    def _get_snapshot(self, project):
        return project.aiden_session_snapshot

    def _set_mode_on_project(self, project, active, session_id="", count=0):
        if active:
            project.set_aiden_mode(True, session_id, count)
        else:
            project.set_aiden_mode(False)

    def _update_snapshot_on_project(self, project, query, count, session_id=""):
        project.update_aiden_snapshot(query=query, query_count=count, session_id=session_id)

    def _clear_snapshot_on_project(self, project):
        project.aiden_session_snapshot = None

    @property
    def current_model(self) -> Optional[str]:
        return self._current_model

    @current_model.setter
    def current_model(self, value: Optional[str]):
        self._current_model = value


class CodexModeHandler(ProgrammingModeHandler):
    mode_name = "Codex"
    mode_emoji = "⚡"
    is_coco = False
    context_source = ContextSourceMode.CODEX
    thinking_text = "⚡ Codex 正在思考..."

    def __init__(self, ctx):
        super().__init__(ctx)
        self._current_model: Optional[str] = None

    def _get_session_manager(self):
        return self.ctx.codex_manager

    def _is_in_this_mode(self, chat_id):
        return self.mode_manager.is_codex_mode(chat_id)

    def _is_in_opposite_mode(self, chat_id):
        return self._is_any_other_programming_mode(chat_id)

    def _exit_opposite_mode(self, message_id, chat_id, project=None):
        self._exit_other_programming_modes(message_id, chat_id, project=project)

    def _enter_mode_on_manager(self, chat_id, project_id=None):
        self.mode_manager.enter_codex_mode(chat_id, project_id=project_id)

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        if project and getattr(project, "acp_tool_name", "") == "codex":
            return getattr(project, "acp_model_name", None)
        return self._current_model

    def _get_interaction_mode(self):
        from ...mode import InteractionMode

        return InteractionMode.CODEX

    def _get_snapshot(self, project):
        return project.codex_session_snapshot

    def _set_mode_on_project(self, project, active, session_id="", count=0):
        if active:
            project.set_codex_mode(True, session_id, count)
        else:
            project.set_codex_mode(False)

    def _update_snapshot_on_project(self, project, query, count, session_id=""):
        project.update_codex_snapshot(query=query, query_count=count, session_id=session_id)

    def _clear_snapshot_on_project(self, project):
        project.codex_session_snapshot = None

    @property
    def current_model(self) -> Optional[str]:
        return self._current_model

    @current_model.setter
    def current_model(self, value: Optional[str]):
        self._current_model = value


class GeminiModeHandler(ProgrammingModeHandler):
    mode_name = "Gemini"
    mode_emoji = "✨"
    is_coco = False
    context_source = ContextSourceMode.GEMINI
    thinking_text = "✨ Gemini 正在思考..."

    def __init__(self, ctx):
        super().__init__(ctx)
        self._current_model: Optional[str] = None

    def _get_session_manager(self):
        return self.ctx.gemini_manager

    def _is_in_this_mode(self, chat_id):
        return self.mode_manager.is_gemini_mode(chat_id)

    def _is_in_opposite_mode(self, chat_id):
        return self._is_any_other_programming_mode(chat_id)

    def _exit_opposite_mode(self, message_id, chat_id, project=None):
        self._exit_other_programming_modes(message_id, chat_id, project=project)

    def _enter_mode_on_manager(self, chat_id, project_id=None):
        self.mode_manager.enter_gemini_mode(chat_id, project_id=project_id)

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        if project and getattr(project, "acp_tool_name", "") == "gemini":
            return getattr(project, "acp_model_name", None)
        return self._current_model

    def _get_interaction_mode(self):
        from ...mode import InteractionMode

        return InteractionMode.GEMINI

    def _get_snapshot(self, project):
        return project.gemini_session_snapshot

    def _set_mode_on_project(self, project, active, session_id="", count=0):
        if active:
            project.set_gemini_mode(True, session_id, count)
        else:
            project.set_gemini_mode(False)

    def _update_snapshot_on_project(self, project, query, count, session_id=""):
        project.update_gemini_snapshot(query=query, query_count=count, session_id=session_id)

    def _clear_snapshot_on_project(self, project):
        project.gemini_session_snapshot = None

    @property
    def current_model(self) -> Optional[str]:
        return self._current_model

    @current_model.setter
    def current_model(self, value: Optional[str]):
        self._current_model = value


class TTADKModeHandler(ProgrammingModeHandler):
    mode_name = "TTADK"
    mode_emoji = "🎮"
    is_coco = False
    context_source = ContextSourceMode.TTADK
    thinking_text = "🎮 TTADK 正在思考..."

    def __init__(self, ctx):
        super().__init__(ctx)
        self._current_tool: Optional[str] = None
        self._current_model: Optional[str] = None

    def _get_session_manager(self):
        return self.ctx.ttadk_manager

    def _is_in_this_mode(self, chat_id):
        return self.mode_manager.is_ttadk_mode(chat_id)

    def _is_in_opposite_mode(self, chat_id):
        return self._is_any_other_programming_mode(chat_id)

    def _exit_opposite_mode(self, message_id, chat_id, project=None):
        self._exit_other_programming_modes(message_id, chat_id, project=project)

    def _enter_mode_on_manager(self, chat_id, project_id=None):
        self.mode_manager.enter_ttadk_mode(chat_id, project_id=project_id)

    def _get_interaction_mode(self):
        from ...mode import InteractionMode

        return InteractionMode.TTADK

    def _get_snapshot(self, project):
        return project.ttadk_session_snapshot

    def _set_mode_on_project(self, project, active, session_id="", count=0):
        if active:
            project.set_ttadk_mode(True, session_id, count)
        else:
            project.set_ttadk_mode(False)

    def _update_snapshot_on_project(self, project, query, count, session_id=""):
        project.update_ttadk_snapshot(query=query, query_count=count, session_id=session_id)

    def _clear_snapshot_on_project(self, project):
        project.ttadk_session_snapshot = None

    def _get_agent_type_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        tool = (project.ttadk_tool_name if project else None) or self._current_tool
        if not tool:
            from ...ttadk import get_ttadk_manager

            tool = get_ttadk_manager().get_current_tool() or "coco"
        return f"ttadk_{tool}"

    def _get_model_name_override(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        model = (project.ttadk_model_name if project else None) or self._current_model
        if not model:
            from ...ttadk import get_ttadk_manager

            model = get_ttadk_manager().get_current_model()
        return model

    def _get_ttadk_tool_display(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        agent_type = self._get_agent_type_override(project)
        return agent_type.replace("ttadk_", "", 1) if agent_type else None

    def _get_ttadk_model_display(self, project: Optional["ProjectContext"] = None) -> Optional[str]:
        return self._get_model_name_override(project)

    @property
    def current_tool(self) -> Optional[str]:
        return self._current_tool

    @current_tool.setter
    def current_tool(self, value: Optional[str]):
        self._current_tool = value

    @property
    def current_model(self) -> Optional[str]:
        return self._current_model

    @current_model.setter
    def current_model(self, value: Optional[str]):
        self._current_model = value
