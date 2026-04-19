"""Tests for TimeoutError handling improvements across review diagnostics,
circuit breaker, and convergence detection.

Covers:
- T7:  sync_adapter TimeoutError re-raise carries non-empty message
- T8:  review.py _extract_error_text / _infer_fail_reason friendly output
- T9:  conduct_review returns friendly ReviewResult on timeout
- T10: circuit breaker triggers after consecutive failures and recovers
- T11: convergence detection skips review_failed cycles
"""

import concurrent.futures
import threading
import types
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from src.spec_engine.convergence import detect_convergence
from src.spec_engine.models import (
    CriteriaTracker,
    SpecCycle,
    SpecCycleMetrics,
    SpecProject,
)
from src.spec_engine.review import (
    ReviewCircuitState,
    build_review_exception_diagnostics,
    conduct_review,
    normalize_review_diagnostics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(**overrides):
    """Create a minimal mock settings object with review-related defaults."""
    defaults = {
        "spec_review_timeout": 120,
        "spec_review_failure_circuit_enabled": True,
        "spec_review_failure_max_consecutive": 3,
        "spec_review_failure_cooldown_cycles": 3,
        "spec_review_failure_max_cooldown_cycles": 12,
        "spec_review_min_timeout": 30,
        "spec_review_enabled": True,
        "diagnostics_redact_enabled": False,
        "diagnostics_snippet_limit": 240,
        "diagnostics_total_limit": 2000,
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


def _make_project(*, requirement: str = "test", cycles: list | None = None) -> SpecProject:
    return SpecProject(
        project_id="test-1",
        name="test",
        root_path="/tmp/test",
        requirement=requirement,
        cycles=cycles or [],
        criteria_tracker=CriteriaTracker(criteria=["criterion_a"]),
    )


def _make_review_result(passed: bool, suggestions: list[str] | None = None) -> ReviewResult:
    return ReviewResult(
        reviews=[
            PerspectiveReview(
                perspective=p,
                passed=passed,
                suggestions=suggestions or [],
            )
            for p in ReviewPerspective
        ],
    )


# ===========================================================================
# T7: sync_adapter TimeoutError re-raise carries non-empty message
# ===========================================================================


class TestSyncAdapterTimeoutMessage:
    """Verify that send_prompt wraps bare TimeoutError with a meaningful message."""

    def test_timeout_error_has_message(self):
        """When future.result raises TimeoutError, re-raised error must carry text."""
        from src.acp.sync_adapter import SyncACPSession

        session = SyncACPSession.__new__(SyncACPSession)
        session._acp_session = MagicMock()
        session._loop = MagicMock()
        session._active_future = None
        session.last_active = 0
        session.message_count = 0
        session.last_query = ""
        session._watchdog_timer = None
        session._watchdog_lock = threading.Lock()
        session.cancel = MagicMock()

        # Mock _start_watchdog to be a no-op
        session._start_watchdog = MagicMock()

        # Create a future that times out
        future = concurrent.futures.Future()

        with patch("asyncio.run_coroutine_threadsafe", return_value=future):
            # Simulate TimeoutError from future.result
            with patch.object(future, "result", side_effect=TimeoutError()):
                with pytest.raises(TimeoutError, match="ACP prompt 执行超时"):
                    session.send_prompt("hello", timeout=30)

    def test_timeout_error_includes_timeout_value(self):
        """Re-raised TimeoutError message includes the timeout seconds."""
        from src.acp.sync_adapter import SyncACPSession

        session = SyncACPSession.__new__(SyncACPSession)
        session._acp_session = MagicMock()
        session._loop = MagicMock()
        session._active_future = None
        session.last_active = 0
        session.message_count = 0
        session.last_query = ""
        session._watchdog_timer = None
        session._watchdog_lock = threading.Lock()
        session.cancel = MagicMock()
        session._start_watchdog = MagicMock()

        future = concurrent.futures.Future()

        with patch("asyncio.run_coroutine_threadsafe", return_value=future):
            with patch.object(future, "result", side_effect=TimeoutError()):
                with pytest.raises(TimeoutError) as exc_info:
                    session.send_prompt("hello", timeout=42)
                assert "42" in str(exc_info.value)


# ===========================================================================
# T8: review.py diagnostics — friendly text for TimeoutError
# ===========================================================================


class TestReviewDiagnosticsFriendlyText:
    """Verify _extract_error_text and _infer_fail_reason handle TimeoutError correctly."""

    def test_build_diagnostics_timeout_empty_message_replaced(self):
        """TimeoutError() with empty message should produce friendly Chinese text."""
        diag = build_review_exception_diagnostics(
            TimeoutError(),
            cycle=5,
            get_settings_fn=_make_settings,
        )
        error_text = str(diag.get("error_text") or "")
        assert "empty message" not in error_text.lower()
        assert "审查超时" in error_text

    def test_build_diagnostics_timeout_with_message_kept(self):
        """TimeoutError('some detail') should keep the original message."""
        diag = build_review_exception_diagnostics(
            TimeoutError("ACP prompt 执行超时 (120s)"),
            cycle=5,
            get_settings_fn=_make_settings,
        )
        error_text = str(diag.get("error_text") or "")
        assert "120s" in error_text

    def test_build_diagnostics_fail_reason_is_timeout(self):
        """fail_reason should be 'timeout' for TimeoutError."""
        diag = build_review_exception_diagnostics(
            TimeoutError(),
            cycle=1,
            get_settings_fn=_make_settings,
        )
        assert diag["fail_reason"] == "timeout"

    def test_build_diagnostics_non_timeout_keeps_error_text(self):
        """Non-timeout exceptions should preserve their error_text."""
        diag = build_review_exception_diagnostics(
            ValueError("bad value"),
            cycle=2,
            get_settings_fn=_make_settings,
        )
        assert "bad value" in str(diag.get("error_text") or "")
        assert diag["fail_reason"] == "parse_error"


# ===========================================================================
# T9: conduct_review returns friendly ReviewResult on timeout
# ===========================================================================


class TestConductReviewTimeout:
    """conduct_review should return a ReviewResult with friendly timeout text."""

    def test_timeout_fallback_suggestion_is_friendly(self):
        """On timeout, fallback suggestions must be user-friendly Chinese."""
        settings = _make_settings()
        project = _make_project()
        circuit = ReviewCircuitState()

        def fake_send_prompt(*args, **kwargs):
            raise TimeoutError()

        result = conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=fake_send_prompt,
            build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
            ),
            circuit=circuit,
            cycle=1,
        )

        assert isinstance(result, ReviewResult)
        assert len(result.reviews) == len(ReviewPerspective)
        for pr in result.reviews:
            assert not pr.passed
            assert len(pr.suggestions) == 1
            suggestion = pr.suggestions[0]
            assert "超时" in suggestion
            assert "empty message" not in suggestion.lower()

    def test_non_timeout_fallback_shows_error_detail(self):
        """Non-timeout exceptions should produce a suggestion with error detail."""
        settings = _make_settings()
        project = _make_project()
        circuit = ReviewCircuitState()

        def fake_send_prompt(*args, **kwargs):
            raise ValueError("connection reset")

        result = conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=fake_send_prompt,
            build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
            ),
            circuit=circuit,
            cycle=1,
        )

        assert isinstance(result, ReviewResult)
        for pr in result.reviews:
            suggestion = pr.suggestions[0]
            assert "审查执行异常" in suggestion


