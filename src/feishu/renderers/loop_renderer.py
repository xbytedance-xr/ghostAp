
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Any

from ...card import CardBuilder, DeepCardState
from ...loop_engine import LoopEngineCallbacks
from ...loop_engine.models import (
    LoopProject,
    LoopProjectStatus,
    IterationRecord,
    ReviewResult,
)
from ...utils.text import append_duration_to_title
from ..emoji import EmojiReaction
from .base import BaseRenderer, SmartSender

if TYPE_CHECKING:
    from ..handlers.loop import LoopHandler
    from ...project import ProjectContext

logger = logging.getLogger(__name__)

class LoopRenderer(BaseRenderer):
    """
    Handles UI rendering and state management for Loop Engine interactions.
    Separated from LoopHandler to improve maintainability.
    """

    def __init__(self, handler: "LoopHandler") -> None:
        super().__init__(handler)

    def get_default_ui_state(self) -> dict[str, Any]:
        return {
            "compact": self.settings.card_deep_compact_default,
            "expanded": False,
            "expand_ac": False,  # Default to collapsed
            "view_mode": "status",
            "view_context": {},
            "history_page": 1,
        }

    def create_loop_callbacks(self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str = "Coco") -> LoopEngineCallbacks:
        request_id = self.handler.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.loop_reporter
        
        sender = SmartSender(
            handler=self.handler,
            message_id=message_id,
            chat_id=chat_id,
            initial_message_id=None
        )
        
        # Calculate loop_project_id once for UI state lookups in this closure
        loop_project_id = project.project_id if project else self.handler.get_working_dir(chat_id)

        def _send_loop_message(card_content: str, msg_type: str = "interactive", is_update: bool = False, throttle: bool = False):
            sender.send(card_content, msg_type, is_update, throttle, request_id)

        def on_analyzing_done(loop_project: LoopProject):
            # View State Update: Status
            self.update_ui_state(loop_project_id, view_mode="status", view_context={})

            content = reporter.format_analyzing_done(loop_project)
            title = reporter.get_analyzing_done_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    engine_name=f"Loop({engine_name})",
                    show_buttons=False,
                )
            )
            # This is effectively the first "status" card we track. Immediate flush.
            _send_loop_message(card_content, msg_type, is_update=False, throttle=False)

        def on_iteration_start(current: int, max_iterations: int):
            # View State Update: Status
            self.update_ui_state(loop_project_id, view_mode="status", view_context={})

            engine = self.ctx.loop_engine_manager.get(chat_id, project.root_path if project else "")
            loop_project = engine.project if engine else None
            criteria_status = ""
            progress_bar = None
            status_line = None
            duration_line = None
            criteria_section = None
            # Re-fetch state (although reference is same)
            state = self.get_ui_state(loop_project_id)
            
            if loop_project:
                criteria_status = reporter.format_criteria_brief(loop_project)
                # progress_bar = reporter._make_progress_bar(loop_project.satisfied_count, loop_project.total_criteria)
                progress_bar = self._generate_progress_bar(loop_project.satisfied_count, loop_project.total_criteria)
                
                status_line = reporter.format_status_line(loop_project)
                duration_line = reporter.format_duration_line(loop_project)
                criteria_section = reporter.format_criteria_section(loop_project)
                
                # Apply AC folding
                criteria_section = self._render_collapsible_section(
                    criteria_section, 
                    loop_project.total_criteria,
                    state.get("expand_ac", False),
                    completed_count=loop_project.satisfied_count
                )

            content = reporter.format_iteration_start(current, max_iterations, criteria_status=criteria_status)
            title = reporter.get_iteration_start_title(current, max_iterations)
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    is_executing=True,
                    engine_name=f"Loop({engine_name})",
                    status_line=status_line,
                    duration_line=duration_line,
                    criteria_section=criteria_section,
                    deep_project_id=loop_project_id,
                    compact=state["compact"],
                    expanded=state["expanded"],
                    expand_ac=state.get("expand_ac", False),
                    action_prefix="loop",
                )
            )
            # Iteration start: updates existing card, can be throttled or immediate.
            # Usually start is significant, so let's flush immediately to show user "it started"
            _send_loop_message(card_content, msg_type, is_update=True, throttle=False)

        def on_iteration_done(iteration: int, record: IterationRecord):
            # View State Update: Iteration Done
            self.update_ui_state(loop_project_id, view_mode="iteration_done", view_context={"iteration_id": iteration})

            engine = self.ctx.loop_engine_manager.get(chat_id, project.root_path if project else "")
            if engine and engine.project:
                lp = engine.project
                iter_content = reporter.format_iteration_done(iteration, record)
                content = iter_content
                success = record.status.value == "success"
                title = reporter.get_iteration_done_title(success, iteration)
                title = append_duration_to_title(title, lp.duration())
                # progress_bar = reporter._make_progress_bar(lp.satisfied_count, lp.total_criteria)
                progress_bar = self._generate_progress_bar(lp.satisfied_count, lp.total_criteria)
                status_line = reporter.format_status_line(lp)
                duration_line = reporter.format_duration_line(lp)
                criteria_section = reporter.format_criteria_section(lp)
                
                state = self.get_ui_state(loop_project_id)
                
                # Apply AC folding
                criteria_section = self._render_collapsible_section(
                    criteria_section, 
                    lp.total_criteria,
                    state.get("expand_ac", False),
                    completed_count=lp.satisfied_count
                )
                
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project,
                    state=DeepCardState(
                        title=title,
                        content=content,
                        progress_bar=progress_bar,
                        is_executing=True,
                        engine_name=f"Loop({engine_name})",
                        status_line=status_line,
                        duration_line=duration_line,
                        criteria_section=criteria_section,
                        deep_project_id=loop_project_id,
                        compact=state["compact"],
                        expanded=state["expanded"],
                        expand_ac=state.get("expand_ac", False),
                        action_prefix="loop",
                    )
                )
                # Iteration done: significant state change, immediate flush
                _send_loop_message(card_content, msg_type, is_update=True, throttle=False)

        def on_review_done(iteration: int, review: ReviewResult):
            # View State Update: Review Done
            self.update_ui_state(loop_project_id, view_mode="review_done", view_context={"iteration_id": iteration})
            state = self.get_ui_state(loop_project_id)

            content = reporter.format_review_result(review)
            title = reporter.get_review_title(iteration, review.all_passed)
            engine = self.ctx.loop_engine_manager.get(chat_id, project.root_path if project else "")
            progress_bar = None
            status_line = None
            duration_line = None
            criteria_section = None
            if engine and engine.project:
                # progress_bar = reporter._make_progress_bar(engine.project.satisfied_count, engine.project.total_criteria)
                progress_bar = self._generate_progress_bar(engine.project.satisfied_count, engine.project.total_criteria)
                title = append_duration_to_title(title, engine.project.duration())
                status_line = reporter.format_status_line(engine.project)
                duration_line = reporter.format_duration_line(engine.project)
                criteria_section = reporter.format_criteria_section(engine.project)
                
                # Apply AC folding
                criteria_section = self._render_collapsible_section(
                    criteria_section, 
                    engine.project.total_criteria,
                    state.get("expand_ac", False),
                    completed_count=engine.project.satisfied_count
                )
            
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    is_executing=True,
                    engine_name=f"Loop({engine_name})",
                    status_line=status_line,
                    duration_line=duration_line,
                    criteria_section=criteria_section,
                    deep_project_id=loop_project_id,
                    compact=state["compact"],
                    expanded=state["expanded"],
                    expand_ac=state.get("expand_ac", False),
                    action_prefix="loop",
                )
            )
            # Review done: immediate flush
            _send_loop_message(card_content, msg_type, is_update=True, throttle=False)

        def on_project_done(loop_project: LoopProject):
            # View State Update: Status (completed)
            self.update_ui_state(loop_project_id, view_mode="status", view_context={})

            content = reporter.format_project_done(loop_project)
            title = reporter.get_project_done_title(loop_project)
            # progress_bar = reporter._make_progress_bar(loop_project.satisfied_count, loop_project.total_criteria)
            progress_bar = self._generate_progress_bar(loop_project.satisfied_count, loop_project.total_criteria)
            duration_line = reporter.format_duration_line(loop_project)
            
            state = self.get_ui_state(loop_project_id)
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    progress_bar=progress_bar,
                    engine_name=f"Loop({engine_name})",
                    duration_line=duration_line,
                    deep_project_id=loop_project_id,
                    compact=state["compact"],
                    expanded=state["expanded"],
                    expand_ac=state.get("expand_ac", False),
                    action_prefix="loop",
                )
            )
            # Project done: immediate flush
            _send_loop_message(card_content, msg_type, is_update=True, throttle=False)
            self.handler.add_reaction(message_id, EmojiReaction.on_multi_task_done())

        def on_error(error: str):
            # View State Update: Error
            self.update_ui_state(loop_project_id, view_mode="error", view_context={"error": error})

            content = reporter.format_error(error)
            title = reporter.get_error_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project,
                state=DeepCardState(
                    title=title,
                    content=content,
                    engine_name=f"Loop({engine_name})",
                    show_buttons=False,
                )
            )
            _send_loop_message(card_content, msg_type, is_update=True)
            self.handler.add_reaction(message_id, EmojiReaction.on_error())

        return LoopEngineCallbacks(
            on_analyzing_done=on_analyzing_done,
            on_iteration_start=on_iteration_start,
            on_iteration_done=on_iteration_done,
            on_review_done=on_review_done,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    def render_current_view(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, origin_message_id: Optional[str] = None):
        if project is None:
            project = self.handler.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.handler.get_working_dir(chat_id)
        engine = self.ctx.loop_engine_manager.get(chat_id, root_path)
        
        loop_project_id = project.project_id if project else root_path
        state = self.get_ui_state(loop_project_id)
        
        view_mode = state.get("view_mode", "status")
        view_context = state.get("view_context", {})
        
        if not engine or not engine.project:
            running = self.ctx.loop_engine_manager.get_active_engines(chat_id)
            if len(running) == 1 and running[0].project:
                engine = running[0]
            else:
                engine_name = self.handler.get_engine_name(chat_id, project_id=(project.project_id if project else None))
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project,
                    state=DeepCardState(
                        title="📊 Loop 状态",
                        content="当前没有 Loop 任务\n\n发送 `/loop 你的需求` 开始迭代式开发",
                        engine_name=f"Loop({engine_name})",
                        show_buttons=False,
                    )
                )
                self.handler.reply_message(message_id, card_content, msg_type=msg_type)
                return

        reporter = self.ctx.loop_reporter
        
        # Dispatch rendering based on view_mode
        if view_mode == "status":
            self._render_status_view(message_id, chat_id, project, engine, state, origin_message_id)
        elif view_mode == "iteration_done":
            iteration_id = view_context.get("iteration_id")
            self._render_iteration_view(message_id, chat_id, project, engine, state, iteration_id, origin_message_id)
        elif view_mode == "review_done":
            iteration_id = view_context.get("iteration_id")
            self._render_review_view(message_id, chat_id, project, engine, state, iteration_id, origin_message_id)
        elif view_mode == "error":
            error_msg = view_context.get("error", "未知错误")
            self._render_error_view(message_id, chat_id, project, engine, state, error_msg, origin_message_id)
        elif view_mode == "history":
            self._render_history_view(message_id, chat_id, project, engine, state, origin_message_id)
        else:
            # Fallback to status view
            self._render_status_view(message_id, chat_id, project, engine, state, origin_message_id)

    def _patch_or_send(self, message_id: str, chat_id: str, card_content: str, msg_type: str, origin_message_id: Optional[str] = None):
        """Helper to patch existing status message or send new one."""
        patched = False
        if origin_message_id:
            # Explicit UI interactions (view switch, refresh) should be immediate
            patched = self.handler.patch_message(origin_message_id, card_content, max_retries=1, throttle=False)
        
        if not patched:
            self.handler.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=origin_message_id)

    def _render_status_view(self, message_id: str, chat_id: str, project, engine, state, origin_message_id):
        reporter = self.ctx.loop_reporter
        engine_name = engine.engine_name
        
        status_content = reporter.format_status(engine.project)
        status_title = reporter.get_status_title()
        progress_info = reporter.get_progress_info(engine.project)
        
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            state=DeepCardState(
                title=status_title,
                content=status_content,
                progress_bar=self._generate_progress_bar(engine.project.satisfied_count, engine.project.total_criteria),
                is_executing=progress_info["is_running"],
                engine_name=f"Loop({engine_name})",
                deep_project_id=project.project_id if project else engine.project.root_path,
                compact=state["compact"],
                expanded=state["expanded"],
                expand_ac=state.get("expand_ac", False),
                action_prefix="loop",
            )
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)

    def _render_iteration_view(self, message_id: str, chat_id: str, project, engine, state, iteration_id, origin_message_id):
        reporter = self.ctx.loop_reporter
        engine_name = engine.engine_name
        loop_project = engine.project
        
        # Find the iteration record
        record = next((it for it in loop_project.iterations if it.iteration == iteration_id), None)
        if not record:
            # If not found, fallback to status
            self._render_status_view(message_id, chat_id, project, engine, state, origin_message_id)
            return

        iter_content = reporter.format_iteration_done(iteration_id, record)
        content = iter_content
        success = record.status.value == "success"
        title = reporter.get_iteration_done_title(success, iteration_id)
        title = append_duration_to_title(title, loop_project.duration())
        progress_bar = self._generate_progress_bar(loop_project.satisfied_count, loop_project.total_criteria)
        status_line = reporter.format_status_line(loop_project)
        duration_line = reporter.format_duration_line(loop_project)
        criteria_section = reporter.format_criteria_section(loop_project)
        
        # Apply AC folding
        criteria_section = self._render_collapsible_section(
            criteria_section, 
            loop_project.total_criteria,
            state.get("expand_ac", False),
            completed_count=loop_project.satisfied_count
        )
        
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            state=DeepCardState(
                title=title,
                content=content,
                progress_bar=progress_bar,
                is_executing=True,
                engine_name=f"Loop({engine_name})",
                status_line=status_line,
                duration_line=duration_line,
                criteria_section=criteria_section,
                deep_project_id=project.project_id if project else loop_project.root_path,
                compact=state["compact"],
                expanded=state["expanded"],
                expand_ac=state.get("expand_ac", False),
                action_prefix="loop",
            )
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)

    def _render_review_view(self, message_id: str, chat_id: str, project, engine, state, iteration_id, origin_message_id):
        reporter = self.ctx.loop_reporter
        engine_name = engine.engine_name
        loop_project = engine.project
        
        record = next((it for it in loop_project.iterations if it.iteration == iteration_id), None)
        if not record or not record.review_result:
            self._render_status_view(message_id, chat_id, project, engine, state, origin_message_id)
            return

        review = record.review_result
        content = reporter.format_review_result(review)
        title = reporter.get_review_title(iteration_id, review.all_passed)
        
        progress_bar = self._generate_progress_bar(loop_project.satisfied_count, loop_project.total_criteria)
        title = append_duration_to_title(title, loop_project.duration())
        status_line = reporter.format_status_line(loop_project)
        duration_line = reporter.format_duration_line(loop_project)
        criteria_section = reporter.format_criteria_section(loop_project)

        # Apply AC folding
        criteria_section = self._render_collapsible_section(
            criteria_section, 
            loop_project.total_criteria,
            state.get("expand_ac", False),
            completed_count=loop_project.satisfied_count
        )
        
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            state=DeepCardState(
                title=title,
                content=content,
                progress_bar=progress_bar,
                is_executing=True,
                engine_name=f"Loop({engine_name})",
                status_line=status_line,
                duration_line=duration_line,
                criteria_section=criteria_section,
                deep_project_id=project.project_id if project else loop_project.root_path,
                compact=state["compact"],
                expanded=state["expanded"],
                expand_ac=state.get("expand_ac", False),
                action_prefix="loop",
            )
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)

    def _render_error_view(self, message_id: str, chat_id: str, project, engine, state, error_msg, origin_message_id):
        reporter = self.ctx.loop_reporter
        engine_name = engine.engine_name
        
        content = reporter.format_error(error_msg)
        title = reporter.get_error_title()
        
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project,
            state=DeepCardState(
                title=title,
                content=content,
                engine_name=f"Loop({engine_name})",
                show_buttons=False,
                deep_project_id=project.project_id if project else engine.project.root_path,
                compact=state["compact"],
                expanded=state["expanded"],
                action_prefix="loop",
            )
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)

    def _render_history_view(self, message_id: str, chat_id: str, project, engine, state, origin_message_id):
        loop_project = engine.project
        
        iterations = loop_project.iterations
        total = len(iterations)
        page = state.get("history_page", 1)
        PAGE_SIZE = 5
        
        # Calculate pagination
        start_idx = (page - 1) * PAGE_SIZE
        end_idx = start_idx + PAGE_SIZE
        # In Loop Engine, iterations are usually appended, so latest is last.
        # But for history view, we might want reverse order (newest first).
        reversed_iterations = list(reversed(iterations))
        current_page_items = reversed_iterations[start_idx:end_idx] if start_idx < total else []
        has_next = end_idx < total
        
        history_buttons = []
        for it in current_page_items:
            status_icon = "✅" if it.status.value == "success" else "❌" if it.status.value == "failed" else "🔄"
            history_buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"{status_icon} 迭代 {it.iteration}"},
                "type": "default",
                "value": {
                    "action": "loop_history_item", 
                    "iteration_id": it.iteration,
                    "project_id": project.project_id if project else None,
                    "deep_project_id": project.project_id if project else loop_project.root_path
                }
            })
            
        content = f"共 {total} 次迭代"
        msg_type, card_content = CardBuilder.build_history_list_card(
            project=project,
            title="历史记录",
            content=content,
            history_buttons=history_buttons,
            page=page,
            has_next=has_next,
            deep_project_id=project.project_id if project else loop_project.root_path,
            engine_name=f"Loop({engine.engine_name})"
        )
        self._patch_or_send(message_id, chat_id, card_content, msg_type, origin_message_id)
