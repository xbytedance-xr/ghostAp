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