# ===========================================================================
# T10: Circuit breaker — triggers after consecutive failures, recovers
# ===========================================================================


class TestCircuitBreaker:
    """Verify the review failure circuit breaker logic."""

    def _run_review_cycle(self, settings, project, circuit, cycle):
        """Helper: run one conduct_review that always times out."""
        def fake_send_prompt(*args, **kwargs):
            raise TimeoutError()

        return conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=fake_send_prompt,
            build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
            ),
            circuit=circuit,
            cycle=cycle,
        )

    def test_circuit_opens_after_max_consecutive_failures(self):
        """After 3 consecutive failures, circuit should be open."""
        settings = _make_settings(
            spec_review_failure_circuit_enabled=True,
            spec_review_failure_max_consecutive=3,
            spec_review_failure_cooldown_cycles=3,
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        # 3 consecutive failures
        for cycle in range(1, 4):
            self._run_review_cycle(settings, project, circuit, cycle)

        assert circuit.review_failure_consecutive >= 3
        assert circuit.review_circuit_open_until_cycle > 0

    def test_circuit_skips_review_while_open(self):
        """While circuit is open, conduct_review should skip and return fallback."""
        settings = _make_settings(
            spec_review_failure_circuit_enabled=True,
            spec_review_failure_max_consecutive=3,
            spec_review_failure_cooldown_cycles=3,
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        # Open circuit: 3 failures at cycles 1-3
        for cycle in range(1, 4):
            self._run_review_cycle(settings, project, circuit, cycle)

        open_until = circuit.review_circuit_open_until_cycle

        # Cycle within cooldown should be skipped (no prompt sent)
        send_called = False
        def tracking_send(*args, **kwargs):
            nonlocal send_called
            send_called = True
            raise TimeoutError()

        result = conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=tracking_send,
            build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
            ),
            circuit=circuit,
            cycle=4,  # within cooldown
        )

        assert not send_called, "Review prompt should not be sent while circuit is open"
        assert isinstance(result, ReviewResult)
        for pr in result.reviews:
            assert "熔断" in pr.suggestions[0]

    def test_circuit_recovers_after_cooldown(self):
        """After cooldown cycles pass, circuit should allow review again."""
        settings = _make_settings(
            spec_review_failure_circuit_enabled=True,
            spec_review_failure_max_consecutive=3,
            spec_review_failure_cooldown_cycles=3,
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        # Open circuit at cycle 3 (open_until = 3 + 3 = 6)
        for cycle in range(1, 4):
            self._run_review_cycle(settings, project, circuit, cycle)

        open_until = circuit.review_circuit_open_until_cycle

        # Cycle after cooldown should attempt review again
        send_called = False
        def tracking_send(*args, **kwargs):
            nonlocal send_called
            send_called = True
            raise ValueError("normal error")  # not timeout

        conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=tracking_send,
            build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
            ),
            circuit=circuit,
            cycle=open_until + 1,  # past cooldown
        )

        assert send_called, "Review should be attempted after cooldown expires"


