"""In-cycle retry logic for the parallel review pipeline.

Extracted from review.py to keep that module focused on orchestration.
Engine layer returns RetryStatus enums; renderer maps them to UI text.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, List, Optional, Type

from ..engine_base import ReviewResult
from ..utils.text import format_friendly_duration
from ..utils.review_diagnostics import normalize_review_diagnostics
from ..utils.review_helpers import compute_adaptive_timeout, compute_retry_delay
from .retry_status import RetryEvent, RetryStatus
from .review_types import RetryTexts, ReviewCircuitState

if TYPE_CHECKING:
    from ..config import Settings
    from ..engine_base import ReviewArtifacts
    from .cycle_budget import CycleBudget
    from .perspective_worker import PerspectiveOutcome

logger = logging.getLogger(__name__)


@dataclass
class RetryConfig:
    """Immutable configuration for a pipeline retry attempt."""

    base_timeout: int
    multiplier: int
    pipeline_fn: Callable[..., List["PerspectiveOutcome"]]
    budget_cls: Type["CycleBudget"]
    artifacts: "ReviewArtifacts"
    agent_type: str
    model_name: Optional[str]


@dataclass
class RetryCallbacks:
    """Mutable event/callback handles for retry lifecycle."""

    cancel_event: Optional[threading.Event] = None
    on_retry_status: Optional[Callable[[RetryEvent], None]] = None
    skip_retry_event: Optional[threading.Event] = None


class PipelineRetryContext:
    """Bundles the parameters that are pass-through from _conduct_review_pipeline
    to _handle_pipeline_errors_with_retry to _attempt_pipeline_retry.

    Composes RetryConfig (immutable) and RetryCallbacks (events/callbacks).
    Individual fields are forwarded as properties for backward compatibility.

    Supports two construction styles:
      - New: PipelineRetryContext(config=RetryConfig(...), callbacks=RetryCallbacks(...))
      - Legacy: PipelineRetryContext(cancel_event=..., base_timeout=..., ...)
    """

    __slots__ = ("config", "callbacks")

    def __init__(
        self,
        *,
        config: Optional[RetryConfig] = None,
        callbacks: Optional[RetryCallbacks] = None,
        # Legacy flat fields (used when config/callbacks are not provided)
        cancel_event: Optional[threading.Event] = None,
        on_retry_status: Optional[Callable[[RetryEvent], None]] = None,
        skip_retry_event: Optional[threading.Event] = None,
        base_timeout: int = 0,
        multiplier: int = 0,
        pipeline_fn: Optional[Callable[..., List["PerspectiveOutcome"]]] = None,
        budget_cls: Optional[Type["CycleBudget"]] = None,
        artifacts: Optional["ReviewArtifacts"] = None,
        agent_type: str = "",
        model_name: Optional[str] = None,
    ):
        if config is not None:
            self.config = config
        else:
            self.config = RetryConfig(
                base_timeout=base_timeout,
                multiplier=multiplier,
                pipeline_fn=pipeline_fn,  # type: ignore[arg-type]
                budget_cls=budget_cls,  # type: ignore[arg-type]
                artifacts=artifacts,  # type: ignore[arg-type]
                agent_type=agent_type,
                model_name=model_name,
            )
        if callbacks is not None:
            self.callbacks = callbacks
        else:
            self.callbacks = RetryCallbacks(
                cancel_event=cancel_event,
                on_retry_status=on_retry_status,
                skip_retry_event=skip_retry_event,
            )

    # --- backward-compatible field access ---

    @property
    def cancel_event(self) -> Optional[threading.Event]:
        return self.callbacks.cancel_event

    @property
    def on_retry_status(self) -> Optional[Callable[[RetryEvent], None]]:
        return self.callbacks.on_retry_status

    @property
    def skip_retry_event(self) -> Optional[threading.Event]:
        return self.callbacks.skip_retry_event

    @property
    def base_timeout(self) -> int:
        return self.config.base_timeout

    @property
    def multiplier(self) -> int:
        return self.config.multiplier

    @property
    def pipeline_fn(self) -> Callable[..., List["PerspectiveOutcome"]]:
        return self.config.pipeline_fn

    @property
    def budget_cls(self) -> Type["CycleBudget"]:
        return self.config.budget_cls

    @property
    def artifacts(self) -> "ReviewArtifacts":
        return self.config.artifacts

    @property
    def agent_type(self) -> str:
        return self.config.agent_type

    @property
    def model_name(self) -> Optional[str]:
        return self.config.model_name


def attempt_pipeline_retry(
    *,
    circuit: ReviewCircuitState,
    settings: "Settings",
    cycle: int,
    ctx: PipelineRetryContext,
) -> Optional[List]:
    """Attempt an in-cycle retry after all workers timed out.

    Returns the successful retry outcomes, or ``None`` if the retry was
    cancelled, failed, or produced errors.
    """
    _raw = settings.spec_review_retry_max_attempts
    max_attempts = max(0, int(_raw)) if _raw is not None else 2
    if max_attempts < 1:
        return None

    _max_delay = float(settings.spec_review_retry_max_delay)
    _base_delay = float(getattr(settings, "spec_review_retry_base_delay", 5.0) or 5.0)
    _decay_factor = float(getattr(settings, "spec_review_retry_decay_factor", 1.5) or 1.5)
    _delay = compute_retry_delay(
        int(circuit.consecutive_timeouts or 0),  # type: ignore[union-attr]
        base_delay=_base_delay,
        max_delay=_max_delay,
        decay_factor=_decay_factor,
    )

    logger.info(
        "[Spec] all workers timed out, scheduling in-cycle retry: delay=%.1fs cycle=%d",
        _delay, cycle,
    )

    _cancel = ctx.cancel_event

    for attempt in range(max_attempts):
        # Check cancel before starting attempt
        if _cancel is not None and _cancel.is_set():
            logger.info("[Spec] in-cycle retry cancelled before attempt: cycle=%d", cycle)
            return None

        # Push wait-phase status BEFORE the delay (so user sees immediate feedback).
        if ctx.on_retry_status and _delay >= 2:
            try:
                ctx.on_retry_status(RetryEvent(
                    status=RetryStatus.WAITING,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                    delay_sec=_delay,
                ))
            except Exception:
                logger.debug("on_retry_status callback failed", exc_info=True)

        # Interruptible wait: use Event.wait if cancel_event exists, else time.sleep.
        # skip_retry_event allows skipping the wait without cancelling the retry entirely.
        _skip = ctx.skip_retry_event
        if _skip is not None:
            _skip.clear()  # reset for this attempt
        if _cancel is not None:
            if _skip is not None:
                # Poll both events: cancel aborts, skip proceeds immediately
                _elapsed = 0.0
                _poll_interval = 0.5
                while _elapsed < _delay:
                    if _cancel.is_set():
                        logger.info("[Spec] in-cycle retry cancelled during wait: cycle=%d", cycle)
                        return None
                    if _skip.is_set():
                        logger.info("[Spec] skip_retry triggered, proceeding immediately: cycle=%d", cycle)
                        break
                    _wait_time = min(_poll_interval, _delay - _elapsed)
                    _cancel.wait(timeout=_wait_time)
                    _elapsed += _wait_time
            else:
                if _cancel.wait(timeout=_delay):
                    logger.info("[Spec] in-cycle retry cancelled during wait: cycle=%d", cycle)
                    return None
        elif _skip is not None:
            if _skip.wait(timeout=_delay):
                logger.info("[Spec] skip_retry triggered, proceeding immediately: cycle=%d", cycle)
        else:
            time.sleep(_delay)

        # Push execute-phase status AFTER the delay (always, regardless of delay length).
        if ctx.on_retry_status:
            try:
                ctx.on_retry_status(RetryEvent(
                    status=RetryStatus.EXECUTING,
                    attempt=attempt + 1,
                    max_attempts=max_attempts,
                ))
            except Exception:
                logger.debug("on_retry_status callback failed", exc_info=True)

        _retry_base = compute_adaptive_timeout(
            int(circuit.consecutive_timeouts or 0) + 1,  # type: ignore[union-attr]
            base_timeout=ctx.base_timeout,
            min_timeout=settings.spec_review_min_timeout,
            hard_floor=settings.spec_review_hard_floor,
        )
        _retry_budget_s = float(_retry_base * max(2, ctx.multiplier + 2))
        _retry_budget = ctx.budget_cls(
            total_seconds=_retry_budget_s,
            label=f"spec_review_retry_c{cycle}_a{attempt}",
        )
        try:
            retry_outcomes = ctx.pipeline_fn(
                ctx.artifacts,
                _retry_budget,
                agent_type=ctx.agent_type,
                model_name=ctx.model_name,
            )
            retry_has_errors = any(
                o.error and o.error != "lint_gate_short_circuit"
                for o in retry_outcomes
            )
            if not retry_has_errors:
                logger.info("[Spec] in-cycle retry succeeded: cycle=%d attempt=%d", cycle, attempt)
                # Notify caller of success before returning
                if ctx.on_retry_status:
                    try:
                        ctx.on_retry_status(RetryEvent(
                            status=RetryStatus.SUCCEEDED,
                            attempt=attempt + 1,
                            max_attempts=max_attempts,
                        ))
                    except Exception:
                        logger.debug("on_retry_status callback failed", exc_info=True)
                return retry_outcomes
            logger.info("[Spec] in-cycle retry still has errors: cycle=%d attempt=%d", cycle, attempt)
        except Exception as retry_exc:
            logger.warning(
                "[Spec] in-cycle retry failed with exception: cycle=%d attempt=%d err=%s",
                cycle, attempt, repr(retry_exc),
            )
    return None


def build_retry_diagnostics(
    *,
    outcomes: List,
    failed_workers: List,
    circuit: ReviewCircuitState,
    settings: "Settings",
    cycle: int,
    err_type_val: str,
    all_timeout: bool,
    retry_attempted: bool,
    retry_texts: RetryTexts,
) -> dict:
    """Build diagnostics dict for failed pipeline review (pure function).

    Does NOT mutate *circuit* — caller is responsible for updating circuit
    state via circuit.on_failure().

    Args:
        retry_texts: Typed dict with keys "retry_no_retry" and "retry_exhausted".
    """
    from .perspective_worker import ReviewErrorCode

    _txt_no_retry = retry_texts["retry_no_retry"]
    _txt_exhausted = retry_texts["retry_exhausted"]

    if all_timeout:
        if retry_attempted:
            _actual_attempts = max(0, int(settings.spec_review_retry_max_attempts))
            if _actual_attempts == 0:
                err_type_val = _txt_no_retry
            else:
                _elapsed_sec = int(settings.spec_review_timeout + settings.spec_review_retry_max_delay * _actual_attempts)
                err_type_val = _txt_exhausted.format(n=_actual_attempts, elapsed_friendly=format_friendly_duration(_elapsed_sec))
        else:
            # Not attempted — regardless of max_attempts config, report as "no retry"
            err_type_val = _txt_no_retry
    elif failed_workers and failed_workers[0].error_code == ReviewErrorCode.TIMEOUT:
        err_type_val = _txt_no_retry

    diag = normalize_review_diagnostics({
        "phase": "review",
        "role": "pipeline_parallel",
        "cycle": int(cycle or 0),
        "decision": "review_failed_continue",
        "fail_reason": "worker_errors",
        "err_type": err_type_val,
        "err_repr": "; ".join(o.error or "" for o in failed_workers[:3]),
        "error_text": f"{len(failed_workers)}/{len(outcomes)} workers failed",
        "cycle_number": int(cycle or 0),
        "exception_type": "PipelineWorkerErrors",
        "review_role": "pipeline_parallel",
        "traceback_snippet": "",
        "consecutive_failures": int(circuit.review_failure_consecutive or 0),  # type: ignore[union-attr]
    })
    # Attach retry info after normalize (not in stable keys whitelist).
    diag["retry_attempted"] = retry_attempted
    diag["retry_succeeded"] = False

    return diag


def outcomes_to_review_result(
    outcomes: List,
    cycle: int,
) -> ReviewResult:
    """Convert parallel pipeline outcomes to the unified ReviewResult."""
    return ReviewResult(
        reviews=[o.review for o in outcomes],
        iteration=cycle,
    )


def handle_pipeline_errors_with_retry(
    *,
    outcomes: List,
    review_result: ReviewResult,
    circuit: ReviewCircuitState,
    settings: "Settings",
    cycle: int,
    ctx: PipelineRetryContext,
    retry_texts: RetryTexts,
) -> tuple[ReviewResult, "Optional[dict]"]:
    """Handle pipeline worker errors, optionally retrying on full timeout.

    Extracted from ``_conduct_review_pipeline`` to reduce nesting depth.
    Uses ``circuit.on_failure(all_timeout)`` for state transitions.

    Returns:
        A ``(review_result, diag)`` tuple where *diag* is ``None`` when
        the retry succeeded and the circuit was reset, or a diagnostics
        dict to assign to ``circuit.last_review_failure_diag`` otherwise.
    """
    from .perspective_worker import ReviewErrorCode

    _wall_t0 = time.monotonic()

    failed_workers = [o for o in outcomes if o.error and o.error != "lint_gate_short_circuit"]
    err_type_val = failed_workers[0].error if failed_workers else "unknown"
    all_timeout = bool(failed_workers) and all(
        o.error_code == ReviewErrorCode.TIMEOUT for o in failed_workers
    )

    # --- In-cycle delayed retry on full timeout ---
    _retry_attempted = False
    _retry_succeeded = False
    if all_timeout and settings.spec_review_retry_max_attempts > 0:
        _retry_attempted = True
        retry_result = attempt_pipeline_retry(
            circuit=circuit, settings=settings,
            cycle=cycle, ctx=ctx,
        )
        if retry_result is not None and retry_result:
            _retry_succeeded = True
            outcomes = retry_result
            review_result = outcomes_to_review_result(outcomes, cycle)
            circuit.reset_on_success()  # type: ignore[union-attr]

    # Retry succeeded — no diagnostics needed.
    if _retry_succeeded:
        return review_result, None

    # --- Retry didn't succeed (or wasn't attempted): build diagnostics ---
    diag = build_retry_diagnostics(
        outcomes=outcomes,
        failed_workers=failed_workers,
        circuit=circuit,
        settings=settings,
        cycle=cycle,
        err_type_val=err_type_val,
        all_timeout=all_timeout,
        retry_attempted=_retry_attempted,
        retry_texts=retry_texts,
    )

    # Enrich diagnostics with wall-clock and retry detail for post-mortem analysis.
    diag["total_wall_clock_ms"] = int((time.monotonic() - _wall_t0) * 1000)
    _max_attempts = max(0, int(settings.spec_review_retry_max_attempts))
    diag["retry_attempts_detail"] = {
        "max_attempts": _max_attempts,
        "attempted": _retry_attempted,
        "succeeded": False,
        "all_timeout": all_timeout,
    }

    # --- Circuit state mutation (symmetric with reset_on_success) ---
    circuit.on_failure(all_timeout)  # type: ignore[union-attr]

    # --- Emit terminal retry status for renderer visibility ---
    if ctx.on_retry_status:
        try:
            if _retry_attempted:
                # Retried but exhausted all attempts
                ctx.on_retry_status(RetryEvent(
                    status=RetryStatus.EXHAUSTED,
                    max_attempts=int(settings.spec_review_retry_max_attempts),
                    message=str(settings.spec_review_retry_max_attempts),
                ))
            else:
                # Decided not to retry (disabled or non-timeout failure)
                ctx.on_retry_status(RetryEvent(
                    status=RetryStatus.NO_RETRY,
                ))
        except Exception:
            logger.debug("on_retry_status callback failed", exc_info=True)

    # Overwrite user-facing suggestions with recovery guidance when retry was attempted.
    if _retry_attempted and all_timeout:
        for pr in review_result.reviews:
            pr.suggestions = [diag.get("err_type", err_type_val)]

    return review_result, diag
