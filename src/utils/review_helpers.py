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
    _raw = (error_text or "").strip() or (err_repr or "").strip()

    if _fail_reason == "timeout" or "TimeoutError" in _raw:
        return "审查超时，跳过本轮审查继续执行"

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


def compute_retry_delay(
    consecutive_timeouts: int,
    base_delay: float = 5.0,
    max_delay: float = 30.0,
    decay_factor: float = 1.5,
) -> float:
    """Compute progressive backoff delay before an in-cycle review retry.

    Formula: ``min(base_delay * decay_factor**n, max_delay)``

    The delay grows with each consecutive timeout to avoid hammering a busy
    system, while *max_delay* caps the wait so that a single cycle doesn't
    stall for too long.

    Parameters
    ----------
    decay_factor : float
        Exponential growth factor per consecutive timeout (default 1.5).
        Configurable via ``spec_review_retry_decay_factor``.

    Examples (base=5.0, max=30.0, decay_factor=1.5):
        n=0 → 5.0
        n=1 → 7.5
        n=2 → 11.25
        n=3 → 16.875
        n=4 → 25.3125
        n=5 → 30.0  (capped)
    """
    if base_delay <= 0:
        raise ValueError(f"base_delay must be > 0, got {base_delay}")
    if max_delay <= 0:
        raise ValueError(f"max_delay must be > 0, got {max_delay}")
    if decay_factor <= 0:
        raise ValueError(f"decay_factor must be > 0, got {decay_factor}")
    n = max(0, int(consecutive_timeouts))
    return min(base_delay * (decay_factor ** n), float(max_delay))


def compute_adaptive_timeout(
    consecutive_timeouts: int,
    base_timeout: int = 120,
    min_timeout: int = 30,
    hard_floor: int = 15,
) -> int:
    """Compute progressively shorter review timeout after consecutive timeouts.

    Formula: ``max(int(base_timeout / 1.3**n), min_timeout, hard_floor)``

    Uses a 1.3x decay factor (instead of 1.5x) for a gentler curve that avoids
    cascading instant timeouts after a few consecutive failures.

    The *hard_floor* (default 15 s) ensures the timeout never drops below
    the minimum viable ACP communication round-trip, preventing cascading
    instant timeouts.

    Examples (base=240, min=60, hard_floor=20):
        n=0 → 240
        n=1 → 184
        n=2 → 142
        n=3 → 109
        n=4 → 84
        n=5 → 64
        n=6 → 60  (floored by min_timeout)
    """
    n = max(0, int(consecutive_timeouts))
    return max(int(base_timeout / (1.3 ** n)), min_timeout, hard_floor)


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


class _EngineConfig:
    """Resolved engine-specific config attribute names and values."""

    __slots__ = (
        "circuit_open_attr", "cycle_key", "open_until_key", "log_prefix",
        "enabled", "max_consecutive", "cooldown_base", "max_cooldown",
    )

    def __init__(self, engine: str, settings: object) -> None:
        if engine == "loop":
            self.circuit_open_attr = "review_circuit_open_until_iter"
            _cfg_circuit_enabled = "loop_review_failure_circuit_enabled"
            _cfg_max_consecutive = "loop_review_failure_max_consecutive"
            _cfg_cooldown = "loop_review_failure_cooldown_iterations"
            _cfg_max_cooldown = "loop_review_failure_max_cooldown_iterations"
            self.cycle_key = "iteration"
            self.open_until_key = "open_until_iter"
            self.log_prefix = "[Loop]"
        else:
            self.circuit_open_attr = "review_circuit_open_until_cycle"
            _cfg_circuit_enabled = "spec_review_failure_circuit_enabled"
            _cfg_max_consecutive = "spec_review_failure_max_consecutive"
            _cfg_cooldown = "spec_review_failure_cooldown_cycles"
            _cfg_max_cooldown = "spec_review_failure_max_cooldown_cycles"
            self.cycle_key = "cycle"
            self.open_until_key = "open_until_cycle"
            self.log_prefix = "[Spec]"

        self.enabled = getattr(settings, _cfg_circuit_enabled, True)
        self.max_consecutive = max(1, int(getattr(settings, _cfg_max_consecutive, 3) or 3))
        self.cooldown_base = max(0, int(getattr(settings, _cfg_cooldown, 3) or 3))
        self.max_cooldown = int(getattr(settings, _cfg_max_cooldown, 12) or 12)


