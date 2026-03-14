"""Loop Engine handler — start, status, pause, resume, stop, guide."""

from __future__ import annotations

import logging
import os
import asyncio
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...card.models import DeepCardState
from ...loop_engine.models import LoopProjectStatus
from ...tasking import TaskSpec, TaskPriority
from ...utils.errors import fmt_error
from ...utils.text import generate_task_id
from ..emoji import EmojiReaction
from .base import BaseHandler
from ..renderers.loop_renderer import LoopRenderer

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class LoopHandler(BaseHandler):
    """Manages the full lifecycle of Loop Engine tasks."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        self.renderer = LoopRenderer(self)

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
                self.reply_message(message_id, fmt_error("创建项目", e))
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
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.loop_reporter

        # 发送启动卡片
        content = reporter.format_analyzing_start(requirement)
        title = reporter.get_analyzing_start_title()
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            state=DeepCardState(
                title=title,
                content=f"{content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else content,
                engine_name=f"Loop({engine_name})",
                show_buttons=False,
                action_prefix="loop",
            )
        )
        self.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=message_id, request_id=request_id)

        engine = self.ctx.loop_engine_manager.get_or_create(chat_id, root_path, engine_name=engine_name)

        project_name = project.project_name if project else os.path.basename(root_path) or "loop"
        task_id = generate_task_id(project_name)

        _on_rate_limit = self.create_rate_limit_callback(chat_id, message_id, project, f"Loop({engine_name})", request_id)

        def run_loop_engine():
            try:
                callbacks = self.renderer.create_loop_callbacks(message_id, chat_id, project, engine_name)
                engine.execute(requirement, callbacks, task_id=task_id, on_rate_limit=_on_rate_limit)
            except Exception as e:
                logger.error("Loop Engine 执行异常: %s", e, exc_info=True)
                # 使用增强的 fmt_error 处理异常消息（包含空消息 TimeoutError 的自动兜底）
                # 注意：fmt_error 返回带 emoji 的完整消息，这里我们只需要提取处理后的 detail
                # 但为了复用逻辑且保持 reporter 的格式，我们这里模拟 fmt_error 的内部逻辑
                # 或者更好地，既然 fmt_error 已经增强，我们可以直接用它，但 format_error 会再加一层框。
                
                # 为了完全符合"unified call"的要求，我们应该尽量复用 errors.py 中的逻辑。
                # 但 errors.py 没有暴露 extraction logic。
                # 考虑到本次优化的核心是"TimeoutError空消息"，我们可以直接利用 fmt_error 的副作用：
                # 它会把 TimeoutError 变成友好的字符串。
                
                # 既然 fmt_error 返回 "❌ {action}失败: {detail}"，我们可以传入 action=""
                formatted = fmt_error("", e)
                # formatted is "❌ 失败: 操作超时..." OR "❌ 失败: error msg"
                
                # Remove the prefix to get the message content for reporter
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
                    state=DeepCardState(
                        title=error_title,
                        content=f"{error_content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else error_content,
                        engine_name=f"Loop({engine_name})",
                        show_buttons=False,
                        action_prefix="loop",
                    )
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
            task_id=task_id,
            priority=TaskPriority.NORMAL,
        )
        handle = self.scheduler.submit(spec, lambda ctx: run_loop_engine())
        try:
            self.ctx.message_linker.link_task(message_id, handle.run_id)
        except Exception as e:
            logger.debug("link_task失败(loop_engine_run): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def show_loop_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, origin_message_id: Optional[str] = None):
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
            callbacks = self.renderer.create_loop_callbacks(message_id, chat_id, project, engine_name=engine.engine_name)

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
            project=project,
            state=DeepCardState(
                title=title,
                content=content,
                engine_name=f"Loop({engine_name})",
                show_buttons=False,
                action_prefix="loop",
            )
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
        
        # Resolve target project
        target_project = self.project_manager.get_project(project_id) if project_id else None
        if not target_project and loop_project_id:
            try:
                if os.path.isabs(loop_project_id):
                     target_project = self.project_manager.find_project_by_path(loop_project_id)
                else:
                     target_project = self.project_manager.get_project(loop_project_id)
            except Exception:
                pass

        loop_actions = {
            "loop_pause":  self.pause_loop_engine,
            "loop_resume": self.resume_loop_engine,
            "loop_stop":   self.stop_loop_engine,
        }

        # Try dispatching standard actions first
        if self._dispatch_standard_card_action(
            open_message_id,
            open_chat_id,
            action_type,
            value,
            prefix="loop",
            action_map=loop_actions,
            toggle_log_method=self.toggle_loop_log,
            switch_mode_method=self.switch_loop_card_mode,
            project=target_project,
        ):
            return

        # Handle Loop specific actions
        if action_type == "loop_history":
            self.show_loop_history(
                open_message_id, open_chat_id, 
                project=target_project,
                loop_project_id=loop_project_id
            )
            
        elif action_type == "loop_history_page":
            page = value.get("page", 1)
            self.handle_loop_history_page(
                open_message_id, open_chat_id, page,
                project=target_project,
                loop_project_id=loop_project_id
            )
            
        elif action_type == "loop_history_item":
            iteration_id = value.get("iteration_id")
            self.handle_loop_history_item(
                open_message_id, open_chat_id, iteration_id,
                project=target_project,
                loop_project_id=loop_project_id
            )
            
        elif action_type == "loop_back_to_list":
            self.show_loop_status(
                open_message_id, open_chat_id, 
                project=target_project,
                origin_message_id=open_message_id
            )

    def show_loop_history(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, loop_project_id: Optional[str] = None):
        if loop_project_id:
            self.renderer.update_ui_state(loop_project_id, view_mode="history", history_page=1)
            # Refresh card with new state
            self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)

    def handle_loop_history_page(self, message_id: str, chat_id: str, page: int, project: Optional["ProjectContext"] = None, loop_project_id: Optional[str] = None):
        if loop_project_id:
            self.renderer.update_ui_state(loop_project_id, view_mode="history", history_page=page)
            self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)

    def handle_loop_history_item(self, message_id: str, chat_id: str, iteration_id: int, project: Optional["ProjectContext"] = None, loop_project_id: Optional[str] = None):
        if loop_project_id:
            self.renderer.update_ui_state(loop_project_id, view_mode="iteration_done", view_context={"iteration_id": iteration_id})
            self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)

    def toggle_loop_log(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, loop_project_id: Optional[str] = None, expanded: bool = False):
        if loop_project_id:
            self.renderer.update_ui_state(loop_project_id, expanded=expanded)
            # Refresh card with new state
            self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)

    def switch_loop_card_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, loop_project_id: Optional[str] = None, compact: bool = False):
        if loop_project_id:
            self.renderer.update_ui_state(loop_project_id, compact=compact)
            # Refresh card with new state
            self.renderer.render_current_view(message_id, chat_id, project, origin_message_id=message_id)
