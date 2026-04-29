"""Loop Engine handler — start, status, pause, resume, stop, guide."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...card.models import EngineCardState
from ...card.styles import UI_TEXT
from ...loop_engine.models import LoopProjectStatus
from ...utils.text import generate_task_id
from ..emoji import EmojiReaction
from ..renderers.loop_renderer import LoopRenderer
from .engine_base import BaseEngineHandler
from .base import CardActionContext

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class LoopHandler(BaseEngineHandler):
    """Manages the full lifecycle of Loop Engine tasks."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        self.renderer = LoopRenderer(self)

    def _get_engine_manager(self):
        return self.ctx.loop_engine_manager

    def _get_engine_name_prefix(self) -> str:
        return "Loop"

    def _get_task_type(self) -> str:
        return "loop_engine"

    def _show_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self.show_loop_status(message_id, chat_id, project)

    def _create_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str, root_path: str
    ):
        return self.renderer.create_loop_callbacks(message_id, chat_id, project, engine_name)

    def _refresh_card_view(self, message_id: str, chat_id: str, project=None):
        self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)

    def _create_loop_callbacks(self, *args, **kwargs):
        return self.renderer.create_loop_callbacks(*args, **kwargs)

    def _get_ui_state(self, loop_project_id: str) -> dict:
        """Deprecated: Delegate to renderer"""
        return self.renderer.get_ui_state(loop_project_id)

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------
    def handle_loop_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        text_lower = text.lower().strip()

        if text_lower == "/loop_status" or text_lower.startswith("/loop_status "):
            self.show_loop_status(message_id, chat_id, project)
        elif text_lower == "/stop_loop" or text_lower.startswith("/stop_loop "):
            self.stop_loop_engine(message_id, chat_id, project)
        elif text_lower == "/loop_pause":
            self.pause_loop_engine(message_id, chat_id, project)
        elif text_lower == "/loop_resume":
            self.resume_loop_engine(message_id, chat_id, project)
        elif text_lower.startswith("/loop_guide "):
            guide_message = text[len("/loop_guide ") :].strip()
            self.update_loop_guidance(message_id, chat_id, guide_message, project)
        elif text_lower == "/loop_guide":
            self.reply_error(
                message_id,
                UI_TEXT["loop_cmd_guide_usage"],
                title="参数错误",
            )
        elif text_lower.startswith("/loop "):
            requirement = text[6:].strip()
            self.start_loop_engine(message_id, chat_id, requirement, project)
        elif text_lower == "/loop":
            self.reply_error(
                message_id,
                UI_TEXT["loop_cmd_help_usage"],
                title="参数错误",
            )
        else:
            self.reply_error(message_id, "未知的 Loop 命令", title="未知命令")

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------
    def start_loop_engine(
        self, message_id: str, chat_id: str, requirement: str, project: Optional["ProjectContext"] = None
    ):
        project = self._ensure_project(message_id, chat_id, project)
        if not project:
            return

        root_path = project.root_path if project else self.get_working_dir(chat_id)

        existing = self.ctx.loop_engine_manager.get(chat_id, root_path)
        if existing and existing.is_running:
            self.reply_error(
                message_id,
                "当前项目已有 Loop 任务在执行中\n\n发送 `/loop_status` 查看进度\n发送 `/stop_loop` 停止任务",
                title="任务冲突",
            )
            return

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        request_id = self.ensure_request_id(
            message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
        )
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.loop_reporter

        # 发送启动卡片
        content = reporter.format_analyzing_start(requirement)
        title = reporter.get_analyzing_start_title()
        msg_type, card_content = CardBuilder.build_engine_card(
            project=project,
            state=EngineCardState(
                title=title,
                content=f"{content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else content,
                engine_name=f"Loop({engine_name})",
                show_buttons=False,
                action_prefix="loop",
            ),
        )
        self.reply_message(
            message_id, card_content, msg_type=msg_type, origin_message_id=message_id, request_id=request_id
        )

        engine = self.ctx.loop_engine_manager.get_or_create(chat_id, root_path, engine_name=engine_name)

        project_name = project.project_name if project else os.path.basename(root_path) or "loop"
        task_id = generate_task_id(project_name)

        _on_rate_limit = self.create_rate_limit_callback(
            chat_id, message_id, project, f"Loop({engine_name})", request_id
        )

        def run_loop_engine():
            def _executor():
                callbacks = self.renderer.create_loop_callbacks(message_id, chat_id, project, engine_name)
                engine.execute(requirement, callbacks, task_id=task_id, on_rate_limit=_on_rate_limit)

            self._safe_execute_engine(
                executor_func=_executor,
                task_id=task_id,
                chat_id=chat_id,
                message_id=message_id,
                project=project,
                engine_name=engine_name,
                reporter=reporter,
                request_id=request_id,
                action_prefix="loop",
                command_text=f"/loop {requirement}",
            )

        self._submit_engine_task(run_loop_engine, chat_id, message_id, project, request_id, task_id)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def show_loop_status(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
    ):
        # User command "/loop_status" resets to status view
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        loop_project_id = project.project_id if project else root_path

        self.renderer.update_ui_state(loop_project_id, view_mode="status", view_context={})

        self.renderer.render_current_view(message_id, chat_id, project, origin_message_id)

    # ------------------------------------------------------------------
    # pause / resume / stop
    # ------------------------------------------------------------------
    def pause_loop_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self._safe_lifecycle_action(
            lambda: self._pause_engine_generic(
                message_id, chat_id, project, status_paused_enum=LoopProjectStatus.PAUSED
            ),
            "pause",
            chat_id,
            message_id,
            project,
        )

    def resume_loop_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self._safe_lifecycle_action(
            lambda: self._resume_engine_generic(
                message_id, chat_id, project, status_paused_enum=LoopProjectStatus.PAUSED
            ),
            "resume",
            chat_id,
            message_id,
            project,
        )

    def stop_loop_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self._safe_lifecycle_action(
            lambda: self._stop_engine_generic(message_id, chat_id, project), "stop", chat_id, message_id, project
        )

    # ------------------------------------------------------------------
    # guidance
    # ------------------------------------------------------------------
    def update_loop_guidance(
        self, message_id: str, chat_id: str, guide_message: str, project: Optional["ProjectContext"] = None
    ):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        engine = None
        if project:
            engine = self.ctx.loop_engine_manager.get(chat_id, project.root_path)

        if not engine:
            running = self.ctx.loop_engine_manager.get_active_engines(chat_id)
            if len(running) == 1:
                engine = running[0]

        if not engine or not engine.is_running:
            self.reply_error(
                message_id, "当前没有正在运行的 Loop 任务\n\n请先使用 `/loop <需求>` 启动任务", title="无活动任务"
            )
            return

        engine.inject_guidance(guide_message)
        reporter = self.ctx.loop_reporter
        content = reporter.format_guidance_injected(guide_message)
        title = reporter.get_guidance_injected_title()
        engine_name = engine.engine_name

        msg_type, card_content = CardBuilder.build_engine_card(
            project=project,
            state=EngineCardState(
                title=title,
                content=content,
                engine_name=f"Loop({engine_name})",
                show_buttons=False,
                action_prefix="loop",
            ),
        )
        self.send_message(chat_id, card_content, msg_type)

    # ------------------------------------------------------------------
    # UI Interaction Handlers
    # ------------------------------------------------------------------
    def handle_card_action(self, open_message_id: str, open_chat_id: str, action_type: str, value: dict):
        """Handle loop_* card actions."""
        project_id = value.get("project_id", "")
        # Note: Loop engine uses 'deep_project_id' key for compatibility/convention with base templates,
        # but in Loop context it might be root_path or project_id.
        loop_project_id = value.get("deep_project_id", "")

        # Resolve target project (chat-scoped to prevent cross-chat leakage)
        target_project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
        if not target_project and loop_project_id:
            try:
                if os.path.isabs(loop_project_id):
                    target_project = self.project_manager.find_project_by_path(loop_project_id, chat_id=open_chat_id)
                else:
                    target_project = self.project_manager.get_project_for_chat(loop_project_id, open_chat_id)
            except Exception:
                logger.debug("failed to get target_project", exc_info=True)

        loop_actions = {
            "loop_pause": self.pause_loop_engine,
            "loop_resume": self.resume_loop_engine,
            "loop_stop": self.stop_loop_engine,
        }

        # Try dispatching standard actions first
        if self._dispatch_standard_card_action(CardActionContext(
            open_message_id=open_message_id,
            open_chat_id=open_chat_id,
            action_type=action_type,
            value=value,
            prefix="loop",
            action_map=loop_actions,
            toggle_log_method=self._toggle_log,
            switch_mode_method=self._switch_card_mode,
            toggle_ac_method=self._toggle_ac,
            project=target_project,
        )):
            return

        # Handle Loop specific actions
        if action_type == "loop_history":
            self.show_loop_history(
                open_message_id, open_chat_id, project=target_project, loop_project_id=loop_project_id
            )

        elif action_type == "loop_history_page":
            page = value.get("page", 1)
            self.handle_loop_history_page(
                open_message_id, open_chat_id, page, project=target_project, loop_project_id=loop_project_id
            )

        elif action_type == "loop_history_item":
            iteration_id = value.get("iteration_id")
            self.handle_loop_history_item(
                open_message_id, open_chat_id, iteration_id, project=target_project, loop_project_id=loop_project_id
            )

        elif action_type == "loop_back_to_list":
            self.show_loop_status(
                open_message_id, open_chat_id, project=target_project, origin_message_id=open_message_id
            )

    def show_loop_history(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        loop_project_id: Optional[str] = None,
    ):
        if loop_project_id:
            self.renderer.update_ui_state(loop_project_id, view_mode="history", history_page=1)
            # Refresh card with new state
            self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)

    def handle_loop_history_page(
        self,
        message_id: str,
        chat_id: str,
        page: int,
        project: Optional["ProjectContext"] = None,
        loop_project_id: Optional[str] = None,
    ):
        if loop_project_id:
            self.renderer.update_ui_state(loop_project_id, view_mode="history", history_page=page)
            self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)

    def handle_loop_history_item(
        self,
        message_id: str,
        chat_id: str,
        iteration_id: int,
        project: Optional["ProjectContext"] = None,
        loop_project_id: Optional[str] = None,
    ):
        if loop_project_id:
            self.renderer.update_ui_state(
                loop_project_id, view_mode="iteration_done", view_context={"iteration_id": iteration_id}
            )
            self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)
