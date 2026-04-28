from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

if TYPE_CHECKING:
    from .circuit_breaker import CircuitBreaker

from .errors import get_error_detail

logger = logging.getLogger(__name__)

__all__ = ["RetryPolicy", "should_retry", "get_retry_delay", "prompt_with_retry"]

T = TypeVar("T")

RETRYABLE_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Invalid params", re.IGNORECASE),
    re.compile(r"timeout", re.IGNORECASE),
    re.compile(r"connection reset", re.IGNORECASE),
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"too many requests", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"server error", re.IGNORECASE),
    re.compile(r"internal error", re.IGNORECASE),
]

NON_RETRYABLE_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"directory.*(not found|does not exist)", re.IGNORECASE),
    re.compile(r"permission denied", re.IGNORECASE),
    re.compile(r"authentication failed", re.IGNORECASE),
]


@dataclass
class RetryPolicy:
    max_retries: int = 3
    retry_delay: float = 2.0
    backoff_multiplier: float = 1.5
    max_delay: float = 60.0
    jitter_factor: float = 0.25
    total_timeout: Optional[float] = None


def should_retry(error: Exception | str) -> bool:
    # Short-circuit: bare TimeoutError (even with empty message) is always retryable
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return True

    error_str = str(error)

    for pattern in NON_RETRYABLE_ERROR_PATTERNS:
        if pattern.search(error_str):
            return False

    for pattern in RETRYABLE_ERROR_PATTERNS:
        if pattern.search(error_str):
            return True

    return False


def get_retry_delay(attempt: int, policy: RetryPolicy) -> float:
    base = policy.retry_delay * (policy.backoff_multiplier ** attempt)
    capped = min(base, policy.max_delay)
    if policy.jitter_factor > 0:
        lo = capped * (1 - policy.jitter_factor)
        hi = capped * (1 + policy.jitter_factor)
        return random.uniform(lo, hi)
    return capped


def prompt_with_retry(
    action: Callable[[], T],
    cancel_event: threading.Event,
    *,
    retry_policy: Optional[RetryPolicy] = None,
    before_retry: Optional[Callable[[int, Exception], None]] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    total_timeout: Optional[float] = None,
) -> T:
    from .circuit_breaker import CircuitBreaker as _CB

    policy = retry_policy or RetryPolicy()
    # total_timeout precedence: explicit kwarg > policy field > None (disabled)
    _total_timeout = total_timeout if total_timeout is not None else policy.total_timeout
    last_error: Optional[Exception] = None
    t0 = time.monotonic()

    for attempt in range(policy.max_retries + 1):
        # Check total timeout budget before each attempt
        if _total_timeout is not None and attempt > 0:
            elapsed = time.monotonic() - t0
            if elapsed >= _total_timeout:
                raise TimeoutError(
                    f"prompt_with_retry 总耗时 ({elapsed:.1f}s) 超过上限 ({_total_timeout}s)"
                ) from last_error

        try:
            if circuit_breaker is not None:
                return circuit_breaker.call(action)
            return action()
        except Exception as e:
            last_error = e
            if isinstance(circuit_breaker, _CB):
                from .circuit_breaker import CircuitBreakerOpenException

                if isinstance(e, CircuitBreakerOpenException):
                    raise
            if attempt >= policy.max_retries or not should_retry(e):
                raise
            # Observability: log retry attempt with timing context
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            remaining_budget = (
                round(_total_timeout - (time.monotonic() - t0), 1)
                if _total_timeout is not None
                else None
            )
            logger.info(
                "prompt_with_retry: retry attempt=%d/%d elapsed_ms=%d remaining_budget=%s error=%s",
                attempt + 1,
                policy.max_retries,
                elapsed_ms,
                f"{remaining_budget}s" if remaining_budget is not None else "unlimited",
                get_error_detail(e),
            )
            if before_retry:
                try:
                    before_retry(attempt + 1, e)
                except Exception:
                    logger.debug("before_retry callback failed", exc_info=True)
            delay = max(0.0, float(get_retry_delay(attempt, policy)))
            # Clamp delay if total_timeout budget would be exceeded
            if _total_timeout is not None:
                remaining = _total_timeout - (time.monotonic() - t0)
                if remaining <= 0:
                    raise TimeoutError(
                        f"prompt_with_retry 总耗时超过上限 ({_total_timeout}s)"
                    ) from e
                delay = min(delay, remaining)
            if cancel_event.wait(timeout=delay):
                raise

    if last_error is not None:
        raise last_error
    return action()
