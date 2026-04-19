"""Shared review exception-handling helpers for SpecEngine & LoopEngine.

Centralizes:
- Fallback suggestion text generation (timeout / empty / normal errors)
- Exponential-backoff cooldown computation for circuit breakers
- Adaptive (progressive) review timeout computation
- Unified review exception handling (handle_review_exception)
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import NamedTuple, Optional

logger = logging.getLogger(__name__)


def build_review_error_suggestion(
    *,
    fail_reason: str = "",
    error_text: str = "",
    err_repr: str = "",
) -> str:
    """Build a user-friendly Chinese suggestion from review exception diagnostics.

    Logic (same as the previously duplicated branches in SpecEngine & LoopEngine):
    - timeout  → "审查超时，跳过本轮审查继续执行"
    - empty / "(empty message)" → "审查执行异常，将在下一轮重试"
    - otherwise → "审查执行异常: {detail}"
    """
    _fail_reason = (fail_reason or "").strip()
    if _fail_reason == "timeout":
        return "审查超时，跳过本轮审查继续执行"

    _raw = (error_text or "").strip() or (err_repr or "").strip()
    if not _raw or "(empty message)" in _raw:
        return "审查执行异常，将在下一轮重试"
    return f"审查执行异常: {_raw}"


def compute_exponential_cooldown(
    backoff_level: int,
    base_cooldown: int = 3,
    max_cooldown: int = 12,
) -> int:
    """Compute exponential-backoff cooldown for the review circuit breaker.

    Formula: ``min(base_cooldown * 2**backoff_level, max_cooldown)``

    Examples (base=3, max=12):
        level 0 → 3
        level 1 → 6
        level 2 → 12
        level 3 → 12  (capped)
    """
    level = max(0, int(backoff_level))
    return min(base_cooldown * (2 ** level), max_cooldown)


def compute_adaptive_timeout(
    consecutive_timeouts: int,
    base_timeout: int = 120,
    min_timeout: int = 30,
    hard_floor: int = 15,
) -> int:
    """Compute progressively shorter review timeout after consecutive timeouts.

    Formula: ``max(base_timeout // 2**n, min_timeout, hard_floor)``

    The *hard_floor* (default 15 s) ensures the timeout never drops below
    the minimum viable ACP communication round-trip, preventing cascading
    instant timeouts.

    Examples (base=120, min=30, hard_floor=15):
        n=0 → 120
        n=1 → 60
        n=2 → 30
        n=3 → 30  (floored)
    """
    n = max(0, int(consecutive_timeouts))
    return max(base_timeout // (2 ** n), min_timeout, hard_floor)


# ---------------------------------------------------------------------------
# Sliding window tracker for dynamic circuit breaker
# ---------------------------------------------------------------------------


class SlidingWindowTracker:
    """Tracks recent review outcomes in a fixed-size sliding window.

    Used to compute success rate for dynamic circuit-breaker thresholds.

    Parameters
    ----------
    window_size : int
        Maximum number of recent outcomes to keep (default 10).
    """

    def __init__(self, window_size: int = 10) -> None:
        self._window_size = max(1, int(window_size))
        self._outcomes: deque[str] = deque(maxlen=self._window_size)

    @property
    def window_size(self) -> int:
        return self._window_size

    @property
    def outcomes(self) -> list[str]:
        return list(self._outcomes)

    def record(self, outcome: str) -> None:
        """Record a single outcome (``"success"`` / ``"timeout"`` / ``"error"``)."""
        self._outcomes.append(outcome)

    def success_rate(self) -> float:
        """Return success rate in ``[0.0, 1.0]``.  Returns ``1.0`` if empty."""
        if not self._outcomes:
            return 1.0
        return sum(1 for o in self._outcomes if o == "success") / len(self._outcomes)

    def should_open_circuit(self, threshold: float = 0.3) -> bool:
        """Return ``True`` if success rate is below *threshold* and window is full."""
        if len(self._outcomes) < self._window_size:
            return False
        return self.success_rate() < threshold

    @classmethod
    def from_list(cls, outcomes: list[str], window_size: int = 10) -> "SlidingWindowTracker":
        """Reconstruct from a serialised outcome list."""
        tracker = cls(window_size=window_size)
        for o in outcomes:
            tracker.record(o)
        return tracker


# ---------------------------------------------------------------------------
# Unified review exception handler
# ---------------------------------------------------------------------------


class ReviewExceptionResult(NamedTuple):
    """Return type for :func:`handle_review_exception`."""

    diag: dict
    """Normalized diagnostics dict."""
    suggestion_text: str
    """User-facing fallback suggestion text."""
    review_decision: Optional[str]
    """``None`` on success, ``"review_failed_continue"`` or
    ``"review_failed_open_circuit"`` on failure."""
    metrics: dict
    """Structured metrics dict ready for JSON serialisation."""


def _is_timeout_error(
    e: Exception,
    *,
    fail_reason: str,
    error_detail: str = "",
) -> bool:
    """Unified timeout detection — merges Spec (fail_reason only) and Loop
    (fail_reason + isinstance + detail substring) strategies.

    ``fail_reason`` is already computed by ``build_review_exception_diagnostics``
    via ``_infer_fail_reason`` which traverses ``__cause__``/``__context__``.
    The extra ``isinstance`` / substring checks are **defensive redundancy**.
    """
    if (fail_reason or "").strip() == "timeout":
        return True
    if isinstance(e, TimeoutError):
        return True
    if error_detail and "timeout" in error_detail.lower():
        return True
    return False


def handle_review_exception(
    e: Exception,
    *,
    circuit: object,
    cycle: int,
    settings: object,
    engine: str = "spec",
    build_diag_fn: Optional[object] = None,
    build_diag_kwargs: Optional[dict] = None,
    review_timeout: int = 0,
    review_elapsed_ms: int = 0,
) -> ReviewExceptionResult:
    """Centralised review exception handler shared by Spec & Loop engines.

    Performs — in order:
    1. Structured diagnostics via *build_diag_fn*
    2. Unified timeout detection (``_is_timeout_error``)
    3. ``consecutive_timeouts`` tracking
    4. Circuit breaker counter + cooldown computation
    5. Structured log line + metrics dict assembly
    6. Fallback suggestion text

    Parameters
    ----------
    e : Exception
        The caught exception.
    circuit : ReviewCircuitState | LoopReviewCircuitState
        Mutable circuit-breaker state (updated in-place).
    cycle : int
        Current cycle (Spec) or iteration (Loop).
    settings : object
        Application settings object.
    engine : ``"spec"`` | ``"loop"``
        Controls config attribute prefixes and log/metrics key names.
    build_diag_fn : callable, optional
        ``build_review_exception_diagnostics(e, cycle=…, **build_diag_kwargs)``.
        Imported lazily if ``None``.
    build_diag_kwargs : dict, optional
        Extra kwargs forwarded to *build_diag_fn* (e.g. ``project_name``,
        ``chat_id``).
    review_timeout : int
        The adaptive timeout that was used for the review call.
    review_elapsed_ms : int
        Actual wall-clock elapsed time for the review call (including retries),
        in milliseconds.  Written to ``circuit.last_review_elapsed_ms`` and
        emitted as ``total_elapsed_ms`` in the metrics dict.

    Returns
    -------
    ReviewExceptionResult
        ``(diag, suggestion_text, review_decision, metrics)``
    """
    from .errors import get_error_detail
    from .review_diagnostics import (
        format_review_exception_log_line,
        normalize_review_diagnostics,
    )

    if build_diag_fn is None:
        from .review_diagnostics import build_review_exception_diagnostics
        build_diag_fn = build_review_exception_diagnostics

    # -- config attribute names vary by engine --
    if engine == "loop":
        _circuit_open_attr = "review_circuit_open_until_iter"
        _cfg_circuit_enabled = "loop_review_failure_circuit_enabled"
        _cfg_max_consecutive = "loop_review_failure_max_consecutive"
        _cfg_cooldown = "loop_review_failure_cooldown_iterations"
        _cfg_max_cooldown = "loop_review_failure_max_cooldown_iterations"
        _cycle_key = "iteration"
        _open_until_key = "open_until_iter"
        _log_prefix = "[Loop]"
    else:
        _circuit_open_attr = "review_circuit_open_until_cycle"
        _cfg_circuit_enabled = "spec_review_failure_circuit_enabled"
        _cfg_max_consecutive = "spec_review_failure_max_consecutive"
        _cfg_cooldown = "spec_review_failure_cooldown_cycles"
        _cfg_max_cooldown = "spec_review_failure_max_cooldown_cycles"
        _cycle_key = "cycle"
        _open_until_key = "open_until_cycle"
        _log_prefix = "[Spec]"

    enabled = getattr(settings, _cfg_circuit_enabled, True)
    max_consecutive = max(1, int(getattr(settings, _cfg_max_consecutive, 3) or 3))
    cooldown_base = max(0, int(getattr(settings, _cfg_cooldown, 3) or 3))
    max_cooldown = int(getattr(settings, _cfg_max_cooldown, 12) or 12)

    # 1. Structured diagnostics
    error_detail = get_error_detail(e)
    diag_raw = build_diag_fn(e, cycle=cycle, **(build_diag_kwargs or {}))
    diag = normalize_review_diagnostics(diag_raw)
    circuit.last_review_failure_diag = dict(diag)

    # 1b. Record elapsed time on circuit
    try:
        circuit.last_review_elapsed_ms = int(review_elapsed_ms or 0)
    except Exception:
        pass

    # 2. Unified timeout detection + consecutive tracking
    _fail_reason = str(diag.get("fail_reason") or "").strip()
    if _is_timeout_error(e, fail_reason=_fail_reason, error_detail=error_detail):
        circuit.consecutive_timeouts = int(circuit.consecutive_timeouts or 0) + 1
    else:
        circuit.consecutive_timeouts = 0

    # 3. Suggestion text (early — before circuit mutations)
    suggestion_text = build_review_error_suggestion(
        fail_reason=_fail_reason,
        error_text=str(diag.get("error_text") or ""),
        err_repr=str(diag.get("err_repr") or ""),
    )

    # 4. Circuit breaker counter update
    try:
        circuit.review_failure_consecutive = int(circuit.review_failure_consecutive or 0) + 1
    except Exception:
        circuit.review_failure_consecutive = 1

    # 4b. Sliding window outcome recording
    _outcome = "timeout" if _is_timeout_error(e, fail_reason=_fail_reason, error_detail=error_detail) else "error"
    try:
        if hasattr(circuit, "recent_outcomes"):
            circuit.recent_outcomes.append(_outcome)
            # Trim to window size (default 20 for persistence; SlidingWindowTracker uses its own maxlen)
            if len(circuit.recent_outcomes) > 20:
                circuit.recent_outcomes[:] = circuit.recent_outcomes[-20:]
    except Exception:
        pass

    review_decision: Optional[str] = "review_failed_continue"

    # 4c. Sliding window circuit-breaker check (dynamic threshold)
    _raw_ws = getattr(settings, "review_circuit_window_size", None)
    _window_size = int(_raw_ws) if isinstance(_raw_ws, (int, float)) else 10
    _window_size = max(3, _window_size)
    _raw_st = getattr(settings, "review_circuit_success_rate_threshold", None)
    _success_threshold = float(_raw_st) if isinstance(_raw_st, (int, float)) else 0.3
    _sliding_trigger = False
    try:
        if hasattr(circuit, "recent_outcomes") and len(circuit.recent_outcomes) >= _window_size:
            _tracker = SlidingWindowTracker.from_list(circuit.recent_outcomes, window_size=_window_size)
            _sliding_trigger = _tracker.should_open_circuit(threshold=_success_threshold)
    except Exception:
        pass

    _consecutive_trigger = circuit.review_failure_consecutive >= max_consecutive

    if enabled and (_consecutive_trigger or _sliding_trigger) and cooldown_base > 0:
        actual_cooldown = compute_exponential_cooldown(
            circuit.backoff_level, base_cooldown=cooldown_base, max_cooldown=max_cooldown,
        )
        try:
            setattr(circuit, _circuit_open_attr, int(cycle or 0) + actual_cooldown)
        except Exception:
            setattr(circuit, _circuit_open_attr, int(cycle or 0))
        circuit.backoff_level = int(circuit.backoff_level or 0) + 1
        review_decision = "review_failed_open_circuit"

        try:
            circuit.last_review_failure_diag["review_circuit_open"] = True
            circuit.last_review_failure_diag[_open_until_key] = int(getattr(circuit, _circuit_open_attr, 0))
            circuit.last_review_failure_diag["consecutive_failures"] = int(circuit.review_failure_consecutive or 0)
            circuit.last_review_failure_diag["decision"] = "review_failed_open_circuit"
        except Exception:
            pass
        logger.warning(
            "%s Review 熔断器打开: consecutive=%d, %s=%d",
            _log_prefix,
            circuit.review_failure_consecutive,
            _open_until_key,
            getattr(circuit, _circuit_open_attr, 0),
        )

    # 5. Structured log line
    diag_json = ""
    try:
        diag_json = json.dumps(diag, ensure_ascii=False, sort_keys=True)
    except Exception:
        diag_json = '{"phase":"review","decision":"review_failed_continue"}'
    try:
        logger.warning(format_review_exception_log_line(diag, diag_json=diag_json, prefix=_log_prefix))
    except Exception:
        logger.warning(
            "%s 多视角审查异常: %s, 将继续循环", _log_prefix, error_detail,
        )

    # 6. Metrics dict
    _circuit_open_val = getattr(circuit, _circuit_open_attr, 0)
    metrics = {
        "metric_type": "review_exception",
        "engine": engine,
        _cycle_key: int(cycle or 0),
        "fail_reason": _fail_reason,
        "consecutive_timeouts": int(circuit.consecutive_timeouts or 0),
        "consecutive_failures": int(circuit.review_failure_consecutive or 0),
        "circuit_open": bool(_circuit_open_val and int(cycle or 0) < int(_circuit_open_val)),
        "adaptive_timeout": int(review_timeout),
        "backoff_level": int(circuit.backoff_level or 0),
        "total_elapsed_ms": int(review_elapsed_ms or 0),
    }
    try:
        from .metrics_exporter import get_metrics_exporter
        _exporter_type = getattr(settings, "review_metrics_exporter_type", "logger") or "logger"
        _exporter_kwargs: dict = {}
        if _exporter_type == "jsonl":
            _exporter_kwargs["path"] = getattr(settings, "review_metrics_jsonl_path", "review_metrics.jsonl")
        exporter = get_metrics_exporter(exporter_type=_exporter_type, **_exporter_kwargs)
        exporter.export_metrics(metrics, prefix=_log_prefix)
    except Exception:
        # Fallback: original logger.info if exporter fails
        try:
            logger.info("%s review_metrics: %s", _log_prefix, json.dumps(metrics, ensure_ascii=False))
        except Exception:
            pass

    return ReviewExceptionResult(
        diag=diag,
        suggestion_text=suggestion_text,
        review_decision=review_decision,
        metrics=metrics,
    )
