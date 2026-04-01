from __future__ import annotations

import logging
import random
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional, TypeVar

if TYPE_CHECKING:
    from .circuit_breaker import CircuitBreaker

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


def should_retry(error: Exception | str) -> bool:
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
) -> T:
    from .circuit_breaker import CircuitBreaker as _CB

    policy = retry_policy or RetryPolicy()
    last_error: Optional[Exception] = None

    for attempt in range(policy.max_retries + 1):
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
            if before_retry:
                try:
                    before_retry(attempt + 1, e)
                except Exception:
                    pass
            delay = max(0.0, float(get_retry_delay(attempt, policy)))
            if cancel_event.wait(timeout=delay):
                raise

    if last_error is not None:
        raise last_error
    return action()
