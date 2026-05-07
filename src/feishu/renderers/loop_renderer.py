from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from ...card.events import CardEvent
from ...card.render.budget import RenderBudget
from ...card.state.models import CardMetadata
from ...card.ui_text import UI_TEXT
from ...loop_engine import LoopEngineCallbacks
from ...loop_engine.models import (
    IterationRecord,
    LoopProject,
    ReviewResult,
)
from ...utils.text import append_duration_to_title
from ..emoji import EmojiReaction
from ._rotating_mixin import EngineView, RotatingRendererMixin
from .base import BaseRenderer, _ACPStreamBridge, _dispatch_text_block

if TYPE_CHECKING:
    from ...card.protocols import Dispatchable
    from ...card.session_rotator import SessionRotator
    from ...project import ProjectContext
    from ..handlers.loop import LoopHandler

logger = logging.getLogger(__name__)


class LoopRenderer(RotatingRendererMixin, BaseRenderer):
    """
    Handles UI rendering and state management for Loop Engine interactions.
    Separated from LoopHandler to improve maintainability.
    """

    _engine_type = "loop"
    _mode_prefix = "Loop"
    _mode_emoji = "🔁"
    _engine_cmd = "/loop"

    def __init__(self, handler: "LoopHandler") -> None:
        super().__init__(handler)
        self._current_session: "Dispatchable | None" = None

    def get_active_session(self) -> "Dispatchable | None":
        """Return the currently active loop engine session (rotator)."""
        return self._current_session

    def _get_reporter(self):
        return self.ctx.loop_reporter

    def get_default_ui_state(self) -> dict[str, Any]:
        state = super().get_default_ui_state()
        state["history_page"] = 0
        return state

    def create_loop_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str = "Coco"
    ) -> LoopEngineCallbacks:
        request_id = self.handler.ensure_request_id(
            message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
        )
        reporter = self.ctx.loop_reporter

        # Calculate loop_project_id once for UI state lookups in this closure
        loop_project_id = project.project_id if project else self.handler.get_working_dir(chat_id)

        # UI state for metadata
        state = self.get_ui_state(loop_project_id)
        metadata = CardMetadata(
            engine_type="loop",
            mode_name="Loop",
            mode_emoji="🔁",
            tool_name=engine_name,
            compact=state["compact"],
            expanded=state["expanded"],
            expand_ac=state.get("expand_ac", False),
        )

        # Session rotator: manages atomic session rotation at iteration boundaries
        # Hooks handle emoji reactions automatically on terminal events
        hooks = self._build_hooks(message_id)

        _loop_budget = RenderBudget(engine_cmd="/loop")
        rotator: SessionRotator = self._create_rotator(chat_id, message_id, metadata, hooks=hooks, budget=_loop_budget)
        self._current_session = rotator
        stream_bridge = _ACPStreamBridge(rotator)

        def _new_session(iteration: int):
            """Atomically rotate to a new session (iteration boundary)."""
            # Re-read UI state for updated preferences
            st = self.get_ui_state(loop_project_id)
            meta = self.build_unit_metadata(
                CardMetadata(
                    engine_type="loop",
                    mode_name="Loop",
                    mode_emoji="🔁",
                    tool_name=engine_name,
                    compact=st["compact"],
                    expanded=st["expanded"],
                    expand_ac=st.get("expand_ac", False),
                ),
                unit_id=str(iteration),
                unit_kind="iteration",
                unit_label=f"第 {iteration} 轮",
                continuation_seq=rotator.rotation_count + 1,
            )
            # Use old card's delivered message_id as reply_to for navigation chain
            old_msg_id = rotator.current.delivered_message_id or message_id
            rotator.rotate(lambda: self.create_session(chat_id, old_msg_id, meta, hooks=hooks, budget=_loop_budget))
            stream_bridge.bind(rotator)

        def on_analyzing_done(loop_project: LoopProject):
            # View State Update: Status
            self.update_ui_state(loop_project_id, view_mode="status", view_context={})

            content = reporter.format_analyzing_done(loop_project)
            rotator.dispatch(CardEvent.started())
            rotator.dispatch(CardEvent.text_delta("_main", content))

        def on_iteration_start(current: int, max_iterations: int):
            # View State Update: Status
            self.update_ui_state(loop_project_id, view_mode="status", view_context={})

            _new_session(current)
            rotator.dispatch(CardEvent.started())

            snap = self.ctx.loop_engine_manager.snapshot(chat_id, project.root_path if project else "")
            lp = snap.ext.get("project") if snap else None

            # Dispatch cycle start
            rotator.dispatch(CardEvent.cycle_started(current, max_iterations))

            # Dispatch criteria/warning if available
            if lp:
                criteria_section = reporter.format_criteria_section(lp)
                rotator.dispatch(CardEvent.criteria_updated(
                    criteria_section,
                    satisfied_count=lp.satisfied_count,
                    total_count=lp.total_criteria,
                ))

                warning = self.check_warning_banner(lp.duration(), is_executing=True)
                if warning:
                    rotator.dispatch(CardEvent.warning_updated(warning))

        def on_iteration_event(iteration: int, event):
            stream_bridge.on_event(event)

        def on_iteration_done(iteration: int, record: IterationRecord):
            # View State Update: Iteration Done
            self.update_ui_state(loop_project_id, view_mode="iteration_done", view_context={"iteration_id": iteration})
            stream_bridge.close_open_blocks()

            snap = self.ctx.loop_engine_manager.snapshot(chat_id, project.root_path if project else "")
            if snap and snap.ext.get("project"):
                lp = snap.ext["project"]
                # Dispatch cycle done on current session
                rotator.dispatch(CardEvent.cycle_done(iteration))
                iter_content = reporter.format_iteration_done(iteration, record)
                _dispatch_text_block(rotator, f"_iteration_done_{iteration}", iter_content)

                criteria_section = reporter.format_criteria_section(lp)
                rotator.dispatch(CardEvent.criteria_updated(
                    criteria_section,
                    satisfied_count=lp.satisfied_count,
                    total_count=lp.total_criteria,
                ))

        def on_review_done(iteration: int, review: ReviewResult):
            # View State Update: Review Done
            self.update_ui_state(loop_project_id, view_mode="review_done", view_context={"iteration_id": iteration})

            snap = self.ctx.loop_engine_manager.snapshot(chat_id, project.root_path if project else "")
            content = reporter.format_review_result(review)
            _dispatch_text_block(rotator, f"_review_{iteration}", content)

            if snap and snap.ext.get("project"):
                lp = snap.ext["project"]
                criteria_section = reporter.format_criteria_section(lp)
                rotator.dispatch(CardEvent.criteria_updated(
                    criteria_section,
                    satisfied_count=lp.satisfied_count,
                    total_count=lp.total_criteria,
                ))

                warning = self.check_warning_banner(lp.duration(), is_executing=True)
                if warning:
                    rotator.dispatch(CardEvent.warning_updated(warning))

            if not review.all_passed:
                rotator.dispatch(CardEvent.review_retry(
                    cycle_num=iteration,
                    attempt=iteration,
                    max_attempts=0,
                    status="executing",
                ))

        def on_project_done(loop_project: LoopProject):
            # View State Update: Status (completed)
            self.update_ui_state(loop_project_id, view_mode="status", view_context={})
            stream_bridge.close_open_blocks()
            content = reporter.format_project_done(loop_project)
            _dispatch_text_block(rotator, "_project_done", content)

            criteria_section = reporter.format_criteria_section(loop_project)
            rotator.dispatch(CardEvent.criteria_updated(
                criteria_section,
                satisfied_count=loop_project.satisfied_count,
                total_count=loop_project.total_criteria,
            ))

            # Terminal event — auto-closes session (hooks fire emoji automatically)
            if loop_project.status.value == "completed":
                rotator.dispatch(CardEvent.completed())
            else:
                rotator.dispatch(CardEvent.failed(UI_TEXT["card_project_failed"]))
            self._current_session = None

        def on_error(error: str):
            # View State Update: Error
            self.update_ui_state(loop_project_id, view_mode="error", view_context={"error": error})

            # Hooks fire emoji automatically on terminal delivery
            stream_bridge.close_open_blocks()
            rotator.dispatch(CardEvent.failed(error))
            self._current_session = None

        return LoopEngineCallbacks(
            on_analyzing_done=on_analyzing_done,
            on_iteration_start=on_iteration_start,
            on_iteration_event=on_iteration_event,
            on_iteration_done=on_iteration_done,
            on_review_done=on_review_done,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    def render_current_view(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
    ):
        if project is None:
            project = self.handler.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.handler.get_working_dir(chat_id)
        snap = self.ctx.loop_engine_manager.snapshot(chat_id, root_path)

        loop_project_id = project.project_id if project else root_path
        state = self.get_ui_state(loop_project_id)

        view_mode = state.get("view_mode", "status")
        view_context = state.get("view_context", {})

        if not snap or not snap.ext.get("project"):
            snaps = self.ctx.loop_engine_manager.snapshot_active(chat_id)
            if len(snaps) == 1 and snaps[0].ext.get("project"):
                snap = snaps[0]
            else:
                engine_name = self.handler.get_engine_name(
                    chat_id, project_id=(project.project_id if project else None)
                )
                metadata = CardMetadata(
                    engine_type="loop",
                    mode_name="Loop",
                    mode_emoji="🔁",
                    tool_name=engine_name,
                )
                session = self.create_session(chat_id, message_id, metadata, budget=RenderBudget(engine_cmd="/loop"))
                session.dispatch(CardEvent.started())
                session.dispatch(CardEvent.text_started("_status"))
                session.dispatch(CardEvent.text_delta("_status", UI_TEXT["loop_status_empty"]))
                session.dispatch(CardEvent.text_done("_status"))
                session.dispatch(CardEvent.completed())
                return

        # Dispatch rendering based on view_mode
        engine = EngineView(snap)

        if view_mode == "status":
            self._render_status_view(message_id, chat_id, project, engine, state)
        elif view_mode == "iteration_done":
            iteration_id = view_context.get("iteration_id")
            self._render_iteration_view(message_id, chat_id, project, engine, state, iteration_id)
        elif view_mode == "review_done":
            iteration_id = view_context.get("iteration_id")
            self._render_review_view(message_id, chat_id, project, engine, state, iteration_id)
        elif view_mode == "error":
            error_msg = view_context.get("error", "未知错误")
            self._render_error_view(message_id, chat_id, project, engine, state, error_msg)
        elif view_mode == "history":
            self._render_history_view(message_id, chat_id, project, engine, state)
        else:
            # Fallback to status view
            self._render_status_view(message_id, chat_id, project, engine, state)

    def _render_iteration_view(
        self, message_id: str, chat_id: str, project, engine, state, iteration_id
    ):
        reporter = self.ctx.loop_reporter
        engine_name = engine.engine_name
        loop_project = engine.project

        # Find the iteration record
        record = next((it for it in loop_project.iterations if it.iteration == iteration_id), None)
        if not record:
            self._render_status_view(message_id, chat_id, project, engine, state)
            return

        session = self._build_view_session(chat_id, message_id, engine_name, state)
        session.dispatch(CardEvent.started())

        iter_content = reporter.format_iteration_done(iteration_id, record)
        session.dispatch(CardEvent.text_delta("_iteration", iter_content))

        # Criteria
        criteria_section = reporter.format_criteria_section(loop_project)
        session.dispatch(CardEvent.criteria_updated(
            criteria_section,
            satisfied_count=loop_project.satisfied_count,
            total_count=loop_project.total_criteria,
        ))

        session.dispatch(CardEvent.completed())

    def _render_review_view(
        self, message_id: str, chat_id: str, project, engine, state, iteration_id
    ):
        reporter = self.ctx.loop_reporter
        engine_name = engine.engine_name
        loop_project = engine.project

        record = next((it for it in loop_project.iterations if it.iteration == iteration_id), None)
        if not record or not record.review_result:
            self._render_status_view(message_id, chat_id, project, engine, state)
            return

        session = self._build_view_session(chat_id, message_id, engine_name, state)
        session.dispatch(CardEvent.started())

        review = record.review_result
        content = reporter.format_review_result(review)
        session.dispatch(CardEvent.text_delta("_review", content))

        # Criteria
        criteria_section = reporter.format_criteria_section(loop_project)
        session.dispatch(CardEvent.criteria_updated(
            criteria_section,
            satisfied_count=loop_project.satisfied_count,
            total_count=loop_project.total_criteria,
        ))

        # Warning
        warning = self.check_warning_banner(loop_project.duration(), is_executing=True)
        if warning:
            session.dispatch(CardEvent.warning_updated(warning))

        session.dispatch(CardEvent.completed())

    def _render_history_view(self, message_id: str, chat_id: str, project, engine, state):
        reporter = self.ctx.loop_reporter
        engine_name = engine.engine_name
        loop_project = engine.project

        iterations = loop_project.iterations
        total = len(iterations)
        page = state.get("history_page", 1)
        PAGE_SIZE = 5

        start_idx = (page - 1) * PAGE_SIZE
        end_idx = start_idx + PAGE_SIZE
        reversed_iterations = list(reversed(iterations))
        current_page_items = reversed_iterations[start_idx:end_idx] if start_idx < total else []

        # Build history content
        lines = [f"📋 共 {total} 次迭代\n"]
        for it in current_page_items:
            status_icon = "✅" if it.status.value == "success" else "❌" if it.status.value == "failed" else "🔄"
            lines.append(f"{status_icon} **迭代 {it.iteration}**")

        session = self._build_view_session(chat_id, message_id, engine_name, state)
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.text_delta("_history", "\n".join(lines)))
        session.dispatch(CardEvent.completed())