# ===========================================================================
# T11: Convergence detection skips review_failed cycles
# ===========================================================================


class TestConvergenceSkipsReviewFailed:
    """detect_convergence must return False when recent cycles have review_failed."""

    def _make_cycle(self, num: int, *, decision: str = "", review: ReviewResult | None = None) -> SpecCycle:
        c = SpecCycle(cycle_number=num)
        c.review_result = review
        c.review_decision = decision
        return c

    def test_review_failed_decision_prevents_convergence(self):
        """Cycles with review_failed* decision should not converge."""
        review = _make_review_result(False, ["fix timeout"])
        project = _make_project(cycles=[
            self._make_cycle(1, decision="review_failed_continue", review=review),
            self._make_cycle(2, decision="review_failed_continue", review=review),
        ])

        result = detect_convergence(
            project,
            convergence_window=2,
            review_enabled=True,
        )

        assert result is False

    def test_review_failed_open_circuit_prevents_convergence(self):
        """review_failed_open_circuit decision should also prevent convergence."""
        review = _make_review_result(False, ["circuit breaker"])
        project = _make_project(cycles=[
            self._make_cycle(1, decision="review_failed_open_circuit", review=review),
            self._make_cycle(2, decision="review_failed_open_circuit", review=review),
        ])

        result = detect_convergence(
            project,
            convergence_window=2,
            review_enabled=True,
        )

        assert result is False

    def test_normal_identical_reviews_do_converge(self):
        """Normal (non-failed) cycles with identical suggestions should converge."""
        review = _make_review_result(False, ["same suggestion"])
        project = _make_project(cycles=[
            self._make_cycle(1, decision="needs_improvement", review=review),
            self._make_cycle(2, decision="needs_improvement", review=review),
        ])

        result = detect_convergence(
            project,
            convergence_window=2,
            review_enabled=True,
        )

        assert result is True


