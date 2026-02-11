"""Programming mode handlers — shared template for Coco and Claude.

The ``ProgrammingModeHandler`` captures the 90 %+ duplicated logic between the
two programming backends.  ``CocoModeHandler`` and ``ClaudeModeHandler`` are thin
subclasses that supply mode-specific attributes (name, emoji, session manager, …).
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING, Optional

from ...acp import ACPEventRenderer, ACPSessionManager, SyncACPSession
from ...card import CardBuilder
from ...project import ContextSourceMode
from ...utils.errors import fmt_error
from ..emoji import EmojiReaction
from ..message_formatter import FeishuMessageFormatter as fmt
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class ProgrammingModeHandler(BaseHandler):
    """Template-method base for Coco / Claude programming modes."""

    # Subclass must set these
    mode_name: str          # "Coco" / "Claude"
    mode_emoji: str         # "🤖" / "🔮"
    is_coco: bool           # True for Coco, False for Claude
    context_source: ContextSourceMode
    thinking_text: str      # "🤔 Coco 正在思考..." / "🔮 Claude 正在思考..."

    # ------------------------------------------------------------------
    # Hooks — subclass implements
    # ------------------------------------------------------------------
    @abstractmethod
    def _get_session_manager(self) -> ACPSessionManager:
        ...

    @abstractmethod
    def _is_in_this_mode(self, chat_id: str) -> bool:
        ...

    @abstractmethod
    def _is_in_opposite_mode(self, chat_id: str) -> bool:
        ...

    @abstractmethod
    def _exit_opposite_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"]):
        """Exit the *other* programming mode (mutual exclusion)."""
        ...

    @abstractmethod
    def _enter_mode_on_manager(self, chat_id: str):
        """Call mode_manager.enter_xxx_mode(chat_id)."""
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
    # enter_mode
    # ------------------------------------------------------------------
    def enter_mode(self, message_id: str, chat_id: str, silent: bool = False, project: Optional["ProjectContext"] = None):
        from ...mode import InteractionMode

        if self._is_in_this_mode(chat_id):
            if not silent:
                info = self._get_session_manager().get_session_info(chat_id)
                self.reply_message(
                    message_id,
                    fmt.format_warning(f"已经在{self.mode_name}编程模式中\n\n{info}\n\n说「退出模式」或发送 /exit 退出"),
                )
            return

        previous_mode = self.mode_manager.get_mode(chat_id)

        # Mutual exclusion
        if self._is_in_opposite_mode(chat_id):
            self._exit_opposite_mode(message_id, chat_id, project=project)

        if not project:
            working_dir = self.get_working_dir(chat_id)
            try:
                project, is_new = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    logger.info("自动创建项目: %s @ %s", project.project_name, project.root_path)
            except Exception as e:
                logger.error("自动创建项目失败: %s", e)

        working_dir = self.get_working_dir(chat_id)
        cwd = project.root_path if project else working_dir

        if project:
            valid, path_msg = self.project_manager.validate_project_path(project.project_id)
            if not valid:
                if not silent:
                    self.reply_message(message_id, f"⚠️ {path_msg}\n\n请切换到有效目录后重试")
                return

        # Determine whether we should resume an existing ACP session
        target_session_id = None
        snapshot = self._get_snapshot(project) if project else None
        if snapshot and snapshot.is_resumable:
            target_session_id = snapshot.session_id

        # Ensure ACP server is running before switching mode
        startup_timeout = getattr(self.settings, "acp_startup_timeout", 20)
        try:
            session = self._get_session_manager().ensure_session(
                chat_id,
                cwd=cwd,
                session_id=target_session_id,
                startup_timeout=startup_timeout,
            )
        except TimeoutError as e:
            if not silent:
                self.reply_message(message_id, fmt_error(
                    f"启动 {self.mode_name} ACP Server 超时({startup_timeout}s)",
                    str(e),
                ))
            return
        except Exception as e:
            if not silent:
                self.reply_message(message_id, fmt_error(f"启动 {self.mode_name} ACP Server 失败", str(e)))
            return

        # Now switch mode (after ACP server is confirmed ready)
        self._enter_mode_on_manager(chat_id)
        self.add_reaction(message_id, EmojiReaction.on_coco_enter())

        if project and snapshot and snapshot.is_resumable:
            self._set_mode_on_project(project, True, snapshot.session_id, snapshot.query_count)
            if not silent:
                content = (
                    f"🔄 已恢复 {self.mode_name} 会话\n\n"
                    f"• 会话 ID: `{session.session_id}`\n"
                    f"• 历史对话: {snapshot.query_count} 条\n\n"
                    f"继续之前的对话吧！"
                )
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, f"{self.mode_name} 会话已恢复", content, show_buttons=True,
                    footer=f"📂 项目目录: {project.root_path}",
                )
                response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self.register_message_project(response_id, project)
        elif project:
            self._set_mode_on_project(project, True, session.session_id)
            if not silent:
                content = f"{self.mode_emoji} 已进入{self.mode_name}编程模式\n\n现在可以用自然语言描述你的需求\n\n说「退出模式」或发送 `/exit` 退出"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, f"{self.mode_emoji} {self.mode_name}编程模式", content, show_buttons=True,
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
                    self.reply_message(message_id, f"{self.mode_emoji} 已进入 {self.mode_name} 编程模式\n\n现在可以用自然语言描述你的需求\n\n说「退出模式」或发送 `/exit` 退出")

        # Unified context: record mode transition
        if project:
            self.record_mode_transition(
                project.project_id, previous_mode, self._get_interaction_mode(),
                reason=f"enter_{self.mode_name.lower()}_mode",
            )

    # ------------------------------------------------------------------
    # exit_mode
    # ------------------------------------------------------------------
    def exit_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        session = self._get_session_manager().get_session(chat_id)

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
            self._set_mode_on_project(project, False)

        self.mode_manager.exit_to_smart(chat_id)

        if self._get_session_manager().end_session(chat_id):
            self.add_reaction(message_id, EmojiReaction.on_coco_exit())

            if project:
                content = f"👋 已退出{self.mode_name}编程模式\n\n会话已保存，下次可以恢复\n\n当前为 🧠 智能模式"
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, f"已退出{self.mode_name}编程模式", content, show_buttons=True,
                )
                response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                if response_id:
                    self.register_message_project(response_id, project)
            else:
                self.reply_message(message_id, f"👋 已退出{self.mode_name}编程模式\n\n当前为 🧠 智能模式")
        else:
            self.reply_message(message_id, fmt.format_warning(f"当前不在 {self.mode_name} 模式中"))

    # ------------------------------------------------------------------
    # handle_message
    # ------------------------------------------------------------------
    def handle_message(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        session = self._get_session_manager().get_session(chat_id)

        if self.is_coco and project and project.coco_session_snapshot:
            project_session_id = project.coco_session_snapshot.session_id
            if not session or session.session_id != project_session_id:
                cwd = project.root_path if project else self.get_working_dir(chat_id)
                session = self._get_session_manager().resume_session(chat_id, project_session_id, cwd=cwd)
                logger.info("切换到项目 %s 的 %s 会话: %s", project.project_name, self.mode_name, project_session_id)

        if not session:
            if project:
                self.enter_mode(message_id, chat_id, project=project)
                session = self._get_session_manager().get_session(chat_id)
                if not session:
                    return
            else:
                self.reply_message(message_id, fmt.format_warning(f"{self.mode_name} 会话已过期，请发送 /{self.mode_name.lower()} 重新开始"))
                return

        text = self.inject_bridge_context(text, project)
        global_working_dir = self.get_working_dir(chat_id)
        cwd = project.root_path if project else global_working_dir
        self.handle_response(message_id, chat_id, text, session, project, cwd, global_working_dir)

    # ------------------------------------------------------------------
    # handle_response (streaming / non-streaming)
    # ------------------------------------------------------------------
    def handle_response(self, message_id: str, chat_id: str, text: str, session: SyncACPSession, project, cwd: str, global_working_dir: str):
        from ...acp.models import ACPEvent
        streaming_manager = self.get_streaming_manager()

        project_name = project.project_name if project else None
        project_path = project.root_path if project else global_working_dir
        project_id = project.project_id if project else None
        with self.ctx.pending_image_lock:
            image_keys = self.ctx.pending_image_keys.get(message_id)

        logger.info("开始 %s ACP输出: project=%s, path=%s", self.mode_name, project_name, project_path)

        streaming_card = streaming_manager.create_streaming_card(
            chat_id=chat_id,
            project_name=project_name,
            project_path=project_path,
            project_id=project_id,
            initial_content=self.thinking_text,
            is_coco_mode=self.is_coco,
            is_claude_mode=not self.is_coco,
            reply_to_message_id=message_id,
            image_keys=image_keys,
        )

        card_message_id = None
        if streaming_card:
            card_message_id = streaming_manager.send_streaming_card(streaming_card)

        if card_message_id:
            try:
                rid = self.ensure_request_id(message_id, chat_id=chat_id, project_id=project_id)
                self.ctx.message_linker.register_origin(message_id, request_id=rid, chat_id=chat_id, project_id=project_id)
                self.ctx.message_linker.link_reply(message_id, card_message_id)
            except Exception as e:
                logger.debug("link消息失败(programming): message_id=%s, card_message_id=%s, err=%s", message_id, card_message_id, e)

        # ACP event-driven rendering
        renderer = ACPEventRenderer()
        timeout = self.settings.coco_execution_timeout if self.is_coco else self.settings.claude_execution_timeout

        if not streaming_card or not card_message_id:
            logger.warning("创建流式卡片失败，回退到纯文本ACP模式")
            try:
                result = session.send_prompt(text, on_event=None, timeout=timeout)
                final_response = renderer.get_final_content() or "✅ 执行完成"
            except Exception as e:
                final_response = f"❌ 执行异常: {e}"
            response_with_dir = f"{final_response}\n\n---\n📁 工作目录: `{global_working_dir}`"
            self.reply_message(message_id, response_with_dir)
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
            except Exception as e:
                final_response = f"❌ 执行异常: {e}"
                logger.error("%s ACP执行异常: %s", self.mode_name, e)

            logger.info("%s ACP输出完成: 事件数=%d, 最终长度=%d", self.mode_name, update_count[0], len(final_response))
            streaming_manager.close_streaming(streaming_card, final_content=final_response)

        # Post-processing: record context, add reaction
        if project:
            self._update_snapshot_on_project(project, text, session.message_count, session.session_id)
            project.add_conversation("user", text, message_id)
            project.add_conversation("assistant", final_response)
            source = self.mode_name.lower()
            self.context_manager.update_context(project.project_id, conversation={"role": "user", "content": text, "source_mode": source, "message_id": message_id})
            self.context_manager.update_context(project.project_id, conversation={"role": "assistant", "content": final_response, "source_mode": source})

        self.add_reaction(message_id, EmojiReaction.on_coco_response())

        if card_message_id and project:
            self.register_message_project(card_message_id, project)

    # ------------------------------------------------------------------
    # show_info
    # ------------------------------------------------------------------
    def show_info(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        info = self._get_session_manager().get_session_info(chat_id)
        if info:
            if project:
                msg_type, card_content = CardBuilder.build_project_response_card(
                    project, f"{self.mode_name} 会话信息", info, show_buttons=True,
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
    def handle_card_enter(self, message_id: str, chat_id: str, project_id: str):
        if project_id:
            project = self.project_manager.get_project(project_id)
            if project:
                self.project_manager.set_active_project(chat_id, project_id)

                snapshot = self._get_snapshot(project)
                if snapshot and snapshot.is_resumable:
                    if self.is_coco:
                        msg_type, card_content = CardBuilder.build_coco_resume_card(project)
                    else:
                        msg_type, card_content = CardBuilder.build_claude_resume_card(project)
                    response_id = self.reply_message_with_id(message_id, card_content, msg_type)
                    if response_id:
                        self.register_message_project(response_id, project)
                    return

                self.enter_mode(message_id, chat_id, project=project)
                return

        self.enter_mode(message_id, chat_id)

    def handle_card_exit(self, message_id: str, chat_id: str, project_id: str):
        if project_id:
            project = self.project_manager.get_project(project_id)
            if project:
                self._set_mode_on_project(project, False)
            self.exit_mode(message_id, chat_id, project=project)
            return
        self.exit_mode(message_id, chat_id)

    def handle_card_resume(self, message_id: str, chat_id: str, project_id: str, session_id: str):
        from ...mode import InteractionMode

        project = self.project_manager.get_project(project_id) if project_id else None
        if project:
            self.project_manager.set_active_project(chat_id, project_id)

        self.add_reaction(message_id, EmojiReaction.on_coco_enter())

        previous_mode = self.mode_manager.get_mode(chat_id)

        cwd = project.root_path if project else self.get_working_dir(chat_id)
        if not self.is_coco:
            # Claude resume: start_session with session_id, set resumed
            try:
                session = self.ctx.claude_manager.start_session(chat_id, cwd=cwd, session_id=session_id)
            except Exception as e:
                self.reply_message(message_id, fmt_error("恢复 Claude ACP 会话", str(e)))
                return
            session.is_resumed = True
            # Mutual exclusion
            if project and project.coco_mode:
                project.set_coco_mode(False)
            self._enter_mode_on_manager(chat_id)
        else:
            self._enter_mode_on_manager(chat_id)
            try:
                session = self._get_session_manager().start_session(chat_id, cwd=cwd, session_id=session_id)
            except Exception as e:
                self.reply_message(message_id, fmt_error("恢复 Coco ACP 会话", str(e)))
                return

        if project:
            self._set_mode_on_project(project, True, session_id)
            self.record_mode_transition(
                project.project_id, previous_mode, self._get_interaction_mode(),
                reason=f"resume_{self.mode_name.lower()}_session",
            )
            content = f"🔄 已恢复 {self.mode_name} 会话\n\n会话 ID: `{session_id}`\n\n现在可以继续之前的对话了"
            msg_type, card_content = CardBuilder.build_project_response_card(
                project, f"{self.mode_name} 会话已恢复", content, show_buttons=True,
            )
            response_id = self.reply_message_with_id(message_id, card_content, msg_type)
            if response_id:
                self.register_message_project(response_id, project)
        else:
            self.reply_message(message_id, f"🔄 已恢复 {self.mode_name} 会话: `{session_id}`")

    def handle_card_new(self, message_id: str, chat_id: str, project_id: str):
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

    def _get_session_manager(self):
        return self.ctx.coco_manager

    def _is_in_this_mode(self, chat_id):
        return self.mode_manager.is_coco_mode(chat_id)

    def _is_in_opposite_mode(self, chat_id):
        return self.mode_manager.is_claude_mode(chat_id)

    def _exit_opposite_mode(self, message_id, chat_id, project=None):
        # We need the ClaudeModeHandler — but to avoid circular deps, delegate via ws_client
        # The ws_client wires this up after handler creation.
        if hasattr(self, '_opposite_handler'):
            self._opposite_handler.exit_mode(message_id, chat_id, project=project)

    def _enter_mode_on_manager(self, chat_id):
        self.mode_manager.enter_coco_mode(chat_id)

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


class ClaudeModeHandler(ProgrammingModeHandler):
    mode_name = "Claude"
    mode_emoji = "🔮"
    is_coco = False
    context_source = ContextSourceMode.CLAUDE
    thinking_text = "🔮 Claude 正在思考..."

    def _get_session_manager(self):
        return self.ctx.claude_manager

    def _is_in_this_mode(self, chat_id):
        return self.mode_manager.is_claude_mode(chat_id)

    def _is_in_opposite_mode(self, chat_id):
        return self.mode_manager.is_coco_mode(chat_id)

    def _exit_opposite_mode(self, message_id, chat_id, project=None):
        if hasattr(self, '_opposite_handler'):
            self._opposite_handler.exit_mode(message_id, chat_id, project=project)

    def _enter_mode_on_manager(self, chat_id):
        self.mode_manager.enter_claude_mode(chat_id)

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
