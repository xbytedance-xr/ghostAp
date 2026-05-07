"""SpecStreamProcessor — extracted state object for Spec Engine event callbacks.

Replaces the closure-heavy `SpecRenderer.create_spec_callbacks` approach with
an explicit state class. All mutable state that was previously captured via
`nonlocal` or list-wrapping patterns is now held as plain instance attributes.
"""

from __future__ import annotations

import logging
from dataclasses import replace as _replace
from typing import TYPE_CHECKING, NamedTuple, Optional

from ...acp import ACPEventRenderer, ACPEventType
from ...card.events import CardEvent, card_event_from_acp
from ...card.render.budget import RenderBudget
from ...card.render.throttle import StreamThrottle
from ...card.state.models import CardMetadata
from ...card.ui_text import UI_TEXT
from ...spec_engine import SpecEngineCallbacks
from ...spec_engine.models import (
    ReviewResult,
    SpecCycle,
    SpecPhase,
    SpecProject,
)
from ...spec_engine.retry_status import RetryEvent, RetryStatus

if TYPE_CHECKING:
    from ...card.session_rotator import SessionRotator
    from ...spec_engine.reporter import SpecReporter

logger = logging.getLogger(__name__)

# RetryStatus → UI_TEXT key mapping (class-level constant)
_RETRY_STATUS_TEXT: dict[RetryStatus, str] = {
    RetryStatus.WAITING: "retry_waiting",
    RetryStatus.EXECUTING: "retry_executing",
    RetryStatus.EXHAUSTED: "retry_exhausted",
    RetryStatus.NO_RETRY: "retry_no_retry",
}


class _EngineContext(NamedTuple):
    """Lightweight return type for _get_engine_and_state()."""
    snap: object  # EngineSnapshot | None
    spec_project: "SpecProject | None"
    state: dict
    max_cycles: int


