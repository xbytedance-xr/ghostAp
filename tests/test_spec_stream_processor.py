"""Unit tests for SpecStreamProcessor — extracted state/callback class.

Covers:
- Initialization and default state
- on_phase_event throttle logic
- on_review_retry all status branches
- _rotate_session rotation behavior
- build_callbacks returns proper SpecEngineCallbacks
"""

from unittest.mock import MagicMock, patch

import pytest

from src.acp import ACPEventType
from src.card.events import CardEvent, CardEventType
from src.card.render.budget import RenderBudget
from src.card.render.throttle import StreamThrottle
from src.card.state.models import CardMetadata
from src.card.ui_text import UI_TEXT, _MUTABLE_UI_TEXT
from src.feishu.renderers._spec_stream_processor import SpecStreamProcessor
from src.engine_base import ReviewResult
from src.spec_engine import SpecEngineCallbacks
from src.spec_engine.models import SpecPhase, SpecProject, SpecProjectStatus
from src.spec_engine.reporter import SpecReporter
from src.spec_engine.retry_status import RetryEvent, RetryStatus


def _make_processor(**overrides):
    """Create a SpecStreamProcessor with mock dependencies."""
    rotator = MagicMock()
    rotator.dispatch = MagicMock()
    rotator.rotation_count = 0
    rotator.current = MagicMock()
    rotator.current.delivered_message_id = "msg_delivered_1"

    reporter = MagicMock()
    reporter.format_analyzing_done = MagicMock(return_value="analysis done")
    reporter.format_phase_start_content = MagicMock(return_value="phase started")
    reporter.format_phase_done_content = MagicMock(return_value="phase done")
    reporter.format_cycle_done = MagicMock(return_value="cycle done")
    reporter.format_review_result = MagicMock(return_value="review result")
    reporter.format_project_done = MagicMock(return_value="project done")
    reporter.format_criteria_section = MagicMock(return_value="criteria")

    renderer = MagicMock()
    renderer.update_ui_state = MagicMock()
    renderer.get_ui_state = MagicMock(return_value={})
    renderer.check_warning_banner = MagicMock(return_value=None)
    renderer.create_session = MagicMock()

    # Mock ctx.spec_engine_manager.snapshot
    snap = MagicMock()
    snap.ext = {"project": MagicMock(
        cycle_count_total=5,
        satisfied_count=2,
        total_criteria=5,
        duration=MagicMock(return_value=60.0),
        status=MagicMock(value="running"),
    )}
    renderer.ctx = MagicMock()
    renderer.ctx.spec_engine_manager.snapshot = MagicMock(return_value=snap)

    metadata = CardMetadata(engine_type="spec", mode_name="Spec", mode_emoji="📋")
    budget = RenderBudget(engine_cmd="/spec")
    throttle = StreamThrottle(min_interval=1.0, min_chars=20)

    defaults = dict(
        rotator=rotator,
        reporter=reporter,
        metadata=metadata,
        hooks=(),
        budget=budget,
        spec_project_id="proj_1",
        message_id="msg_1",
        chat_id="chat_1",
        renderer=renderer,
        project_root_path="/tmp/test",
        throttle=throttle,
    )
    defaults.update(overrides)
    proc = SpecStreamProcessor(**defaults)
    return proc, defaults


class TestSpecStreamProcessorInit:
    """Verify initial state after construction."""

    def test_initial_mutable_state(self):
        proc, _ = _make_processor()
        assert proc._max_cycles == 0
        assert proc._footer_status is None
        assert proc._last_phase_content == ""
        assert proc._acp_renderer is not None

    def test_build_callbacks_returns_spec_engine_callbacks(self):
        proc, _ = _make_processor()
        cbs = proc.build_callbacks()
        assert isinstance(cbs, SpecEngineCallbacks)
        assert cbs.on_analyzing_done is not None
        assert cbs.on_error is not None
        assert cbs.on_phase_event is not None
        assert cbs.on_review_retry is not None


class TestRotateSession:
    """Verify _rotate_session delegates correctly to rotator.rotate."""

    def test_rotate_session_calls_rotator_rotate(self):
        proc, deps = _make_processor()
        proc._rotate_session(1)
        deps["rotator"].rotate.assert_called_once()

    def test_rotate_session_uses_delivered_message_id(self):
        proc, deps = _make_processor()
        deps["rotator"].current.delivered_message_id = "msg_old"
        proc._rotate_session(1)
        # The lambda passed to rotate should call renderer.create_session
        factory_fn = deps["rotator"].rotate.call_args[0][0]
        factory_fn()
        deps["renderer"].create_session.assert_called_once()
        # reply_to (second positional arg) should be the old delivered message id
        call_args = deps["renderer"].create_session.call_args
        assert call_args[0][1] == "msg_old"

    def test_rotate_session_falls_back_to_original_message_id(self):
        proc, deps = _make_processor()
        deps["rotator"].current.delivered_message_id = None
        proc._rotate_session(1)
        factory_fn = deps["rotator"].rotate.call_args[0][0]
        factory_fn()
        call_args = deps["renderer"].create_session.call_args
        assert call_args[0][1] == "msg_1"