# ===========================================================================
# T12: Exponential backoff for circuit breaker cooldown
# ===========================================================================


class TestSpecCircuitExponentialBackoff:
    """Verify exponential backoff: cooldown grows 3→6→12 on repeated triggers."""

    def _run_review_cycle(self, settings, project, circuit, cycle):
        def fake_send_prompt(*args, **kwargs):
            raise TimeoutError()

        return conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=fake_send_prompt,
            build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
            ),
            circuit=circuit,
            cycle=cycle,
        )

    def test_first_trigger_cooldown_is_base(self):
        """First circuit trigger: cooldown = base (3)."""
        settings = _make_settings(
            spec_review_failure_max_consecutive=3,
            spec_review_failure_cooldown_cycles=3,
            spec_review_failure_max_cooldown_cycles=12,
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        for cycle in range(1, 4):
            self._run_review_cycle(settings, project, circuit, cycle)

        # First trigger: cooldown=3, open_until = 3 + 3 = 6
        assert circuit.review_circuit_open_until_cycle == 6
        assert circuit.backoff_level == 1

    def test_second_trigger_cooldown_doubles(self):
        """Second circuit trigger: cooldown = 6."""
        settings = _make_settings(
            spec_review_failure_max_consecutive=3,
            spec_review_failure_cooldown_cycles=3,
            spec_review_failure_max_cooldown_cycles=12,
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        # First trigger (cycles 1-3)
        for cycle in range(1, 4):
            self._run_review_cycle(settings, project, circuit, cycle)
        first_open_until = circuit.review_circuit_open_until_cycle

        # Reset consecutive but keep backoff_level (simulate: circuit closes, fails again)
        circuit.review_failure_consecutive = 0

        # Second trigger (cycles after cooldown)
        base = first_open_until + 1
        for cycle in range(base, base + 3):
            self._run_review_cycle(settings, project, circuit, cycle)

        # cooldown = 3 * 2^1 = 6
        assert circuit.review_circuit_open_until_cycle == (base + 2) + 6
        assert circuit.backoff_level == 2

    def test_third_trigger_cooldown_capped(self):
        """Third circuit trigger: cooldown = min(12, max_cooldown=12)."""
        settings = _make_settings(
            spec_review_failure_max_consecutive=3,
            spec_review_failure_cooldown_cycles=3,
            spec_review_failure_max_cooldown_cycles=12,
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        # First trigger (cycles 1-3) → cooldown=3, open_until=6, backoff=1
        for cycle in range(1, 4):
            self._run_review_cycle(settings, project, circuit, cycle)
        assert circuit.backoff_level == 1

        # Second trigger: start after cooldown expires
        circuit.review_failure_consecutive = 0
        base2 = circuit.review_circuit_open_until_cycle + 1
        for cycle in range(base2, base2 + 3):
            self._run_review_cycle(settings, project, circuit, cycle)
        assert circuit.backoff_level == 2

        # Third trigger: start after cooldown expires
        circuit.review_failure_consecutive = 0
        base3 = circuit.review_circuit_open_until_cycle + 1
        for cycle in range(base3, base3 + 3):
            self._run_review_cycle(settings, project, circuit, cycle)
        assert circuit.backoff_level == 3
        # cooldown = min(3 * 2^2, 12) = 12
        assert circuit.review_circuit_open_until_cycle == (base3 + 2) + 12

        # Fourth trigger: still capped at 12
        circuit.review_failure_consecutive = 0
        base4 = circuit.review_circuit_open_until_cycle + 1
        for cycle in range(base4, base4 + 3):
            self._run_review_cycle(settings, project, circuit, cycle)
        assert circuit.review_circuit_open_until_cycle == (base4 + 2) + 12

    def test_success_resets_backoff_level(self):
        """After a successful review, backoff_level resets to 0."""
        settings = _make_settings(
            spec_review_failure_max_consecutive=3,
            spec_review_failure_cooldown_cycles=3,
            spec_review_failure_max_cooldown_cycles=12,
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        # Trigger once
        for cycle in range(1, 4):
            self._run_review_cycle(settings, project, circuit, cycle)
        assert circuit.backoff_level == 1

        # Simulate success
        def success_send(*args, **kwargs):
            pass  # no exception = success

        conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=success_send,
            build_review_exception_diagnostics_fn=lambda e, **kw: {},
            circuit=circuit,
            cycle=10,
        )

        assert circuit.backoff_level == 0
        assert circuit.consecutive_timeouts == 0


# ===========================================================================
# T13: Adaptive (progressive) review timeout
# ===========================================================================


class TestSpecAdaptiveTimeout:
    """Verify review timeout decreases on consecutive timeouts."""

    def test_timeout_decreases_on_consecutive_timeouts(self):
        """Consecutive TimeoutErrors should trigger progressively shorter timeouts."""
        settings = _make_settings(
            spec_review_timeout=120,
            spec_review_min_timeout=30,
            spec_review_failure_max_consecutive=100,  # high so circuit doesn't interfere
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        captured_timeouts = []

        def capturing_send(*args, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            raise TimeoutError()

        for cycle in range(1, 4):
            conduct_review(
                session=MagicMock(),
                settings=settings,
                project=project,
                send_prompt_with_retry_fn=capturing_send,
                build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                    e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
                ),
                circuit=circuit,
                cycle=cycle,
            )

        # Timeout sequence: 120 (n=0), 60 (n=1), 30 (n=2)
        assert captured_timeouts == [120, 60, 30]

    def test_timeout_resets_after_success(self):
        """After success, timeout should go back to base."""
        settings = _make_settings(
            spec_review_timeout=120,
            spec_review_min_timeout=30,
            spec_review_failure_max_consecutive=100,
        )
        project = _make_project()
        circuit = ReviewCircuitState()

        # 2 timeouts
        def timeout_send(*args, **kwargs):
            raise TimeoutError()

        for cycle in range(1, 3):
            conduct_review(
                session=MagicMock(),
                settings=settings,
                project=project,
                send_prompt_with_retry_fn=timeout_send,
                build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                    e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
                ),
                circuit=circuit,
                cycle=cycle,
            )
        assert circuit.consecutive_timeouts == 2

        # 1 success
        def success_send(*args, **kwargs):
            pass

        conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=success_send,
            build_review_exception_diagnostics_fn=lambda e, **kw: {},
            circuit=circuit,
            cycle=10,
        )
        assert circuit.consecutive_timeouts == 0

        # Next timeout should use base again
        captured = []

        def capture_send(*args, **kwargs):
            captured.append(kwargs.get("timeout"))
            raise TimeoutError()

        conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=capture_send,
            build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
            ),
            circuit=circuit,
            cycle=11,
        )
        assert captured == [120]

    def test_timeout_respects_min(self):
        """Timeout never goes below min_timeout."""
        settings = _make_settings(
            spec_review_timeout=120,
            spec_review_min_timeout=30,
            spec_review_failure_max_consecutive=100,
        )
        project = _make_project()
        circuit = ReviewCircuitState()
        circuit.consecutive_timeouts = 10  # very high

        captured = []

        def capture_send(*args, **kwargs):
            captured.append(kwargs.get("timeout"))
            raise TimeoutError()

        conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=capture_send,
            build_review_exception_diagnostics_fn=lambda e, **kw: build_review_exception_diagnostics(
                e, cycle=kw.get("cycle", 1), get_settings_fn=lambda: settings,
            ),
            circuit=circuit,
            cycle=1,
        )
        assert captured == [30]


