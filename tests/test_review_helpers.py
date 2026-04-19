"""Unit tests for src/utils/review_helpers.py shared functions."""

import pytest
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock

from src.utils.review_helpers import (
    ReviewExceptionResult,
    _is_timeout_error,
    build_review_error_suggestion,
    compute_adaptive_timeout,
    compute_exponential_cooldown,
    handle_review_exception,
)


# ---------------------------------------------------------------------------
# build_review_error_suggestion
# ---------------------------------------------------------------------------

class TestBuildReviewErrorSuggestion:
    def test_timeout_returns_timeout_text(self):
        result = build_review_error_suggestion(fail_reason="timeout")
        assert "超时" in result
        assert "审查超时" in result

    def test_empty_error_text_returns_retry(self):
        result = build_review_error_suggestion(fail_reason="exception", error_text="", err_repr="")
        assert result == "审查执行异常，将在下一轮重试"

    def test_empty_message_marker_returns_retry(self):
        result = build_review_error_suggestion(
            fail_reason="exception",
            error_text="TimeoutError (empty message)",
            err_repr="",
        )
        assert result == "审查执行异常，将在下一轮重试"

    def test_empty_message_in_err_repr_returns_retry(self):
        result = build_review_error_suggestion(
            fail_reason="exception",
            error_text="",
            err_repr="TimeoutError (empty message)",
        )
        assert result == "审查执行异常，将在下一轮重试"

    def test_real_error_text_included(self):
        result = build_review_error_suggestion(
            fail_reason="exception",
            error_text="Connection refused",
            err_repr="",
        )
        assert result == "审查执行异常: Connection refused"

    def test_err_repr_used_when_error_text_empty(self):
        result = build_review_error_suggestion(
            fail_reason="exception",
            error_text="",
            err_repr="RuntimeError('bad state')",
        )
        assert "审查执行异常: RuntimeError('bad state')" == result

    def test_all_defaults_returns_retry(self):
        result = build_review_error_suggestion()
        assert result == "审查执行异常，将在下一轮重试"

    def test_whitespace_only_error_text(self):
        result = build_review_error_suggestion(fail_reason="exception", error_text="   ", err_repr="   ")
        assert result == "审查执行异常，将在下一轮重试"


# ---------------------------------------------------------------------------
# compute_exponential_cooldown
# ---------------------------------------------------------------------------

class TestComputeExponentialCooldown:
    def test_level_0(self):
        assert compute_exponential_cooldown(0, base_cooldown=3, max_cooldown=12) == 3

    def test_level_1(self):
        assert compute_exponential_cooldown(1, base_cooldown=3, max_cooldown=12) == 6

    def test_level_2(self):
        assert compute_exponential_cooldown(2, base_cooldown=3, max_cooldown=12) == 12

    def test_level_3_capped(self):
        assert compute_exponential_cooldown(3, base_cooldown=3, max_cooldown=12) == 12

    def test_level_10_capped(self):
        assert compute_exponential_cooldown(10, base_cooldown=3, max_cooldown=12) == 12

    def test_negative_level_treated_as_0(self):
        assert compute_exponential_cooldown(-1, base_cooldown=3, max_cooldown=12) == 3

    def test_custom_base_and_max(self):
        assert compute_exponential_cooldown(0, base_cooldown=5, max_cooldown=20) == 5
        assert compute_exponential_cooldown(1, base_cooldown=5, max_cooldown=20) == 10
        assert compute_exponential_cooldown(2, base_cooldown=5, max_cooldown=20) == 20
        assert compute_exponential_cooldown(3, base_cooldown=5, max_cooldown=20) == 20


# ---------------------------------------------------------------------------
# compute_adaptive_timeout
# ---------------------------------------------------------------------------

class TestComputeAdaptiveTimeout:
    def test_n0_returns_base(self):
        assert compute_adaptive_timeout(0, base_timeout=120, min_timeout=30) == 120

    def test_n1_halves(self):
        assert compute_adaptive_timeout(1, base_timeout=120, min_timeout=30) == 60

    def test_n2_quarters(self):
        assert compute_adaptive_timeout(2, base_timeout=120, min_timeout=30) == 30

    def test_n3_floored(self):
        assert compute_adaptive_timeout(3, base_timeout=120, min_timeout=30) == 30

    def test_n10_floored(self):
        assert compute_adaptive_timeout(10, base_timeout=120, min_timeout=30) == 30

    def test_negative_n_treated_as_0(self):
        assert compute_adaptive_timeout(-1, base_timeout=120, min_timeout=30) == 120

    def test_custom_base_and_min(self):
        assert compute_adaptive_timeout(0, base_timeout=60, min_timeout=10) == 60
        assert compute_adaptive_timeout(1, base_timeout=60, min_timeout=10) == 30
        assert compute_adaptive_timeout(2, base_timeout=60, min_timeout=10) == 15
        assert compute_adaptive_timeout(3, base_timeout=60, min_timeout=10) == 10