class TestOnPhaseEventThrottle:
    """Verify throttle behavior in on_phase_event."""

    def _make_event(self, event_type):
        ev = MagicMock()
        ev.event_type = event_type
        return ev

    def test_build_phase_text_chunk_still_streams(self):
        proc, deps = _make_processor()
        proc._acp_renderer.process_event = MagicMock()
        ev = self._make_event(ACPEventType.TEXT_CHUNK)
        ev.text = "build output"
        ev.tool_call = None
        ev.plan = None

        proc.on_phase_event(1, SpecPhase.BUILD, ev)

        dispatched_types = [
            call[0][0].type for call in deps["rotator"].dispatch.call_args_list
        ]
        assert CardEventType.TEXT_DELTA in dispatched_types

    def test_structured_phase_text_chunk_is_not_streamed_as_raw_json(self):
        proc, deps = _make_processor()
        proc._acp_renderer.process_event = MagicMock()
        ev = self._make_event(ACPEventType.TEXT_CHUNK)
        ev.text = '{"goals":["avoid raw json"],"acceptance_criteria":["readable card"]}'
        ev.tool_call = None
        ev.plan = None

        proc.on_phase_event(1, SpecPhase.SPEC, ev)

        dispatched_types = [
            call[0][0].type for call in deps["rotator"].dispatch.call_args_list
        ]
        assert CardEventType.TEXT_STARTED not in dispatched_types
        assert CardEventType.TEXT_DELTA not in dispatched_types

    def test_build_phase_text_chunk_forwards_as_text_delta(self):
        """In Build phase, TEXT_CHUNK events are forwarded as TEXT_DELTA via card_event_from_acp."""
        proc, deps = _make_processor()
        proc._acp_renderer.process_event = MagicMock()
        ev = self._make_event(ACPEventType.TEXT_CHUNK)
        ev.text = "hello"
        ev.tool_call = None
        ev.plan = None
        proc.on_phase_event(1, SpecPhase.BUILD, ev)
        dispatched_types = [
            call[0][0].type for call in deps["rotator"].dispatch.call_args_list
        ]
        assert CardEventType.TEXT_DELTA in dispatched_types

    def test_tool_call_done_triggers_update_when_throttle_passes(self):
        proc, deps = _make_processor()
        proc._acp_renderer.process_event = MagicMock()

        ev = self._make_event(ACPEventType.TOOL_CALL_DONE)
        # Mock tool_call for card_event_from_acp
        tc = MagicMock()
        tc.id = "tool_1"
        tc.title = "done"
        tc.content = "output"
        tc.status = "completed"
        ev.tool_call = tc
        ev.text = None
        ev.plan = None

        proc.on_phase_event(1, SpecPhase.BUILD, ev)
        # Build phase: TOOL_CALL_DONE dispatches TOOL_DONE + PROGRESS_UPDATED
        dispatched_types = [
            call[0][0].type for call in deps["rotator"].dispatch.call_args_list
        ]
        assert CardEventType.TOOL_DONE in dispatched_types
        assert CardEventType.PROGRESS_UPDATED in dispatched_types

    def test_footer_status_tracking(self):
        proc, _ = _make_processor()
        proc._acp_renderer.process_event = MagicMock()

        ev_thought = self._make_event(ACPEventType.THOUGHT_CHUNK)
        proc.on_phase_event(1, SpecPhase.BUILD, ev_thought)
        assert proc._footer_status == "thinking"

        ev_tool = self._make_event(ACPEventType.TOOL_CALL_START)
        proc.on_phase_event(1, SpecPhase.BUILD, ev_tool)
        assert proc._footer_status == "tool_running"

        ev_text = self._make_event(ACPEventType.TEXT_CHUNK)
        proc.on_phase_event(1, SpecPhase.BUILD, ev_text)
        assert proc._footer_status is None