def _build_diagnostics(
    e: Exception,
    *,
    circuit: object,
    cycle: int,
    build_diag_fn: object,
    build_diag_kwargs: Optional[dict],
    review_elapsed_ms: int,
) -> tuple:  # (diag: dict, error_detail: str)
    """Step 1: Build structured diagnostics and record on circuit."""
    from .errors import get_error_detail
    from .review_diagnostics import normalize_review_diagnostics

    error_detail = get_error_detail(e)
    diag_raw = build_diag_fn(e, cycle=cycle, **(build_diag_kwargs or {}))
    diag = normalize_review_diagnostics(diag_raw)
    circuit.last_review_failure_diag = dict(diag)

    try:
        circuit.last_review_elapsed_ms = int(review_elapsed_ms or 0)
    except Exception:
        pass

    return diag, error_detail


def _track_timeout_state(
    e: Exception,
    *,
    circuit: object,
    fail_reason: str,
    error_detail: str,
) -> bool:
    """Step 2: Detect timeout and update consecutive_timeouts on circuit."""
    is_timeout = _is_timeout_error(e, fail_reason=fail_reason, error_detail=error_detail)
    if is_timeout:
        circuit.consecutive_timeouts = int(circuit.consecutive_timeouts or 0) + 1
    else:
        circuit.consecutive_timeouts = 0
    return is_timeout


def _update_circuit_breaker(
    e: Exception,
    *,
    circuit: object,
    cycle: int,
    settings: object,
    ecfg: _EngineConfig,
    is_timeout: bool,
    fail_reason: str,
    error_detail: str,
) -> Optional[str]:
    """Step 3: Update circuit breaker counter/sliding window, return decision."""
    # Increment consecutive failure counter
    try:
        circuit.review_failure_consecutive = int(circuit.review_failure_consecutive or 0) + 1
    except Exception:
        circuit.review_failure_consecutive = 1

    # Sliding window outcome recording
    _outcome = "timeout" if is_timeout else "error"
    try:
        if hasattr(circuit, "recent_outcomes"):
            circuit.recent_outcomes.append(_outcome)
            if len(circuit.recent_outcomes) > 20:
                circuit.recent_outcomes[:] = circuit.recent_outcomes[-20:]
    except Exception:
        pass

    review_decision: Optional[str] = "review_failed_continue"

    # Sliding window circuit-breaker check (dynamic threshold)
    _raw_ws = getattr(settings, "review_circuit_window_size", None)
    _window_size = max(3, int(_raw_ws) if isinstance(_raw_ws, (int, float)) else 10)
    _raw_st = getattr(settings, "review_circuit_success_rate_threshold", None)
    _success_threshold = float(_raw_st) if isinstance(_raw_st, (int, float)) else 0.3
    _sliding_trigger = False
    try:
        if hasattr(circuit, "recent_outcomes") and len(circuit.recent_outcomes) >= _window_size:
            _tracker = SlidingWindowTracker.from_list(circuit.recent_outcomes, window_size=_window_size)
            _sliding_trigger = _tracker.should_open_circuit(threshold=_success_threshold)
    except Exception:
        pass

    _consecutive_trigger = circuit.review_failure_consecutive >= ecfg.max_consecutive

    if ecfg.enabled and (_consecutive_trigger or _sliding_trigger) and ecfg.cooldown_base > 0:
        actual_cooldown = compute_exponential_cooldown(
            circuit.backoff_level, base_cooldown=ecfg.cooldown_base, max_cooldown=ecfg.max_cooldown,
        )
        try:
            setattr(circuit, ecfg.circuit_open_attr, int(cycle or 0) + actual_cooldown)
        except Exception:
            setattr(circuit, ecfg.circuit_open_attr, int(cycle or 0))
        circuit.backoff_level = int(circuit.backoff_level or 0) + 1
        review_decision = "review_failed_open_circuit"

        try:
            circuit.last_review_failure_diag["review_circuit_open"] = True
            circuit.last_review_failure_diag[ecfg.open_until_key] = int(getattr(circuit, ecfg.circuit_open_attr, 0))
            circuit.last_review_failure_diag["consecutive_failures"] = int(circuit.review_failure_consecutive or 0)
            circuit.last_review_failure_diag["decision"] = "review_failed_open_circuit"
        except Exception:
            pass
        logger.warning(
            "%s Review 熔断器打开: consecutive=%d, %s=%d",
            ecfg.log_prefix,
            circuit.review_failure_consecutive,
            ecfg.open_until_key,
            getattr(circuit, ecfg.circuit_open_attr, 0),
        )

    return review_decision


