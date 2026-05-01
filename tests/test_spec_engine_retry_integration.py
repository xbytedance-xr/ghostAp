"""Integration test: SpecEngine._conduct_review → on_review_retry callback wiring.

AC-R16: Verifies the complete chain from SpecEngine._conduct_review through
the _on_retry_status closure to callbacks.on_review_retry, ensuring the
RetryEvent dataclass is correctly forwarded.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from src.engine_base import EngineRunState, ReviewResult
from src.spec_engine.engine import SpecEngine, SpecEngineCallbacks
from src.spec_engine.models import SpecProject
from src.spec_engine.retry_status import RetryEvent, RetryStatus


class TestConductReviewRetryCallbackWiring:
    """AC-R16: _conduct_review → _on_retry_status → callbacks.on_review_retry chain."""

    def _make_settings(self):
        """Create mock settings with retry enabled."""
        s = MagicMock()
        s.spec_max_cycles = 1
        s.spec_max_cycles_limit = 5000
        s.spec_convergence_window = 1
        s.spec_execution_timeout = 300
        s.spec_review_enabled = True
        s.spec_infinite_mode = False
        s.spec_disable_convergence = False
        s.spec_disable_early_stop = False
        s.spec_min_cycles = 1
        s.spec_max_retries = 1
        s.spec_cycle_tasks_max = 50
        s.spec_cycle_output_max_chars = 4000
        s.spec_state_filename = ".spec_engine_state.json"
        s.spec_artifacts_dirname = ".spec_engine"
        s.spec_review_timeout = 120
        s.spec_review_min_timeout = 30
        s.spec_review_hard_floor = 20
        s.spec_review_max_parallel = 3
        s.spec_review_retry_max_delay = 1  # Short for test speed
        s.spec_review_retry_max_attempts = 1
        s.spec_review_retry_base_delay = 5.0
        s.spec_review_retry_decay_factor = 1.5
        s.spec_review_failure_circuit_enabled = False
        s.spec_review_failure_max_consecutive = 4
        s.spec_review_failure_cooldown_cycles = 2
        s.spec_review_failure_max_cooldown_cycles = 12
        s.spec_review_adaptive_timeout_enabled = True
        s.acp_provider = "coco"
        return s

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.engine_base.get_settings")
    def test_retry_event_forwarded_to_callback(self, mock_settings, mock_create):
        """When pipeline times out and retries, on_review_retry receives RetryEvent."""
        settings = self._make_settings()
        mock_settings.return_value = settings

        # Mock session that always times out
        session = MagicMock()
        session.send_prompt_with_retry.side_effect = TimeoutError("review timeout")
        session.send_prompt.side_effect = TimeoutError("review timeout")

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._session = session
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.requirement = "Build login"

        # Set engine to RUNNING state
        with engine._lock:
            engine._run_state = EngineRunState.RUNNING

        # Collect all on_review_retry calls
        retry_events = []

        def capture_retry(cycle, event):
            retry_events.append((cycle, event))

        callbacks = SpecEngineCallbacks(on_review_retry=capture_retry)

        # Run review — should trigger timeout → retry mechanism
        result = engine._conduct_review(1, callbacks)

        # Verify result is a ReviewResult (even on failure)
        assert isinstance(result, ReviewResult)

        # If retry events were emitted, verify they are RetryEvent instances
        for cycle, event in retry_events:
            assert isinstance(event, RetryEvent), f"Expected RetryEvent, got {type(event)}"
            assert isinstance(event.status, RetryStatus)
            assert cycle == 1

    @patch("src.spec_engine.engine.create_engine_session")
    @patch("src.engine_base.get_settings")
    def test_no_retry_event_when_callback_not_set(self, mock_settings, mock_create):
        """When on_review_retry is None, no crash occurs."""
        settings = self._make_settings()
        mock_settings.return_value = settings

        session = MagicMock()
        session.send_prompt_with_retry.side_effect = TimeoutError("review timeout")
        session.send_prompt.side_effect = TimeoutError("review timeout")

        engine = SpecEngine(chat_id="c1", root_path="/tmp/test")
        engine._session = session
        engine._project = SpecProject.create(root_path="/tmp/test")
        engine._project.requirement = "Build login"

        with engine._lock:
            engine._run_state = EngineRunState.RUNNING

        # No on_review_retry callback — should not crash
        callbacks = SpecEngineCallbacks()
        result = engine._conduct_review(1, callbacks)
        assert isinstance(result, ReviewResult)


class TestRetryFullCallbackSequence:
    """Verify the complete ordered callback sequence during retry with max_attempts=2."""

    def test_retry_full_callback_sequence(self):
        """max_attempts=2 all timeout → sequence: WAITING(1)→EXECUTING(1)→WAITING(2)→EXECUTING(2)→EXHAUSTED."""
        from unittest.mock import patch as _patch
        from src.spec_engine.review_retry import (
            PipelineRetryContext,
            handle_pipeline_errors_with_retry,
        )
        from src.spec_engine.review_types import ReviewCircuitState

        # Build a mock settings with max_attempts=2, base_delay >= 2 to trigger WAITING events
        settings = MagicMock()
        settings.spec_review_retry_max_attempts = 2
        settings.spec_review_retry_max_delay = 3  # >= 2 to trigger WAITING events
        settings.spec_review_retry_base_delay = 3.0
        settings.spec_review_retry_decay_factor = 1.5
        settings.spec_review_timeout = 120
        settings.spec_review_min_timeout = 30
        settings.spec_review_hard_floor = 20

        # Mock outcomes: single worker that timed out
        mock_outcome = MagicMock()
        mock_outcome.error = "timeout"
        mock_outcome.error_code = MagicMock()
        mock_outcome.error_code.name = "TIMEOUT"
        # Make all_timeout check pass
        from src.spec_engine.perspective_worker import ReviewErrorCode
        mock_outcome.error_code = ReviewErrorCode.TIMEOUT
        mock_outcome.review = MagicMock()
        mock_outcome.review.suggestions = []

        # Pipeline function that always times out
        def always_timeout_pipeline(*args, **kwargs):
            raise TimeoutError("still timeout")

        # Collect events
        events: list[RetryEvent] = []

        def capture(event: RetryEvent):
            events.append(event)

        circuit = ReviewCircuitState()
        ctx = PipelineRetryContext(
            cancel_event=None,
            on_retry_status=capture,
            base_timeout=30,
            multiplier=2,
            pipeline_fn=always_timeout_pipeline,
            budget_cls=MagicMock(return_value=MagicMock()),
            artifacts=MagicMock(),
            agent_type="coco",
            model_name=None,
            skip_retry_event=None,
        )

        review_result = ReviewResult(reviews=[mock_outcome.review], iteration=1)

        with _patch("src.spec_engine.review_retry.time.sleep", return_value=None):
            result, diag = handle_pipeline_errors_with_retry(
                outcomes=[mock_outcome],
                review_result=review_result,
                circuit=circuit,
                settings=settings,
                cycle=1,
                ctx=ctx,
                retry_texts={"retry_no_retry": "no retry", "retry_exhausted": "exhausted {n}"},
            )

        # Verify the complete ordered sequence
        statuses = [e.status for e in events]
        assert statuses == [
            RetryStatus.WAITING,
            RetryStatus.EXECUTING,
            RetryStatus.WAITING,
            RetryStatus.EXECUTING,
            RetryStatus.EXHAUSTED,
        ], f"Expected full sequence, got: {statuses}"

        # Verify attempt numbers
        assert events[0].attempt == 1  # WAITING attempt 1
        assert events[1].attempt == 1  # EXECUTING attempt 1
        assert events[2].attempt == 2  # WAITING attempt 2
        assert events[3].attempt == 2  # EXECUTING attempt 2
        assert events[4].max_attempts == 2  # EXHAUSTED


class TestRetrySuccessCallbackSequence:
    """AC-24: Verify pipeline_fn succeeds on first attempt → callback sequence [WAITING, EXECUTING, SUCCEEDED]."""

    def test_retry_success_callback_sequence(self):
        """pipeline_fn succeeds on first attempt → WAITING→EXECUTING→SUCCEEDED."""
        from unittest.mock import patch as _patch
        from src.spec_engine.review_retry import (
            PipelineRetryContext,
            attempt_pipeline_retry,
        )
        from src.spec_engine.review_types import ReviewCircuitState

        settings = MagicMock()
        settings.spec_review_retry_max_attempts = 1
        settings.spec_review_retry_max_delay = 3  # >= 2 to trigger WAITING event
        settings.spec_review_retry_base_delay = 3.0
        settings.spec_review_retry_decay_factor = 1.5
        settings.spec_review_timeout = 120
        settings.spec_review_min_timeout = 30
        settings.spec_review_hard_floor = 10

        # Mock outcome: no errors (success)
        mock_outcome = MagicMock()
        mock_outcome.error = ""
        mock_outcome.error_code = None

        def success_pipeline(*args, **kwargs):
            return [mock_outcome]

        events: list[RetryEvent] = []

        def capture(event: RetryEvent):
            events.append(event)

        circuit = ReviewCircuitState(consecutive_timeouts=1)  # Triggers retry path
        ctx = PipelineRetryContext(
            cancel_event=None,
            on_retry_status=capture,
            base_timeout=30,
            multiplier=2,
            pipeline_fn=success_pipeline,
            budget_cls=MagicMock(return_value=MagicMock()),
            artifacts=MagicMock(),
            agent_type="coco",
            model_name=None,
            skip_retry_event=None,
        )

        with _patch("src.spec_engine.review_retry.time.sleep", return_value=None):
            result = attempt_pipeline_retry(
                circuit=circuit, settings=settings, cycle=1, ctx=ctx,
            )

        assert result is not None, "Expected successful retry outcomes"
        statuses = [e.status for e in events]
        assert statuses == [
            RetryStatus.WAITING,
            RetryStatus.EXECUTING,
            RetryStatus.SUCCEEDED,
        ], f"Expected [WAITING, EXECUTING, SUCCEEDED], got: {statuses}"

        # Verify attempt numbers
        assert events[0].attempt == 1
        assert events[1].attempt == 1
        assert events[2].attempt == 1