# ---------------------------------------------------------------------------
# _is_timeout_error
# ---------------------------------------------------------------------------

class TestIsTimeoutError:
    def test_fail_reason_timeout(self):
        assert _is_timeout_error(RuntimeError("x"), fail_reason="timeout") is True

    def test_isinstance_timeout(self):
        assert _is_timeout_error(TimeoutError(), fail_reason="exception") is True

    def test_detail_contains_timeout(self):
        assert _is_timeout_error(RuntimeError("x"), fail_reason="exception", error_detail="request timeout") is True

    def test_not_timeout(self):
        assert _is_timeout_error(RuntimeError("oops"), fail_reason="exception", error_detail="oops") is False

    def test_empty_inputs(self):
        assert _is_timeout_error(RuntimeError(), fail_reason="", error_detail="") is False

    def test_timeout_empty_message(self):
        assert _is_timeout_error(TimeoutError(""), fail_reason="timeout") is True


# ---------------------------------------------------------------------------
# handle_review_exception — helpers
# ---------------------------------------------------------------------------

@dataclass
class _MockCircuitSpec:
    """Mimics ReviewCircuitState."""
    review_failure_consecutive: int = 0
    review_circuit_open_until_cycle: int = 0
    last_review_failure_diag: Optional[dict] = None
    backoff_level: int = 0
    consecutive_timeouts: int = 0


@dataclass
class _MockCircuitLoop:
    """Mimics LoopReviewCircuitState."""
    review_failure_consecutive: int = 0
    review_circuit_open_until_iter: int = 0
    last_review_failure_diag: Optional[dict] = None
    backoff_level: int = 0
    consecutive_timeouts: int = 0


def _stub_build_diag(e, *, cycle, **kw):
    """Minimal diagnostics builder for tests."""
    fail_reason = "timeout" if isinstance(e, TimeoutError) else "exception"
    return {
        "phase": "review",
        "role": "multi_perspective",
        "cycle": cycle,
        "decision": "review_failed_continue",
        "fail_reason": fail_reason,
        "err_type": type(e).__name__,
        "err_repr": repr(e),
        "error_text": str(e) or "",
        "traceback_snippet": "",
    }