# ---------------------------------------------------------------------------
# T-Skip: Spec ReviewCircuitState.consecutive_skips serialization & overrun
# ---------------------------------------------------------------------------


class TestConsecutiveSkipsSerialization:
    """consecutive_skips survives to_dict → from_dict round-trip."""

    def test_round_trip_default(self):
        circuit = ReviewCircuitState()
        assert circuit.consecutive_skips == 0
        d = circuit.to_dict()
        assert "consecutive_skips" in d
        restored = ReviewCircuitState.from_dict(d)
        assert restored.consecutive_skips == 0

    def test_round_trip_nonzero(self):
        circuit = ReviewCircuitState(consecutive_skips=7)
        d = circuit.to_dict()
        assert d["consecutive_skips"] == 7
        restored = ReviewCircuitState.from_dict(d)
        assert restored.consecutive_skips == 7

    def test_from_dict_missing_key_defaults_zero(self):
        """Backward compat: old persisted state without consecutive_skips."""
        restored = ReviewCircuitState.from_dict({"review_failure_consecutive": 2})
        assert restored.consecutive_skips == 0


class TestSpecSkipOverrunProtection:
    """conduct_review increments consecutive_skips on circuit-open skip
    and logs a warning when the overrun threshold is reached."""

    def test_consecutive_skips_increments_on_skip(self):
        settings = _make_settings(spec_review_failure_max_consecutive=3)
        project = _make_project()
        circuit = ReviewCircuitState(
            review_failure_consecutive=3,
            review_circuit_open_until_cycle=10,
        )
        assert circuit.consecutive_skips == 0

        result = conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=MagicMock(),
            build_review_exception_diagnostics_fn=MagicMock(),
            circuit=circuit,
            cycle=5,
        )
        assert circuit.consecutive_skips == 1
        # Below overrun threshold: suggestions should NOT contain overrun warning
        for rev in result.reviews:
            assert not any("跳过次数异常偏高" in s for s in rev.suggestions)

    def test_overrun_warning_logged(self):
        """When consecutive_skips reaches threshold, a warning is logged and suggestions reflect overrun."""
        settings = _make_settings(spec_review_failure_max_consecutive=3)
        project = _make_project()
        # threshold = max(1,3)*2 = 6; set skips to 5 so next skip hits 6
        circuit = ReviewCircuitState(
            review_failure_consecutive=3,
            review_circuit_open_until_cycle=100,
            consecutive_skips=5,
        )

        import logging
        with patch.object(logging.getLogger("src.spec_engine.review"), "warning") as mock_warn:
            result = conduct_review(
                session=MagicMock(),
                settings=settings,
                project=project,
                send_prompt_with_retry_fn=MagicMock(),
                build_review_exception_diagnostics_fn=MagicMock(),
                circuit=circuit,
                cycle=50,
            )
        assert circuit.consecutive_skips == 6
        overrun_calls = [c for c in mock_warn.call_args_list if "review_skip_overrun" in str(c)]
        assert len(overrun_calls) >= 1
        # Verify suggestions surface overrun info to user
        for rev in result.reviews:
            assert any("跳过次数异常偏高" in s for s in rev.suggestions)

    def test_consecutive_skips_reset_on_success(self):
        """Successful review resets consecutive_skips to 0."""
        settings = _make_settings(spec_review_failure_max_consecutive=3)
        project = _make_project()
        circuit = ReviewCircuitState(consecutive_skips=5)

        def fake_send(*args, **kwargs):
            # Return valid review text so parse succeeds
            pass

        conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=fake_send,
            build_review_exception_diagnostics_fn=MagicMock(),
            circuit=circuit,
            cycle=1,
        )
        assert circuit.consecutive_skips == 0