def _emit_log_and_metrics(
    *,
    diag: dict,
    error_detail: str,
    circuit: object,
    cycle: int,
    engine: str,
    settings: object,
    ecfg: _EngineConfig,
    fail_reason: str,
    review_decision: Optional[str],
    review_timeout: int,
    review_elapsed_ms: int,
) -> dict:
    """Step 4: Emit structured log line and build metrics dict."""
    from .review_diagnostics import format_review_exception_log_line

    # Structured log line
    diag_json = ""
    try:
        diag_json = json.dumps(diag, ensure_ascii=False, sort_keys=True)
    except Exception:
        diag_json = '{"phase":"review","decision":"review_failed_continue"}'
    try:
        logger.warning(format_review_exception_log_line(diag, diag_json=diag_json, prefix=ecfg.log_prefix))
    except Exception:
        logger.warning(
            "%s 多视角审查异常: %s, 将继续循环", ecfg.log_prefix, error_detail,
        )

    # Metrics dict
    _circuit_open_val = getattr(circuit, ecfg.circuit_open_attr, 0)
    metrics = {
        "metric_type": "review_exception",
        "engine": engine,
        ecfg.cycle_key: int(cycle or 0),
        "fail_reason": fail_reason,
        "consecutive_timeouts": int(circuit.consecutive_timeouts or 0),
        "consecutive_failures": int(circuit.review_failure_consecutive or 0),
        "circuit_open": bool(_circuit_open_val and int(cycle or 0) < int(_circuit_open_val)),
        "adaptive_timeout": int(review_timeout),
        "backoff_level": int(circuit.backoff_level or 0),
        "total_elapsed_ms": int(review_elapsed_ms or 0),
        "retry_attempted": False,
        "retry_succeeded": False,
    }
    try:
        from .metrics_exporter import get_metrics_exporter
        _exporter_type = getattr(settings, "review_metrics_exporter_type", "logger") or "logger"
        _exporter_kwargs: dict = {}
        if _exporter_type == "jsonl":
            _exporter_kwargs["path"] = getattr(settings, "review_metrics_jsonl_path", "review_metrics.jsonl")
        exporter = get_metrics_exporter(exporter_type=_exporter_type, **_exporter_kwargs)
        exporter.export_metrics(metrics, prefix=ecfg.log_prefix)
    except Exception:
        try:
            logger.info("%s review_metrics: %s", ecfg.log_prefix, json.dumps(metrics, ensure_ascii=False))
        except Exception:
            pass

    return metrics


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

    Delegates to single-responsibility sub-functions:
    1. :func:`_build_diagnostics` — structured diagnostics
    2. :func:`_track_timeout_state` — timeout detection + tracking
    3. :func:`_update_circuit_breaker` — counter + sliding window + cooldown
    4. :func:`_emit_log_and_metrics` — structured log + metrics
    """
    if build_diag_fn is None:
        from .review_diagnostics import build_review_exception_diagnostics
        build_diag_fn = build_review_exception_diagnostics

    ecfg = _EngineConfig(engine, settings)

    # 1. Diagnostics
    diag, error_detail = _build_diagnostics(
        e, circuit=circuit, cycle=cycle, build_diag_fn=build_diag_fn,
        build_diag_kwargs=build_diag_kwargs, review_elapsed_ms=review_elapsed_ms,
    )

    # 2. Timeout detection + tracking
    fail_reason = str(diag.get("fail_reason") or "").strip()
    is_timeout = _track_timeout_state(
        e, circuit=circuit, fail_reason=fail_reason, error_detail=error_detail,
    )

    # 3. Suggestion text (before circuit mutations)
    suggestion_text = build_review_error_suggestion(
        fail_reason=fail_reason,
        error_text=str(diag.get("error_text") or ""),
        err_repr=str(diag.get("err_repr") or ""),
    )

    # 4. Circuit breaker state update
    review_decision = _update_circuit_breaker(
        e, circuit=circuit, cycle=cycle, settings=settings, ecfg=ecfg,
        is_timeout=is_timeout, fail_reason=fail_reason, error_detail=error_detail,
    )

    # 5. Logging + metrics
    metrics = _emit_log_and_metrics(
        diag=diag, error_detail=error_detail, circuit=circuit, cycle=cycle,
        engine=engine, settings=settings, ecfg=ecfg, fail_reason=fail_reason,
        review_decision=review_decision, review_timeout=review_timeout,
        review_elapsed_ms=review_elapsed_ms,
    )

    return ReviewExceptionResult(
        diag=diag,
        suggestion_text=suggestion_text,
        review_decision=review_decision,
        metrics=metrics,
    )