class SpecStreamProcessor:
    """Holds mutable state and implements all Spec Engine event callbacks.

    Constructed once per `create_spec_callbacks` invocation.  The renderer
    delegates callback creation to this class via `build_callbacks()`.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        rotator: "SessionRotator",
        reporter: "SpecReporter",
        metadata: CardMetadata,
        hooks: tuple,
        budget: RenderBudget,
        spec_project_id: str,
        message_id: str,
        chat_id: str,
        # Renderer/handler references (kept opaque to avoid circular import)
        renderer,  # SpecRenderer instance
        project_root_path: str,
        throttle: StreamThrottle,
    ) -> None:
        # Immutable dependencies
        self._rotator = rotator
        self._reporter = reporter
        self._metadata = metadata
        self._hooks = hooks
        self._budget = budget
        self._spec_project_id = spec_project_id
        self._message_id = message_id
        self._chat_id = chat_id
        self._renderer = renderer
        self._project_root_path = project_root_path

        # Mutable state (previously closure-captured variables)
        self._max_cycles: int = 0
        self._throttle: StreamThrottle = throttle
        self._acp_renderer: ACPEventRenderer = ACPEventRenderer()
        self._footer_status: Optional[str] = None
        self._last_phase_content: str = ""
        # Build phase tool tracking
        self._build_tool_count: int = 0
        self._build_file_set: set[str] = set()

    # ------------------------------------------------------------------
    # Private helpers (previously nested closures)
    # ------------------------------------------------------------------

    def _rotate_session(self) -> None:
        """Atomically rotate to a new session (cycle boundary)."""
        cont_meta = _replace(self._metadata, continuation_seq=self._rotator.rotation_count + 1)
        old_msg_id = self._rotator.current.delivered_message_id or self._message_id
        renderer = self._renderer
        chat_id = self._chat_id
        hooks = self._hooks
        budget = self._budget
        self._rotator.rotate(
            lambda: renderer.create_session(chat_id, old_msg_id, cont_meta, hooks=hooks, budget=budget)
        )

    def _get_engine_and_state(self) -> _EngineContext:
        """Return structured engine context."""
        ctx = self._renderer.ctx
        snap = ctx.spec_engine_manager.snapshot(self._chat_id, self._project_root_path)
        spec_project = snap.ext.get("project") if snap else None
        state = self._renderer.get_ui_state(self._spec_project_id)
        max_c = self._max_cycles or (spec_project.cycle_count_total if spec_project else 10)
        return _EngineContext(snap=snap, spec_project=spec_project, state=state, max_cycles=max_c)

    # ------------------------------------------------------------------
    # Callback methods
    # ------------------------------------------------------------------

    def on_analyzing_done(self, spec_project: SpecProject) -> None:
        self._renderer.update_ui_state(self._spec_project_id, view_mode="status", view_context={})
        self._rotator.dispatch(CardEvent.started())
        content = self._reporter.format_analyzing_done(spec_project)
        self._rotator.dispatch(CardEvent.text_delta("_main", content))

    def on_cycle_start(self, current: int, max_cycles: int) -> None:
        self._max_cycles = max_cycles
        self._renderer.update_ui_state(self._spec_project_id, view_mode="status", view_context={})

        _, spec_project, state, _ = self._get_engine_and_state()

        self._rotator.dispatch(CardEvent.cycle_started(current, max_cycles))

        if spec_project:
            criteria_section = self._reporter.format_criteria_section(spec_project)
            self._rotator.dispatch(CardEvent.criteria_updated(
                criteria_section,
                satisfied_count=spec_project.satisfied_count,
                total_count=spec_project.total_criteria,
            ))

            warning = self._renderer.check_warning_banner(spec_project.duration())
            if warning:
                self._rotator.dispatch(CardEvent.warning_updated(warning))

    def on_cycle_done(self, cycle_num: int, cycle: SpecCycle) -> None:
        self._renderer.update_ui_state(
            self._spec_project_id, view_mode="cycle_done", view_context={"cycle_num": cycle_num}
        )

        _, spec_project, state, _ = self._get_engine_and_state()
        if spec_project:
            self._rotator.dispatch(CardEvent.cycle_done(cycle_num))

            # Cycle boundary: rotate to new card
            self._rotate_session()

            self._rotator.dispatch(CardEvent.started())
            content = self._reporter.format_cycle_done(cycle_num, cycle)
            self._rotator.dispatch(CardEvent.text_delta("_cycle_done", content))

            criteria_section = self._reporter.format_criteria_section(spec_project)
            self._rotator.dispatch(CardEvent.criteria_updated(
                criteria_section,
                satisfied_count=spec_project.satisfied_count,
                total_count=spec_project.total_criteria,
            ))

    def on_review_done(self, cycle_num: int, review: ReviewResult) -> None:
        self._renderer.update_ui_state(
            self._spec_project_id, view_mode="review_done", view_context={"cycle_num": cycle_num}
        )

        content = self._reporter.format_review_result(review, cycle_num)
        self._rotator.dispatch(CardEvent.text_delta("_review", content))

        _, spec_project, state, _ = self._get_engine_and_state()
        if spec_project:
            criteria_section = self._reporter.format_criteria_section(spec_project)
            self._rotator.dispatch(CardEvent.criteria_updated(
                criteria_section,
                satisfied_count=spec_project.satisfied_count,
                total_count=spec_project.total_criteria,
            ))

            warning = self._renderer.check_warning_banner(spec_project.duration(), is_executing=True)
            if warning:
                self._rotator.dispatch(CardEvent.warning_updated(warning))

        if not review.all_passed:
            self._rotator.dispatch(CardEvent.review_retry(
                cycle_num=cycle_num,
                attempt=cycle_num,
                max_attempts=0,
                status="executing",
            ))

    def on_project_done(self, spec_project: SpecProject) -> None:
        self._renderer.update_ui_state(self._spec_project_id, view_mode="status", view_context={})

        # Project done: rotate to final card
        self._rotate_session()

        self._rotator.dispatch(CardEvent.started())
        content = self._reporter.format_project_done(spec_project)
        self._rotator.dispatch(CardEvent.text_delta("_main", content))

        criteria_section = self._reporter.format_criteria_section(spec_project)
        self._rotator.dispatch(CardEvent.criteria_updated(
            criteria_section,
            satisfied_count=spec_project.satisfied_count,
            total_count=spec_project.total_criteria,
        ))

        # Terminal event (hooks fire emoji automatically)
        if spec_project.status.value == "completed":
            self._rotator.dispatch(CardEvent.completed())
        else:
            self._rotator.dispatch(CardEvent.failed(UI_TEXT["card_project_failed"]))
        self._renderer._current_session = None

    def on_error(self, error: str) -> None:
        self._renderer.update_ui_state(
            self._spec_project_id, view_mode="error", view_context={"error": error}
        )
        # Hooks fire emoji automatically on terminal delivery
        self._rotator.dispatch(CardEvent.failed(error))
        self._renderer._current_session = None

    def on_phase_start(self, cycle_num: int, phase: SpecPhase) -> None:
        self._acp_renderer.reset()
        self._footer_status = "tool_running"

        _, spec_project, state, max_c = self._get_engine_and_state()
        subtitle = self._reporter.format_phase_subtitle(cycle_num, max_c, phase, completed=False)

        self._rotator.dispatch(
            CardEvent.phase_started(cycle_num, phase.value if hasattr(phase, 'value') else str(phase), subtitle=subtitle)
        )

        if phase == SpecPhase.BUILD:
            # Build phase: use tool panels, no text content needed
            self._build_tool_count = 0
            self._build_file_set = set()
        else:
            # Non-Build phases: keep text-based display (without phase progress bar in body)
            content = f"{phase.emoji} **{phase.display_name}** 执行中..."
            self._last_phase_content = content
            self._rotator.dispatch(CardEvent.text_delta("_phase", content))

    def on_phase_event(self, cycle_num: int, phase: SpecPhase, event) -> None:
        """Real-time ACP event processing."""
        self._acp_renderer.process_event(event)

        # Track footer_status
        if event.event_type == ACPEventType.THOUGHT_CHUNK:
            self._footer_status = "thinking"
        elif event.event_type in (ACPEventType.TOOL_CALL_START, ACPEventType.TOOL_CALL_UPDATE):
            self._footer_status = "tool_running"
        elif event.event_type == ACPEventType.TEXT_CHUNK:
            self._footer_status = None

        if phase == SpecPhase.BUILD:
            # Build phase: forward ACP events as native CardEvents (tool panels, plan)
            self._dispatch_build_event(event)
        else:
            # Non-Build phases: text-based display (existing behavior)
            if event.event_type not in (
                ACPEventType.TOOL_CALL_DONE,
                ACPEventType.PLAN_UPDATE,
            ):
                return

            tool_summary = self._acp_renderer.render_summary()
            if not self._throttle.check_throttle(len(tool_summary), force=False, min_interval=2.0, min_new_chars=10):
                return

            _, spec_project, state, max_c = self._get_engine_and_state()
            base_content = f"{phase.emoji} **{phase.display_name}** 执行中..."

            if tool_summary:
                base_content += f"\n---\n{tool_summary}"

            self._last_phase_content = base_content
            self._rotator.dispatch(CardEvent.text_delta("_phase", base_content))
            self._throttle.update_stream_state(len(tool_summary))

    def _dispatch_build_event(self, event) -> None:
        """Dispatch Build-phase ACP events as native tool/plan CardEvents."""
        card_evt = card_event_from_acp(event)

        # Track tool/file counts for footer progress
        if event.event_type == ACPEventType.TOOL_CALL_DONE:
            self._build_tool_count += 1
            # Extract file paths from tool calls for file count
            tc = event.tool_call
            if tc and tc.title:
                # Common tool names that reference files
                title_lower = tc.title.lower() if tc.title else ""
                if any(kw in title_lower for kw in ("write", "edit", "create", "read")):
                    # Try to extract path from content
                    content = tc.content or ""
                    if content and "/" in content:
                        # Rough heuristic: first line might be a path
                        first_line = content.split("\n")[0].strip()
                        if "/" in first_line and len(first_line) < 200:
                            self._build_file_set.add(first_line)
            # Update footer with progress
            progress_text = f"🔨 {self._build_tool_count} 次工具调用"
            if self._build_file_set:
                progress_text += f" · {len(self._build_file_set)} 文件"
            self._rotator.dispatch(CardEvent.progress_updated(
                current=self._build_tool_count, total=0, label=progress_text
            ))

        # Forward the converted event to the card session
        self._rotator.dispatch(card_evt)

    def on_phase_done(self, cycle_num: int, phase: SpecPhase, output: str) -> None:
        _, spec_project, state, max_c = self._get_engine_and_state()
        self._footer_status = None

        self._rotator.dispatch(CardEvent.phase_done(
            cycle_num, phase.value if hasattr(phase, 'value') else str(phase), output
        ))

        if phase == SpecPhase.BUILD:
            # Build phase: tool panels already rendered, just show summary
            summary = self._reporter._extract_phase_summary(phase, output)
            done_text = f"🔨 **构建完成**  {summary}" if summary else "🔨 **构建完成**"
            self._rotator.dispatch(CardEvent.text_delta("_phase", done_text))
        else:
            # Non-Build phases: text-based summary
            content = self._reporter.format_phase_done_content(cycle_num, phase, max_c, output)
            tool_summary = self._acp_renderer.render_summary()
            if tool_summary:
                content += f"\n---\n{tool_summary}"
            self._last_phase_content = content
            self._rotator.dispatch(CardEvent.text_delta("_phase", content))

    def on_phase_retry(self, attempt: int, max_attempts: int, detail: str) -> None:
        """Push phase-level retry status."""
        retry_text = UI_TEXT["phase_retry_progress"].format(attempt=attempt, max_attempts=max_attempts)
        if detail:
            retry_text += f" — {detail[:80]}"
        self._footer_status = retry_text

        self._rotator.dispatch(CardEvent.review_retry(
            cycle_num=0,
            attempt=attempt,
            max_attempts=max_attempts,
            status="executing",
        ))

    def on_review_retry(self, cycle: int, event: RetryEvent) -> None:
        """Push review-level retry status."""
        if event.status == RetryStatus.SUCCEEDED:
            return

        text_key = _RETRY_STATUS_TEXT.get(event.status, "retry_executing")
        if event.status == RetryStatus.WAITING:
            detail_msg = UI_TEXT[text_key].format(sec=int(event.delay_sec), i=event.attempt, n=event.max_attempts)
        elif event.status == RetryStatus.EXECUTING:
            detail_msg = UI_TEXT[text_key].format(i=event.attempt, n=event.max_attempts)
        elif event.status == RetryStatus.EXHAUSTED:
            detail_msg = UI_TEXT[text_key].format(n=event.max_attempts)
        elif event.status == RetryStatus.NO_RETRY:
            if event.max_attempts == 0:
                detail_msg = UI_TEXT["retry_no_retry_disabled"]
            else:
                detail_msg = UI_TEXT["retry_no_retry_budget"]
        else:
            detail_msg = UI_TEXT[text_key]
        self._footer_status = detail_msg

        self._rotator.dispatch(CardEvent.review_retry(
            cycle_num=cycle,
            attempt=event.attempt,
            max_attempts=event.max_attempts,
            status=event.status.value if hasattr(event.status, 'value') else str(event.status),
        ))

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    def build_callbacks(self) -> SpecEngineCallbacks:
        """Assemble a SpecEngineCallbacks bound to this processor's methods."""
        return SpecEngineCallbacks(
            on_analyzing_done=self.on_analyzing_done,
            on_cycle_start=self.on_cycle_start,
            on_phase_start=self.on_phase_start,
            on_phase_event=self.on_phase_event,
            on_phase_done=self.on_phase_done,
            on_cycle_done=self.on_cycle_done,
            on_review_done=self.on_review_done,
            on_project_done=self.on_project_done,
            on_error=self.on_error,
            on_phase_retry=self.on_phase_retry,
            on_review_retry=self.on_review_retry,
        )