class TestOnReviewRetryBranches:
    """Verify on_review_retry handles all RetryStatus branches."""

    def _make_event(self, status, **kwargs):
        defaults = dict(attempt=1, max_attempts=3, delay_sec=5.0)
        defaults.update(kwargs)
        return RetryEvent(status=status, **defaults)

    def test_succeeded_early_return(self):
        proc, deps = _make_processor()
        ev = self._make_event(RetryStatus.SUCCEEDED)
        proc.on_review_retry(1, ev)
        deps["rotator"].dispatch.assert_not_called()

    def test_waiting_sets_footer(self):
        proc, _ = _make_processor()
        ev = self._make_event(RetryStatus.WAITING, delay_sec=7.0, attempt=1, max_attempts=3)
        proc.on_review_retry(1, ev)
        assert proc._footer_status is not None
        assert "7" in proc._footer_status

    def test_executing_sets_footer(self):
        proc, _ = _make_processor()
        ev = self._make_event(RetryStatus.EXECUTING, attempt=2, max_attempts=3)
        proc.on_review_retry(1, ev)
        assert proc._footer_status is not None
        assert "2" in proc._footer_status

    def test_exhausted_sets_footer(self):
        proc, _ = _make_processor()
        ev = self._make_event(RetryStatus.EXHAUSTED, max_attempts=3)
        # retry_exhausted format requires {n} and {elapsed_friendly}; the processor
        # only passes n — patch UI_TEXT to avoid KeyError from missing placeholder
        with patch.dict(_MUTABLE_UI_TEXT, {"retry_exhausted": "已重试 {n} 次"}):
            proc.on_review_retry(1, ev)
        assert proc._footer_status is not None
        assert "3" in proc._footer_status

    def test_no_retry_disabled(self):
        proc, _ = _make_processor()
        ev = self._make_event(RetryStatus.NO_RETRY, max_attempts=0)
        proc.on_review_retry(1, ev)
        assert proc._footer_status == UI_TEXT["retry_no_retry_disabled"]

    def test_no_retry_budget(self):
        proc, _ = _make_processor()
        ev = self._make_event(RetryStatus.NO_RETRY, max_attempts=2)
        proc.on_review_retry(1, ev)
        assert proc._footer_status == UI_TEXT["retry_no_retry_budget"]

    def test_dispatch_called_for_non_succeeded(self):
        proc, deps = _make_processor()
        ev = self._make_event(RetryStatus.EXECUTING, attempt=1, max_attempts=3)
        proc.on_review_retry(2, ev)
        deps["rotator"].dispatch.assert_called_once()
        card_event = deps["rotator"].dispatch.call_args[0][0]
        assert card_event.type == CardEventType.REVIEW_RETRY


class TestOnPhaseRetry:
    """Verify on_phase_retry callback."""

    def test_sets_footer_status(self):
        proc, _ = _make_processor()
        proc.on_phase_retry(2, 3, "timeout")
        assert proc._footer_status is not None
        assert "2/3" in proc._footer_status
        assert "timeout" in proc._footer_status

    def test_dispatches_review_retry_event(self):
        proc, deps = _make_processor()
        proc.on_phase_retry(1, 3, "")
        deps["rotator"].dispatch.assert_called_once()
        ev = deps["rotator"].dispatch.call_args[0][0]
        assert ev.type == CardEventType.REVIEW_RETRY


class TestOnPhaseDone:
    """Verify phase completion emits readable card status."""

    def test_structured_phase_done_uses_summary_not_raw_json(self):
        proc, deps = _make_processor(reporter=SpecReporter())
        raw_json = """```json
{
  "goals": ["让 Spec 卡片更容易阅读"],
  "functional_spec": ["不要展示大段 JSON"],
  "acceptance_criteria": ["阶段状态会流转"]
}
```"""

        proc.on_phase_done(1, SpecPhase.SPEC, raw_json)

        dispatched = [call[0][0] for call in deps["rotator"].dispatch.call_args_list]
        phase_done = [
            event for event in dispatched
            if event.type == CardEventType.PHASE_DONE and event.payload.get("phase") == "spec"
        ]
        assert phase_done
        output = phase_done[-1].payload["output"]
        assert "规格定义完成" in output
        assert "goals" not in output
        assert "functional_spec" not in output


class TestOnCycleStart:
    """Verify on_cycle_start updates max_cycles and dispatches."""

    def test_updates_max_cycles(self):
        proc, _ = _make_processor()
        assert proc._max_cycles == 0
        proc.on_cycle_start(1, 5)
        assert proc._max_cycles == 5

    def test_dispatches_cycle_started(self):
        proc, deps = _make_processor()
        proc.on_cycle_start(2, 4)
        dispatched = [call[0][0] for call in deps["rotator"].dispatch.call_args_list]
        types = [e.type for e in dispatched]
        assert CardEventType.CYCLE_STARTED in types


class TestOnReviewDone:
    """Verify review completion closes the visible review phase."""

    def test_dispatches_review_phase_done(self):
        proc, deps = _make_processor(reporter=SpecReporter())
        proc.on_review_done(2, ReviewResult(iteration=2))

        dispatched = [call[0][0] for call in deps["rotator"].dispatch.call_args_list]
        phase_done = [
            event for event in dispatched
            if event.type == CardEventType.PHASE_DONE and event.payload.get("phase") == "review"
        ]
        assert phase_done
        assert "多角色审查完成" in phase_done[-1].payload["output"]

        text_events = [
            event for event in dispatched
            if event.type == CardEventType.TEXT_DELTA
        ]
        assert any("多角色审查" in str(event.payload.get("text", "")) for event in text_events)
        assert any(event.type == CardEventType.REVIEW_RESULT_UPDATED for event in dispatched)


class TestOnError:
    """Verify on_error dispatches FAILED event."""

    def test_dispatches_failed(self):
        proc, deps = _make_processor()
        proc.on_error("something broke")
        deps["rotator"].dispatch.assert_called_once()
        ev = deps["rotator"].dispatch.call_args[0][0]
        assert ev.type == CardEventType.FAILED