def _make_settings(**overrides):
    """Build a mock settings object with circuit-breaker defaults."""
    defaults = {
        "spec_review_failure_circuit_enabled": True,
        "spec_review_failure_max_consecutive": 3,
        "spec_review_failure_cooldown_cycles": 3,
        "spec_review_failure_max_cooldown_cycles": 12,
        "loop_review_failure_circuit_enabled": True,
        "loop_review_failure_max_consecutive": 3,
        "loop_review_failure_cooldown_iterations": 3,
        "loop_review_failure_max_cooldown_iterations": 12,
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# handle_review_exception — spec engine
# ---------------------------------------------------------------------------

class TestHandleReviewExceptionSpec:
    def test_timeout_increments_consecutive(self):
        circuit = _MockCircuitSpec()
        settings = _make_settings()
        result = handle_review_exception(
            TimeoutError(),
            circuit=circuit,
            cycle=1,
            settings=settings,
            engine="spec",
            build_diag_fn=_stub_build_diag,
        )
        assert circuit.consecutive_timeouts == 1
        assert "超时" in result.suggestion_text
        assert result.review_decision == "review_failed_continue"

    def test_normal_exception_resets_consecutive_timeouts(self):
        circuit = _MockCircuitSpec(consecutive_timeouts=2)
        settings = _make_settings()
        result = handle_review_exception(
            RuntimeError("bad"),
            circuit=circuit,
            cycle=1,
            settings=settings,
            engine="spec",
            build_diag_fn=_stub_build_diag,
        )
        assert circuit.consecutive_timeouts == 0
        assert "审查执行异常: bad" in result.suggestion_text

    def test_empty_message_exception(self):
        circuit = _MockCircuitSpec()
        settings = _make_settings()

        def _empty_diag(e, *, cycle, **kw):
            """Simulate diag where error_text has (empty message) marker."""
            return {
                "phase": "review",
                "role": "multi_perspective",
                "cycle": cycle,
                "decision": "review_failed_continue",
                "fail_reason": "exception",
                "err_type": "RuntimeError",
                "err_repr": "RuntimeError (empty message)",
                "error_text": "(empty message)",
                "traceback_snippet": "",
            }

        result = handle_review_exception(
            RuntimeError(""),
            circuit=circuit,
            cycle=1,
            settings=settings,
            engine="spec",
            build_diag_fn=_empty_diag,
        )
        assert result.suggestion_text == "审查执行异常，将在下一轮重试"

    def test_circuit_opens_after_max_consecutive(self):
        circuit = _MockCircuitSpec(review_failure_consecutive=2)
        settings = _make_settings()
        result = handle_review_exception(
            RuntimeError("fail"),
            circuit=circuit,
            cycle=5,
            settings=settings,
            engine="spec",
            build_diag_fn=_stub_build_diag,
        )
        assert result.review_decision == "review_failed_open_circuit"
        assert circuit.review_circuit_open_until_cycle == 5 + 3  # base cooldown=3
        assert circuit.backoff_level == 1

    def test_metrics_contains_required_fields(self):
        circuit = _MockCircuitSpec()
        settings = _make_settings()
        result = handle_review_exception(
            TimeoutError(),
            circuit=circuit,
            cycle=2,
            settings=settings,
            engine="spec",
            build_diag_fn=_stub_build_diag,
            review_timeout=60,
        )
        m = result.metrics
        assert m["metric_type"] == "review_exception"
        assert m["engine"] == "spec"
        assert "cycle" in m
        assert m["fail_reason"] == "timeout"
        assert m["adaptive_timeout"] == 60
        assert "consecutive_timeouts" in m
        assert "consecutive_failures" in m
        assert "circuit_open" in m
        assert "backoff_level" in m

    def test_result_is_named_tuple(self):
        circuit = _MockCircuitSpec()
        settings = _make_settings()
        result = handle_review_exception(
            RuntimeError("x"),
            circuit=circuit,
            cycle=1,
            settings=settings,
            engine="spec",
            build_diag_fn=_stub_build_diag,
        )
        assert isinstance(result, ReviewExceptionResult)
        assert isinstance(result.diag, dict)

    def test_diag_stored_on_circuit(self):
        circuit = _MockCircuitSpec()
        settings = _make_settings()
        handle_review_exception(
            RuntimeError("store me"),
            circuit=circuit,
            cycle=1,
            settings=settings,
            engine="spec",
            build_diag_fn=_stub_build_diag,
        )
        assert circuit.last_review_failure_diag is not None
        assert circuit.last_review_failure_diag["phase"] == "review"


# ---------------------------------------------------------------------------
# handle_review_exception — loop engine
# ---------------------------------------------------------------------------

class TestHandleReviewExceptionLoop:
    def test_timeout_increments_consecutive(self):
        circuit = _MockCircuitLoop()
        settings = _make_settings()
        result = handle_review_exception(
            TimeoutError(),
            circuit=circuit,
            cycle=1,
            settings=settings,
            engine="loop",
            build_diag_fn=_stub_build_diag,
        )
        assert circuit.consecutive_timeouts == 1
        assert "超时" in result.suggestion_text

    def test_circuit_opens_after_max_consecutive(self):
        circuit = _MockCircuitLoop(review_failure_consecutive=2)
        settings = _make_settings()
        result = handle_review_exception(
            RuntimeError("fail"),
            circuit=circuit,
            cycle=10,
            settings=settings,
            engine="loop",
            build_diag_fn=_stub_build_diag,
        )
        assert result.review_decision == "review_failed_open_circuit"
        assert circuit.review_circuit_open_until_iter == 10 + 3
        assert circuit.backoff_level == 1

    def test_metrics_uses_iteration_key(self):
        circuit = _MockCircuitLoop()
        settings = _make_settings()
        result = handle_review_exception(
            RuntimeError("x"),
            circuit=circuit,
            cycle=7,
            settings=settings,
            engine="loop",
            build_diag_fn=_stub_build_diag,
        )
        assert "iteration" in result.metrics
        assert result.metrics["iteration"] == 7
        assert "cycle" not in result.metrics

    def test_exponential_backoff_level_increments(self):
        circuit = _MockCircuitLoop(review_failure_consecutive=2, backoff_level=1)
        settings = _make_settings()
        handle_review_exception(
            RuntimeError("x"),
            circuit=circuit,
            cycle=5,
            settings=settings,
            engine="loop",
            build_diag_fn=_stub_build_diag,
        )
        assert circuit.backoff_level == 2
        # cooldown = min(3 * 2^1, 12) = 6
        assert circuit.review_circuit_open_until_iter == 5 + 6

    def test_build_diag_kwargs_forwarded(self):
        calls = []

        def _tracking_diag(e, *, cycle, **kw):
            calls.append(kw)
            return _stub_build_diag(e, cycle=cycle)

        circuit = _MockCircuitLoop()
        settings = _make_settings()
        handle_review_exception(
            RuntimeError("x"),
            circuit=circuit,
            cycle=1,
            settings=settings,
            engine="loop",
            build_diag_fn=_tracking_diag,
            build_diag_kwargs={"project_name": "test_proj", "chat_id": "c1"},
        )
        assert len(calls) == 1
        assert calls[0]["project_name"] == "test_proj"
        assert calls[0]["chat_id"] == "c1"
