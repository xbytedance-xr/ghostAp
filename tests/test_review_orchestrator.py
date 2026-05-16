"""Unit tests for ReviewOrchestrator class."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from src.engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from src.spec_engine.review import ReviewOrchestrator
from src.spec_engine.review_types import ReviewCircuitState


class TestReviewOrchestratorInit:
    def test_default_state(self):
        orch = ReviewOrchestrator()
        assert isinstance(orch.circuit, ReviewCircuitState)
        assert isinstance(orch.cancel_event, threading.Event)
        assert not orch.cancel_event.is_set()

    def test_circuit_setter_removed(self):
        """AC-R04: circuit property no longer has a setter — use restore_circuit instead."""
        orch = ReviewOrchestrator()
        new_circuit = ReviewCircuitState(review_failure_consecutive=3)
        with pytest.raises(AttributeError):
            orch.circuit = new_circuit  # type: ignore[misc]
        # Use restore_circuit instead
        orch.restore_circuit(new_circuit)
        assert orch.circuit.review_failure_consecutive == 3


class TestSignalStop:
    def test_signal_stop_sets_event(self):
        orch = ReviewOrchestrator()
        assert not orch.cancel_event.is_set()
        orch.signal_stop()
        assert orch.cancel_event.is_set()

    def test_signal_stop_idempotent(self):
        orch = ReviewOrchestrator()
        orch.signal_stop()
        orch.signal_stop()
        assert orch.cancel_event.is_set()


class TestResetCancelEvent:
    def test_reset_when_running(self):
        orch = ReviewOrchestrator()
        orch.signal_stop()  # set it first
        result = orch.reset_cancel_event(is_running=True)
        assert result is True
        assert not orch.cancel_event.is_set()

    def test_reset_when_stopping(self):
        orch = ReviewOrchestrator()
        result = orch.reset_cancel_event(is_running=False)
        assert result is False
        assert orch.cancel_event.is_set()

    def test_reset_when_idle(self):
        orch = ReviewOrchestrator()
        result = orch.reset_cancel_event(is_running=False)
        assert result is False
        assert orch.cancel_event.is_set()


class TestSerialization:
    def test_to_dict_default(self):
        orch = ReviewOrchestrator()
        d = orch.to_dict()
        assert d["review_failure_consecutive"] == 0
        assert d["review_circuit_open_until_cycle"] == 0
        assert d["backoff_level"] == 0

    def test_to_dict_with_state(self):
        orch = ReviewOrchestrator()
        orch.circuit.review_failure_consecutive = 5
        orch.circuit.backoff_level = 2
        d = orch.to_dict()
        assert d["review_failure_consecutive"] == 5
        assert d["backoff_level"] == 2

    def test_restore_circuit(self):
        orch = ReviewOrchestrator()
        orch.restore_circuit({
            "review_failure_consecutive": 7,
            "review_circuit_open_until_cycle": 10,
            "backoff_level": 3,
            "consecutive_timeouts": 2,
            "consecutive_skips": 1,
            "last_review_elapsed_ms": 5000,
            "recent_outcomes": ["success", "partial_failure"],
        })
        assert orch.circuit.review_failure_consecutive == 7
        assert orch.circuit.review_circuit_open_until_cycle == 10
        assert orch.circuit.backoff_level == 3
        assert orch.circuit.consecutive_timeouts == 2

    def test_restore_circuit_empty_dict(self):
        orch = ReviewOrchestrator()
        orch.circuit.review_failure_consecutive = 99
        orch.restore_circuit({})
        assert orch.circuit.review_failure_consecutive == 0

    def test_from_dict(self):
        data = {
            "review_failure_consecutive": 4,
            "review_circuit_open_until_cycle": 8,
            "backoff_level": 1,
            "consecutive_timeouts": 0,
            "consecutive_skips": 0,
            "last_review_elapsed_ms": 0,
            "recent_outcomes": [],
        }
        orch = ReviewOrchestrator.from_dict(data)
        assert orch.circuit.review_failure_consecutive == 4
        assert orch.circuit.review_circuit_open_until_cycle == 8
        assert isinstance(orch.cancel_event, threading.Event)

    def test_roundtrip(self):
        orch = ReviewOrchestrator()
        orch.circuit.review_failure_consecutive = 3
        orch.circuit.backoff_level = 2
        orch.circuit.recent_outcomes = ["success"]
        d = orch.to_dict()
        orch2 = ReviewOrchestrator.from_dict(d)
        assert orch2.circuit.review_failure_consecutive == 3
        assert orch2.circuit.backoff_level == 2
        assert orch2.circuit.recent_outcomes == ["success"]


class TestConductReview:
    """Test that conduct_review delegates to the module-level function."""

    def test_conduct_review_delegates(self):
        orch = ReviewOrchestrator()

        expected_result = ReviewResult(
            reviews=[
                PerspectiveReview(
                    perspective=ReviewPerspective.ARCHITECT,
                    passed=True,
                    suggestions=[],
                    summary="OK",
                )
            ],
            iteration=1,
        )

        with patch("src.spec_engine.review.conduct_review", return_value=expected_result) as mock_fn:
            result = orch.conduct_review(
                session=MagicMock(),
                settings=MagicMock(),
                project=MagicMock(),
                send_prompt_with_retry_fn=MagicMock(),
                build_review_exception_diagnostics_fn=MagicMock(),
                cycle=1,
            )
            assert result is expected_result
            mock_fn.assert_called_once()
            # Verify circuit and cancel_event are passed correctly
            call_kwargs = mock_fn.call_args.kwargs
            assert call_kwargs["circuit"] is orch.circuit
            assert call_kwargs["cancel_event"] is orch.cancel_event

    def test_conduct_review_passes_all_params(self):
        orch = ReviewOrchestrator()

        with patch("src.spec_engine.review.conduct_review", return_value=ReviewResult(iteration=2)) as mock_fn:
            mock_session = MagicMock()
            mock_settings = MagicMock()
            mock_project = MagicMock()
            mock_send = MagicMock()
            mock_diag = MagicMock()
            mock_done = MagicMock()
            mock_artifacts = MagicMock()
            mock_retry = MagicMock()

            orch.conduct_review(
                session=mock_session,
                settings=mock_settings,
                project=mock_project,
                send_prompt_with_retry_fn=mock_send,
                build_review_exception_diagnostics_fn=mock_diag,
                cycle=2,
                on_review_done=mock_done,
                artifacts=mock_artifacts,
                agent_type="claude",
                model_name="claude-3.5-sonnet",
                on_retry_status=mock_retry,
            )

            call_kwargs = mock_fn.call_args.kwargs
            assert call_kwargs["session"] is mock_session
            assert call_kwargs["settings"] is mock_settings
            assert call_kwargs["project"] is mock_project
            assert call_kwargs["send_prompt_with_retry_fn"] is mock_send
            assert call_kwargs["build_review_exception_diagnostics_fn"] is mock_diag
            assert call_kwargs["cycle"] == 2
            assert call_kwargs["on_review_done"] is mock_done
            assert call_kwargs["artifacts"] is mock_artifacts
            assert call_kwargs["agent_type"] == "claude"
            assert call_kwargs["model_name"] == "claude-3.5-sonnet"
            assert call_kwargs["on_retry_status"] is mock_retry

    def test_circuit_breaker_skip_via_orchestrator(self):
        """When circuit is open, conduct_review should skip (return circuit-breaker result)."""
        orch = ReviewOrchestrator()
        orch.circuit.review_failure_consecutive = 5
        orch.circuit.review_circuit_open_until_cycle = 10

        settings = MagicMock()
        settings.spec_review_failure_circuit_enabled = True
        settings.spec_review_failure_max_consecutive = 3
        settings.spec_review_failure_cooldown_cycles = 2
        settings.review_circuit_lint_fallback_enabled = False

        result = orch.conduct_review(
            session=MagicMock(),
            settings=settings,
            project=MagicMock(),
            send_prompt_with_retry_fn=MagicMock(),
            build_review_exception_diagnostics_fn=MagicMock(),
            cycle=5,  # cycle 5 <= open_until 10
        )

        # Should have gotten a circuit-breaker skip result
        assert not result.all_passed
        assert any("审查暂停" in r.summary for r in result.reviews)


class TestOnRetryStatusClosureInvoked:
    """AC-R13: on_retry_status callback is actually invoked during timeout scenarios."""

    def test_on_retry_status_receives_no_retry_when_disabled(self):
        """When retry is disabled (max_attempts=0) and a timeout occurs,
        on_retry_status should receive NO_RETRY through the full orchestrator path."""
        from src.spec_engine.perspective_worker import PerspectiveOutcome, ReviewErrorCode
        from src.spec_engine.retry_status import RetryStatus

        orch = ReviewOrchestrator()

        # Build settings that disable retry
        settings = MagicMock()
        settings.spec_review_failure_circuit_enabled = False
        settings.spec_review_failure_max_consecutive = 4
        settings.spec_review_failure_cooldown_cycles = 2
        settings.spec_review_max_parallel = 2
        settings.spec_review_retry_max_attempts = 0
        settings.spec_review_retry_max_delay = 1
        settings.spec_review_timeout = 30
        settings.spec_review_min_timeout = 10
        settings.spec_review_hard_floor = 5
        settings.review_circuit_lint_fallback_enabled = False

        # Mock artifacts to trigger pipeline path
        mock_artifacts = MagicMock()
        mock_artifacts.bindings = [MagicMock()]

        # Make pipeline return timeout outcomes
        timeout_outcome = PerspectiveOutcome(
            perspective=ReviewPerspective.ARCHITECT,
            review=PerspectiveReview(
                perspective=ReviewPerspective.ARCHITECT,
                passed=False, suggestions=["timeout"], summary="timeout"
            ),
            error="timeout",
            error_code=ReviewErrorCode.TIMEOUT,
        )

        status_log = []

        def on_retry_status(event):
            status_log.append((event.status, event.detail))

        # Patch run_review_pipeline to return error outcomes (triggers handle_pipeline_errors_with_retry)
        with patch("src.spec_engine.review_pipeline.run_review_pipeline") as mock_pipeline:
            mock_pipeline.return_value = [timeout_outcome]

            orch.conduct_review(
                session=MagicMock(),
                settings=settings,
                project=MagicMock(),
                send_prompt_with_retry_fn=MagicMock(),
                build_review_exception_diagnostics_fn=MagicMock(),
                cycle=1,
                artifacts=mock_artifacts,
                agent_type="coco",
                model_name=None,
                on_retry_status=on_retry_status,
            )

        # The callback should have been invoked with NO_RETRY
        emitted_statuses = [s for s, _ in status_log]
        assert RetryStatus.NO_RETRY in emitted_statuses, (
            f"Expected NO_RETRY in emitted statuses: {status_log}"
        )
