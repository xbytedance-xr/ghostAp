"""Deep Engine handler — start, status, pause, resume, stop, update context."""

from __future__ import annotations

import logging
import os
import asyncio
from typing import TYPE_CHECKING, Optional


from ...card import CardBuilder
from ...deep_engine.models import DeepProjectStatus
from ...tasking import TaskSpec, TaskPriority
from ...utils.errors import fmt_error
from ...utils.text import generate_task_id
from ..emoji import EmojiReaction
from .base import BaseHandler
from ..renderers.deep_renderer import DeepRenderer

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class DeepHandler(BaseHandler):
    """Manages the full lifecycle of Deep Engine tasks."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        self.renderer = DeepRenderer(self)

    def _create_deep_callbacks(self, *args, **kwargs):
        return self.renderer.create_deep_callbacks(*args, **kwargs)

    def _get_ui_state(self, deep_project_id: str) -> dict:
        """Deprecated: Delegate to renderer"""
        return self.renderer.get_ui_state(deep_project_id)

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------
    def handle_deep_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        text_lower = text.lower().strip()

        if text_lower == "/deep_status" or text_lower.startswith("/deep_status "):
            arg = text_lower[len("/deep_status"):].strip()
            if arg in ("all", "-a", "--all"):
                self.show_deep_board(message_id, chat_id)
            else:
                self.show_deep_status(message_id, chat_id, project)
        elif text_lower == "/stop_deep" or text_lower.startswith("/stop_deep "):
            arg = text_lower[len("/stop_deep"):].strip()
            if arg in ("all", "-a", "--all"):
                self.stop_all_deep_engines(message_id, chat_id)
            else:
                self.stop_deep_engine(message_id, chat_id, project)
        elif text_lower.startswith("/deep_update "):
            update_message = text[len("/deep_update "):].strip()
            self.update_deep_context(message_id, chat_id, update_message, project)
        elif text_lower == "/deep_update":
            self.reply_message(message_id, "📝 请提供上下文信息\n\n用法: `/deep_update <上下文描述>`\n\n例如: `/deep_update 数据库改用 PostgreSQL 而不是 SQLite`")
        elif text_lower.startswith("/deep "):
            requirement = text[6:].strip()
            self.start_deep_engine(message_id, chat_id, requirement, project)
        elif text_lower == "/deep":
            self.reply_message(message_id, "📝 请提供需求描述\n\n用法: `/deep <你的需求描述>`\n\n例如: `/deep 帮我写一个 Python 爬虫，爬取豆瓣电影 Top250`")
        else:
            self.reply_message(message_id, "❓ 未知的 Deep 命令\n\n可用命令:\n• `/deep <需求>` - 启动 Deep Agent\n• `/deep_update <上下文>` - 注入执行上下文\n• `/deep_status` - 查看当前项目进度\n• `/deep_status all` - 查看所有项目 Deep Agent 看板\n• `/stop_deep` - 停止当前项目任务\n• `/stop_deep all` - 停止所有项目任务")

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------
    def start_deep_engine(self, message_id: str, chat_id: str, requirement: str, project: Optional["ProjectContext"] = None):
        if not project:
            working_dir = self.get_working_dir(chat_id)
            try:
                project, is_new = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    logger.info("Deep Engine 自动创建项目: %s @ %s", project.project_name, project.root_path)
            except Exception as e:
                self.reply_message(message_id, fmt_error("创建项目", e))
                return

        root_path = project.root_path if project else self.get_working_dir(chat_id)

        existing = self.ctx.deep_engine_manager.get(chat_id, root_path)
        if existing and existing.is_running:
            self.reply_message(
                message_id,
                "⚠️ 当前项目已有 Deep Agent 任务在执行中\n\n发送 `/deep_status` 查看进度\n发送 `/stop_deep` 停止任务",
            )
            return

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.progress_reporter

        planning_content = reporter.format_planning_start(requirement)
        planning_title = reporter.get_planning_start_title()
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            title=planning_title,
            content=f"{planning_content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else planning_content,
            engine_name=engine_name,
            show_buttons=False,
        )
        initial_msg_id = self.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=message_id, request_id=request_id)

        engine = self.ctx.deep_engine_manager.get_or_create(chat_id, root_path, engine_name=engine_name)

        project_name = project.project_name if project else os.path.basename(root_path) or "deep"
        task_id = generate_task_id(project_name)

        _on_rate_limit = self.create_rate_limit_callback(chat_id, message_id, project, engine_name, request_id)

        def run_deep_engine():
            try:
                callbacks = self.renderer.create_deep_callbacks(
                    message_id,
                    chat_id,
                    project,
                    engine_name,
                    root_path=root_path,
                    initial_message_id=initial_msg_id,
                )
                engine.plan_and_execute(requirement, callbacks, task_id=task_id, on_rate_limit=_on_rate_limit)
            except Exception as e:
                logger.error("Deep Engine 执行异常: %s", e, exc_info=True)
                # 使用增强的 fmt_error 处理异常消息
                formatted = fmt_error("", e)
                if formatted.startswith("❌ 失败: "):
                    err_msg = formatted[len("❌ 失败: "):]
                elif formatted == "❌ 失败":
                    err_msg = "未知错误"
                else:
                    err_msg = formatted
                
                error_content = reporter.format_error(err_msg)
                error_title = reporter.get_error_title()
                err_msg_type, err_card = CardBuilder.build_deep_card(
                    project=project,
                    title=error_title,
                    content=f"{error_content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else error_content,
                    engine_name=engine_name,
                    show_buttons=False,
                )
                self.send_message(chat_id, err_card, err_msg_type, origin_message_id=message_id, request_id=request_id)

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
            logger.debug("link_task失败(deep_engine_run): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)

    # ------------------------------------------------------------------
    # status / board
    # ------------------------------------------------------------------
    def show_deep_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, origin_message_id: Optional[str] = None):
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
            msg_type, card_content = CardBuilder.build_deep_card(
                project=None, title="📊 Deep Agent 看板",
                content="当前没有 Deep Agent 任务\n\n发送 `/deep <需求>` 开始一个复杂任务",
                engine_name=engine_name, show_buttons=False,
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
                proj = self.project_manager.find_project_by_path(e.root_path)
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
            project=None, title="📊 Deep Agent 看板", content="\n".join(lines),
            working_dir=self.get_working_dir(chat_id), show_buttons=True,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    # ------------------------------------------------------------------
    # pause / resume / stop
    # ------------------------------------------------------------------
    def pause_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.deep_engine_manager.get(chat_id, root_path)
        if not engine:
            engine = self.ctx.deep_engine_manager.get_active_engine(chat_id)
        if engine and engine.is_running:
            engine.pause()
            self.show_deep_status(message_id, chat_id, project=project)
            return
        self.reply_message(message_id, "当前没有正在执行的任务")

    def resume_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.deep_engine_manager.get(chat_id, root_path)

        if not engine:
            paused = [e for e in self.ctx.deep_engine_manager.list_engines(chat_id)
                      if e.project and e.project.status == DeepProjectStatus.PAUSED]
            if len(paused) == 1:
                engine = paused[0]
            elif len(paused) > 1:
                self.reply_message(message_id, "⚠️ 有多个项目存在可恢复的 Deep Agent 任务，请使用 `/deep_status all` 查看后切换项目再恢复")
                return

        if engine and engine.project and engine.project.status == DeepProjectStatus.PAUSED:
            if project is None:
                try:
                    project = self.project_manager.find_project_by_path(engine.root_path)
                except Exception:
                    project = None

            callbacks = self.renderer.create_deep_callbacks(
                message_id,
                chat_id,
                project,
                engine_name=engine.engine_name,
                root_path=engine.root_path,
            )

            def run_resume():
                engine.resume(callbacks)

            request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
            spec = TaskSpec(
                chat_id=chat_id,
                queue_key=f"{chat_id}:deep:{project.project_id if project else root_path}",
                name="deep_engine_resume", task_type="deep_engine",
                project_id=project.project_id if project else None,
                message_id=message_id, origin_message_id=message_id,
                request_id=request_id, priority=TaskPriority.HIGH,
            )
            handle = self.scheduler.submit(spec, lambda ctx: run_resume())
            try:
                self.ctx.message_linker.link_task(message_id, handle.run_id)
            except Exception as e:
                logger.debug("link_task失败(deep_engine_resume): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)
            self.show_deep_status(message_id, chat_id, project=project)
        else:
            self.reply_message(message_id, "当前没有可恢复的任务")

    def stop_deep_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.deep_engine_manager.get(chat_id, root_path)

        if not engine:
            running = self.ctx.deep_engine_manager.get_active_engines(chat_id)
            if len(running) == 1:
                engine = running[0]
            elif len(running) > 1:
                self.reply_message(message_id, "⚠️ 有多个项目正在执行 Deep Agent 任务，请使用 `/stop_deep all` 或先 `/switch <项目>` 再停止")
                return

        if not engine or not engine.is_running:
            self.reply_message(message_id, "📊 当前没有正在执行的 Deep Agent 任务")
            return

        engine.stop()
        self.show_deep_status(message_id, chat_id, project=project)

    def stop_all_deep_engines(self, message_id: str, chat_id: str):
        engines = self.ctx.deep_engine_manager.get_active_engines(chat_id)
        if not engines:
            self.reply_message(message_id, "📊 当前没有正在执行的 Deep Agent 任务")
            return
        for e in engines:
            try:
                e.stop()
            except Exception as ex:
                logger.debug("停止deep engine失败: %s", ex)
        self.reply_message(message_id, f"🛑 已发送停止信号：{len(engines)} 个 Deep Agent 任务将在当前步骤完成后停止")

    # ------------------------------------------------------------------
    # update context
    # ------------------------------------------------------------------
    def update_deep_context(self, message_id: str, chat_id: str, update_message: str, project: Optional["ProjectContext"] = None):
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
            self.reply_message(message_id, "⚠️ 当前没有正在运行的 Deep Agent 任务\n\n请先使用 `/deep <需求>` 启动任务，或使用 `/deep_status all` 查看所有项目任务")
            return

        engine.inject_context(update_message)
        reporter = self.ctx.progress_reporter
        content = reporter.format_context_injected(update_message)
        title = reporter.get_context_injected_title()
        engine_name = engine.engine_name

        if project is None:
            try:
                project = self.project_manager.find_project_by_path(engine.root_path)
            except Exception:
                project = None

        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=title, content=content,
            engine_name=engine_name, show_buttons=False,
        )
        self.send_message(chat_id, card_content, msg_type)

    # ------------------------------------------------------------------
    # UI Interaction Handlers
    # ------------------------------------------------------------------
    def handle_card_action(self, open_message_id: str, open_chat_id: str, action_type: str, value: dict):
        """Handle deep_* card actions."""
        project_id = value.get("project_id", "")
        deep_project_id = value.get("deep_project_id", "")
        
        # Resolve target project
        target_project = self.project_manager.get_project(project_id) if project_id else None
        if not target_project and deep_project_id:
            try:
                engine = self.ctx.deep_engine_manager.find_by_deep_project_id(open_chat_id, deep_project_id)
                if engine:
                    target_project = self.project_manager.find_project_by_path(engine.root_path)
            except Exception as e:
                logger.debug("resolve_deep_target_project失败: %s", e)
                target_project = None

        deep_actions = {
            "deep_pause":  self.pause_deep_engine,
            "deep_resume": self.resume_deep_engine,
            "deep_stop":   self.stop_deep_engine,
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
            project=target_project,
        )

    def toggle_deep_log(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, deep_project_id: Optional[str] = None, expanded: bool = False):
        if deep_project_id:
            self.renderer.update_ui_state(deep_project_id, expanded=expanded)
            # Refresh card with new state
            self.show_deep_status(message_id, chat_id, project, origin_message_id=message_id)

    def switch_deep_card_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, deep_project_id: Optional[str] = None, compact: bool = False):
        if deep_project_id:
            self.renderer.update_ui_state(deep_project_id, compact=compact)
            # Refresh card with new state
            self.show_deep_status(message_id, chat_id, project, origin_message_id=message_id)
