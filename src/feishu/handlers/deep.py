"""Deep Engine handler — start, status, pause, resume, stop, update context."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...deep_engine.models import DeepProjectStatus
from ...tasking import TaskPriority, TaskSpec
from ...utils.command_parser import CommandParser
from ...utils.errors import get_error_detail
from ...utils.text import generate_task_id
from ..emoji import EmojiReaction
from ..renderers.deep_renderer import DeepRenderer
from .engine_base import BaseEngineHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class DeepHandler(BaseEngineHandler):
    """Manages the full lifecycle of Deep Engine tasks."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        self.renderer = DeepRenderer(self)

    def _get_engine_manager(self):
        return self.ctx.deep_engine_manager

    def _get_engine_name_prefix(self) -> str:
        return "Deep Agent"

    def _get_task_type(self) -> str:
        return "deep_engine"

    def _show_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self.show_deep_status(message_id, chat_id, project)

    def _create_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str, root_path: str
    ):
        return self.renderer.create_deep_callbacks(message_id, chat_id, project, engine_name, root_path)

    def _create_deep_callbacks(self, *args, **kwargs):
        return self.renderer.create_deep_callbacks(*args, **kwargs)

    def _get_ui_state(self, deep_project_id: str) -> dict:
        """Deprecated: Delegate to renderer"""
        return self.renderer.get_ui_state(deep_project_id)

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------
    def handle_deep_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        cmd = CommandParser.parse_basic(text)
        command = cmd.command
        arg = cmd.args

        if command == "/deep_status":
            # Check for flags in args (e.g. "all", "-a", "--all")
            # The basic parser puts these in cmd.flags if the whole string is flags
            is_all = False
            if cmd.flags.get("all") or cmd.flags.get("a"):
                is_all = True
            elif arg in ("all", "-a", "--all"):
                # Fallback for manual check if parser didn't catch mixed case or single token
                is_all = True

            if is_all:
                self.show_deep_board(message_id, chat_id)
            else:
                self.show_deep_status(message_id, chat_id, project)
        elif command == "/stop_deep":
            is_all = False
            if cmd.flags.get("all") or cmd.flags.get("a"):
                is_all = True
            elif arg in ("all", "-a", "--all"):
                is_all = True

            if is_all:
                self.stop_all_deep_engines(message_id, chat_id)
            else:
                self.stop_deep_engine(message_id, chat_id, project)
        elif command == "/deep_update":
            if arg:
                self.update_deep_context(message_id, chat_id, arg, project)
            else:
                self.reply_error(
                    message_id,
                    "请提供上下文信息\n\n用法: `/deep_update <上下文描述>`\n\n例如: `/deep_update 数据库改用 PostgreSQL 而不是 SQLite`",
                    title="参数错误",
                )
        elif command == "/deep":
            if arg:
                self.start_deep_engine(message_id, chat_id, arg, project)
            else:
                self.reply_error(
                    message_id,
                    "请提供需求描述\n\n用法: `/deep <你的需求描述>`\n\n例如: `/deep 帮我写一个 Python 爬虫，爬取豆瓣电影 Top250`",
                    title="参数错误",
                )
        else:
            self.reply_error(
                message_id,
                "未知的 Deep 命令\n\n可用命令:\n• `/deep <需求>` - 启动 Deep Agent\n• `/deep_update <上下文>` - 注入执行上下文\n• `/deep_status` - 查看当前项目进度\n• `/deep_status --all` - 查看所有项目 Deep Agent 看板\n• `/stop_deep` - 停止当前项目任务\n• `/stop_deep --all` - 停止所有项目任务",
                title="未知命令",
            )

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------
    def start_deep_engine(
        self, message_id: str, chat_id: str, requirement: str, project: Optional["ProjectContext"] = None
    ):
        if not project:
            working_dir = self.get_working_dir(chat_id)
            try:
                project, is_new = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    logger.info("Deep Engine 自动创建项目: %s @ %s", project.project_name, project.root_path)
            except Exception as e:
                from ...utils.errors import get_error_detail
                self.reply_error(message_id, get_error_detail(e), title="创建项目失败")
                return

        root_path = project.root_path if project else self.get_working_dir(chat_id)

        existing = self.ctx.deep_engine_manager.get(chat_id, root_path)
        if existing and existing.is_running:
            self.reply_error(
                message_id,
                "当前项目已有 Deep Agent 任务在执行中\n\n发送 `/deep_status` 查看进度\n发送 `/stop_deep` 停止任务",
                title="任务冲突",
            )
            return

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        request_id = self.ensure_request_id(
            message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
        )
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.progress_reporter

        planning_content = reporter.format_planning_start(requirement)
        planning_title = reporter.get_planning_start_title()
        msg_type, card_content = CardBuilder.build_engine_card(
            project=project,
            title=planning_title,
            content=f"{planning_content}\n\n{self.format_ref_note(message_id, request_id)}"
            if request_id
            else planning_content,
            engine_name=engine_name,
            show_buttons=False,
        )
        initial_msg_id = self.reply_message(
            message_id, card_content, msg_type=msg_type, origin_message_id=message_id, request_id=request_id
        )

        engine = self.ctx.deep_engine_manager.get_or_create(chat_id, root_path, engine_name=engine_name)

        project_name = project.project_name if project else os.path.basename(root_path) or "deep"
        task_id = generate_task_id(project_name)

        _on_rate_limit = self.create_rate_limit_callback(chat_id, message_id, project, engine_name, request_id)

        def run_deep_engine():
            def _executor():
                callbacks = self.renderer.create_deep_callbacks(
                    message_id,
                    chat_id,
                    project,
                    engine_name,
                    root_path=root_path,
                    initial_message_id=initial_msg_id,
                )
                engine.plan_and_execute(requirement, callbacks, task_id=task_id, on_rate_limit=_on_rate_limit)

            self._safe_execute_engine(
                executor_func=_executor,
                task_id=task_id,
                chat_id=chat_id,
                message_id=message_id,
                project=project,
                engine_name=engine_name,
                reporter=reporter,
                request_id=request_id,
                action_prefix="deep",
                command_text=f"/deep {requirement}",
            )

        spec = TaskSpec(
            chat_id=chat_id,
            queue_key=f"{chat_id}:deep:{project.project_id if project else root_path}",
            name="deep_engine_run",
            task_type="deep_engine",
            project_id=project.project_id if project else None,
            message_id=message_id,
            origin_message_id=message_id,
            request_id=request_id,
            task_id=task_id,
            priority=TaskPriority.NORMAL,
        )
        handle = self.scheduler.submit(spec, lambda ctx: run_deep_engine())
        try:
            self.ctx.message_linker.link_task(message_id, handle.run_id)
        except Exception as e:
            logger.debug(
                "link_task失败(deep_engine_run): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e
            )

    # ------------------------------------------------------------------
    # status / board
    # ------------------------------------------------------------------
    def show_deep_status(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
    ):
        self.renderer.render_deep_status(message_id, chat_id, project, origin_message_id)

    def show_deep_board(self, message_id: str, chat_id: str):
        engines = self.ctx.deep_engine_manager.list_engines(chat_id)
        candidates = []
        for e in engines:
            if not e.project:
                continue
            if e.project.status in (DeepProjectStatus.EXECUTING, DeepProjectStatus.PAUSED, DeepProjectStatus.IDLE):
                candidates.append(e)

        if not candidates:
            engine_name = self.get_engine_name(chat_id, project_id=None)
            msg_type, card_content = CardBuilder.build_engine_card(
                project=None,
                title="📊 Deep Agent 看板",
                content="当前没有 Deep Agent 任务\n\n发送 `/deep <需求>` 开始一个复杂任务",
                engine_name=engine_name,
                show_buttons=False,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        def _sort_key(e):
            running_rank = 0 if e.is_running else 1
            paused_rank = 0 if (e.project and e.project.status == DeepProjectStatus.PAUSED) else 1
            return (running_rank, paused_rank, -(e.project.started_at or 0))

        candidates.sort(key=_sort_key)
        reporter = self.ctx.progress_reporter
        lines = ["**Deep Agent 任务（按项目）**", ""]
        for e in candidates[:10]:
            proj = None
            try:
                proj = self.project_manager.find_project_by_path(e.root_path, chat_id=chat_id)
            except Exception:
                proj = None
            proj_name = proj.project_name if proj else (e.project.name or "unknown")
            root = e.root_path
            info = reporter.get_progress_info(
                e.project,
                completed=e.progress.completed_steps,
                total=e.progress.total_steps,
            )
            status = e.project.status.value
            lines.append(f"- 🧠 **{proj_name}** · `{status}` · {info['progress_bar']} · `{root}`")

        msg_type, card_content = CardBuilder.build_smart_response_card(
            project=None,
            title="📊 Deep Agent 看板",
            content="\n".join(lines),
            working_dir=self.get_working_dir(chat_id),
            show_buttons=True,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    # ------------------------------------------------------------------
    # pause / resume / stop
    # ------------------------------------------------------------------
    def pause_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self._safe_lifecycle_action(
            lambda: self._pause_engine_generic(
                message_id, chat_id, project, status_paused_enum=DeepProjectStatus.PAUSED
            ),
            "pause",
            chat_id,
            message_id,
            project,
        )

    def resume_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self._safe_lifecycle_action(
            lambda: self._resume_engine_generic(
                message_id, chat_id, project, status_paused_enum=DeepProjectStatus.PAUSED
            ),
            "resume",
            chat_id,
            message_id,
            project,
        )

    def stop_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        self._safe_lifecycle_action(
            lambda: self._stop_engine_generic(message_id, chat_id, project), "stop", chat_id, message_id, project
        )

    def stop_all_deep_engines(self, message_id: str, chat_id: str):
        from ...card.styles import UI_TEXT

        engines = self.ctx.deep_engine_manager.get_active_engines(chat_id)
        if not engines:
            self.reply_error(
                message_id,
                UI_TEXT["deep_no_active_tasks"],
                title="无活动任务",
            )
            return

        for e in engines:
            try:
                e.stop()
            except Exception as ex:
                logger.debug("停止deep engine失败: %s", get_error_detail(ex))

        msg = UI_TEXT["deep_stop_all_success"].format(count=len(engines))
        self.reply_message(message_id, msg)

    # ------------------------------------------------------------------
    # update context
    # ------------------------------------------------------------------
    def update_deep_context(
        self, message_id: str, chat_id: str, update_message: str, project: Optional["ProjectContext"] = None
    ):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        engine = None
        if project:
            engine = self.ctx.deep_engine_manager.get(chat_id, project.root_path)

        if not engine:
            running = self.ctx.deep_engine_manager.get_active_engines(chat_id)
            if len(running) == 1:
                engine = running[0]

        if not engine and project is None:
            root_path = self.get_working_dir(chat_id)
            engine = self.ctx.deep_engine_manager.get(chat_id, root_path)

        if not engine or not engine.is_running:
            self.reply_error(
                message_id,
                "当前没有正在运行的 Deep Agent 任务\n\n请先使用 `/deep <需求>` 启动任务，或使用 `/deep_status all` 查看所有项目任务",
                title="无活动任务",
            )
            return

        engine.inject_guidance(update_message)
        reporter = self.ctx.progress_reporter
        content = reporter.format_context_injected(update_message)
        title = reporter.get_context_injected_title()
        engine_name = engine.engine_name

        if project is None:
            try:
                project = self.project_manager.find_project_by_path(engine.root_path, chat_id=chat_id)
            except Exception:
                project = None

        msg_type, card_content = CardBuilder.build_engine_card(
            project=project,
            title=title,
            content=content,
            engine_name=engine_name,
            show_buttons=False,
        )
        self.send_message(chat_id, card_content, msg_type)

    # ------------------------------------------------------------------
    # UI Interaction Handlers
    # ------------------------------------------------------------------
    def handle_card_action(self, open_message_id: str, open_chat_id: str, action_type: str, value: dict):
        """Handle deep_* card actions."""
        project_id = value.get("project_id", "")
        deep_project_id = value.get("deep_project_id", "")

        # Resolve target project (chat-scoped to prevent cross-chat leakage)
        target_project = self.project_manager.get_project_for_chat(project_id, open_chat_id) if project_id else None
        if not target_project and deep_project_id:
            try:
                engine = self.ctx.deep_engine_manager.find_by_deep_project_id(open_chat_id, deep_project_id)
                if engine:
                    target_project = self.project_manager.find_project_by_path(engine.root_path, chat_id=open_chat_id)
            except Exception as e:
                logger.debug("resolve_deep_target_project失败: %s", get_error_detail(e))
                target_project = None

        deep_actions = {
            "deep_pause": self.pause_deep_engine,
            "deep_resume": self.resume_deep_engine,
            "deep_stop": self.stop_deep_engine,
        }

        self._dispatch_standard_card_action(
            open_message_id,
            open_chat_id,
            action_type,
            value,
            prefix="deep",
            action_map=deep_actions,
            toggle_log_method=self.toggle_deep_log,
            switch_mode_method=self.switch_deep_card_mode,
            toggle_ac_method=self.toggle_deep_ac,
            project=target_project,
        )

    def toggle_deep_log(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        deep_project_id: Optional[str] = None,
        expanded: bool = False,
    ):
        if deep_project_id:
            self.renderer.update_ui_state(deep_project_id, expanded=expanded)
            # Refresh card with new state
            self.show_deep_status(message_id, chat_id, project, origin_message_id=message_id)

    def toggle_deep_ac(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        deep_project_id: Optional[str] = None,
        expand_ac: bool = False,
    ):
        if deep_project_id:
            self.renderer.update_ui_state(deep_project_id, expand_ac=expand_ac)
            # Refresh card with new state
            self.show_deep_status(message_id, chat_id, project, origin_message_id=message_id)

    def switch_deep_card_mode(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        deep_project_id: Optional[str] = None,
        compact: bool = False,
    ):
        if deep_project_id:
            self.renderer.update_ui_state(deep_project_id, compact=compact)
            # Refresh card with new state
            self.show_deep_status(message_id, chat_id, project, origin_message_id=message_id)
