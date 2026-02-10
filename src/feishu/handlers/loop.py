"""Loop Engine handler — start, status, pause, resume, stop, guide."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...loop_engine import LoopEngineCallbacks
from ...loop_engine.models import (
    LoopProject,
    LoopProjectStatus,
    IterationRecord,
)
from ...tasking import TaskSpec, TaskPriority
from ..emoji import EmojiReaction
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class LoopHandler(BaseHandler):
    """Manages the full lifecycle of Loop Engine tasks."""

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
            guide_message = text[len("/loop_guide "):].strip()
            self.update_loop_guidance(message_id, chat_id, guide_message, project)
        elif text_lower == "/loop_guide":
            self.reply_message(message_id, "📝 请提供引导信息\n\n用法: `/loop_guide <引导描述>`\n\n例如: `/loop_guide 优先实现邮箱注册功能`")
        elif text_lower.startswith("/loop "):
            requirement = text[6:].strip()
            self.start_loop_engine(message_id, chat_id, requirement, project)
        elif text_lower == "/loop":
            self.reply_message(message_id, "📝 请提供产品诉求\n\n用法: `/loop <你的需求描述>`\n\n例如: `/loop 实现用户登录注册功能，支持邮箱和手机号`\n\n可用命令:\n• `/loop <需求>` - 启动 Loop 模式\n• `/loop_guide <引导>` - 注入引导信息\n• `/loop_status` - 查看进度\n• `/loop_pause` - 暂停迭代\n• `/loop_resume` - 恢复迭代\n• `/stop_loop` - 停止 Loop")
        else:
            self.reply_message(message_id, "❓ 未知的 Loop 命令")

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------
    def start_loop_engine(self, message_id: str, chat_id: str, requirement: str, project: Optional["ProjectContext"] = None):
        if not project:
            working_dir = self.get_working_dir(chat_id)
            try:
                project, is_new = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    logger.info("Loop Engine 自动创建项目: %s @ %s", project.project_name, project.root_path)
            except Exception as e:
                self.reply_message(message_id, f"❌ 创建项目失败: {e}")
                return

        root_path = project.root_path if project else self.get_working_dir(chat_id)

        existing = self.ctx.loop_engine_manager.get(chat_id, root_path)
        if existing and existing.is_running:
            self.reply_message(
                message_id,
                "⚠️ 当前项目已有 Loop 任务在执行中\n\n发送 `/loop_status` 查看进度\n发送 `/stop_loop` 停止任务",
            )
            return

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        engine_name = self.get_engine_name(chat_id)
        reporter = self.ctx.loop_reporter

        # 发送启动卡片
        content = reporter.format_analyzing_start(requirement)
        title = reporter.get_analyzing_start_title()
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=title,
            content=f"{content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else content,
            engine_name=f"Loop({engine_name})", show_buttons=False,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=message_id, request_id=request_id)

        engine = self.ctx.loop_engine_manager.get_or_create(chat_id, root_path, engine_name=engine_name)

        def run_loop_engine():
            try:
                callbacks = self._create_loop_callbacks(message_id, chat_id, project, engine_name)
                engine.execute(requirement, callbacks)
            except Exception as e:
                logger.error("Loop Engine 执行异常: %s", e, exc_info=True)
                error_content = reporter.format_error(str(e))
                error_title = reporter.get_error_title()
                err_msg_type, err_card = CardBuilder.build_deep_card(
                    project=project, title=error_title,
                    content=f"{error_content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else error_content,
                    engine_name=f"Loop({engine_name})", show_buttons=False,
                )
                self.send_message(chat_id, err_card, err_msg_type, origin_message_id=message_id, request_id=request_id)

        spec = TaskSpec(
            chat_id=chat_id,
            queue_key=f"{chat_id}:loop:{project.project_id if project else root_path}",
            name="loop_engine_run",
            task_type="loop_engine",
            project_id=project.project_id if project else None,
            message_id=message_id,
            origin_message_id=message_id,
            request_id=request_id,
            priority=TaskPriority.NORMAL,
        )
        handle = self.scheduler.submit(spec, lambda ctx: run_loop_engine())
        try:
            self.ctx.message_linker.link_task(message_id, handle.run_id)
        except Exception as e:
            logger.debug("link_task失败(loop_engine_run): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)

    # ------------------------------------------------------------------
    # callbacks factory
    # ------------------------------------------------------------------
    def _create_loop_callbacks(self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str = "Coco") -> LoopEngineCallbacks:
        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.loop_reporter
        thread_root_message_id: list[str | None] = [None]

        def _send_loop_message(card_content: str, msg_type: str = "interactive"):
            use_thread = self.settings.default_reply_mode == "thread"
            if use_thread:
                reply_to = thread_root_message_id[0] or message_id
                result_id = self.reply_message(
                    reply_to, card_content, msg_type=msg_type,
                    origin_message_id=message_id, request_id=request_id,
                    reply_in_thread=True,
                )
                if thread_root_message_id[0] is None and result_id:
                    thread_root_message_id[0] = result_id
            else:
                self.send_message(chat_id, card_content, msg_type, origin_message_id=message_id, request_id=request_id)

        def on_analyzing_done(loop_project: LoopProject):
            content = reporter.format_analyzing_done(loop_project)
            title = reporter.get_analyzing_done_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                engine_name=f"Loop({engine_name})", show_buttons=False,
            )
            _send_loop_message(card_content, msg_type)

        def on_iteration_start(current: int, max_iterations: int):
            content = reporter.format_iteration_start(current, max_iterations)
            title = reporter.get_iteration_start_title(current, max_iterations)
            engine = self.ctx.loop_engine_manager.get(chat_id, project.root_path if project else "")
            loop_project = engine.project if engine else None
            progress_bar = None
            if loop_project:
                progress_bar = reporter._make_progress_bar(loop_project.satisfied_count, loop_project.total_criteria)
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                progress_bar=progress_bar,
                is_executing=True, engine_name=f"Loop({engine_name})",
            )
            _send_loop_message(card_content, msg_type)

        def on_iteration_done(iteration: int, record: IterationRecord):
            engine = self.ctx.loop_engine_manager.get(chat_id, project.root_path if project else "")
            if engine and engine.project:
                lp = engine.project
                content = reporter.format_iteration_done(iteration, record)
                success = record.status.value == "success"
                title = reporter.get_iteration_done_title(success, iteration)
                progress_bar = reporter._make_progress_bar(lp.satisfied_count, lp.total_criteria)
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project, title=title, content=content,
                    progress_bar=progress_bar,
                    is_executing=True, engine_name=f"Loop({engine_name})",
                )
                _send_loop_message(card_content, msg_type)

        def on_project_done(loop_project: LoopProject):
            content = reporter.format_project_done(loop_project)
            title = reporter.get_project_done_title(loop_project)
            progress_bar = reporter._make_progress_bar(loop_project.satisfied_count, loop_project.total_criteria)
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                progress_bar=progress_bar, engine_name=f"Loop({engine_name})",
            )
            _send_loop_message(card_content, msg_type)
            self.add_reaction(message_id, EmojiReaction.on_multi_task_done())

        def on_error(error: str):
            content = reporter.format_error(error)
            title = reporter.get_error_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                engine_name=f"Loop({engine_name})", show_buttons=False,
            )
            _send_loop_message(card_content, msg_type)
            self.add_reaction(message_id, EmojiReaction.on_error())

        return LoopEngineCallbacks(
            on_analyzing_done=on_analyzing_done,
            on_iteration_start=on_iteration_start,
            on_iteration_done=on_iteration_done,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def show_loop_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.loop_engine_manager.get(chat_id, root_path)
        reporter = self.ctx.loop_reporter

        if not engine or not engine.project:
            running = self.ctx.loop_engine_manager.get_active_engines(chat_id)
            if len(running) == 1 and running[0].project:
                engine = running[0]
            else:
                engine_name = self.get_engine_name(chat_id)
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project, title="📊 Loop 状态",
                    content="当前没有 Loop 任务\n\n发送 `/loop 你的需求` 开始迭代式开发",
                    engine_name=f"Loop({engine_name})", show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        status_content = reporter.format_status(engine.project)
        status_title = reporter.get_status_title()
        progress_info = reporter.get_progress_info(engine.project)
        engine_name = engine.engine_name
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=status_title, content=status_content,
            progress_bar=progress_info["progress_bar"],
            is_executing=progress_info["is_running"],
            engine_name=f"Loop({engine_name})",
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    # ------------------------------------------------------------------
    # pause / resume / stop
    # ------------------------------------------------------------------
    def pause_loop_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.loop_engine_manager.get(chat_id, root_path)
        if not engine:
            engine = self.ctx.loop_engine_manager.get_active_engine(chat_id)
        if engine and engine.is_running:
            engine.pause()
            self.show_loop_status(message_id, chat_id, project=project)
            return
        self.reply_message(message_id, "当前没有正在执行的 Loop 任务")

    def resume_loop_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.loop_engine_manager.get(chat_id, root_path)

        if not engine:
            paused = [e for e in self.ctx.loop_engine_manager.list_engines(chat_id)
                      if e.project and e.project.status == LoopProjectStatus.PAUSED]
            if len(paused) == 1:
                engine = paused[0]

        if engine and engine.project and engine.project.status == LoopProjectStatus.PAUSED:
            callbacks = self._create_loop_callbacks(message_id, chat_id, project, engine_name=engine.engine_name)

            def run_resume():
                engine.resume(callbacks)

            request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
            spec = TaskSpec(
                chat_id=chat_id,
                queue_key=f"{chat_id}:loop:{project.project_id if project else root_path}",
                name="loop_engine_resume", task_type="loop_engine",
                project_id=project.project_id if project else None,
                message_id=message_id, origin_message_id=message_id,
                request_id=request_id, priority=TaskPriority.HIGH,
            )
            handle = self.scheduler.submit(spec, lambda ctx: run_resume())
            try:
                self.ctx.message_linker.link_task(message_id, handle.run_id)
            except Exception as e:
                logger.debug("link_task失败(loop_engine_resume): err=%s", e)
            self.show_loop_status(message_id, chat_id, project=project)
        else:
            self.reply_message(message_id, "当前没有可恢复的 Loop 任务")

    def stop_loop_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.loop_engine_manager.get(chat_id, root_path)

        if not engine:
            running = self.ctx.loop_engine_manager.get_active_engines(chat_id)
            if len(running) == 1:
                engine = running[0]

        if not engine or not engine.is_running:
            self.reply_message(message_id, "📊 当前没有正在执行的 Loop 任务")
            return

        engine.stop()
        self.show_loop_status(message_id, chat_id, project=project)

    # ------------------------------------------------------------------
    # guidance
    # ------------------------------------------------------------------
    def update_loop_guidance(self, message_id: str, chat_id: str, guide_message: str, project: Optional["ProjectContext"] = None):
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
            self.reply_message(message_id, "⚠️ 当前没有正在运行的 Loop 任务\n\n请先使用 `/loop <需求>` 启动任务")
            return

        engine.inject_guidance(guide_message)
        reporter = self.ctx.loop_reporter
        content = reporter.format_guidance_injected(guide_message)
        title = reporter.get_guidance_injected_title()
        engine_name = engine.engine_name

        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=title, content=content,
            engine_name=f"Loop({engine_name})", show_buttons=False,
        )
        self.send_message(chat_id, card_content, msg_type)
