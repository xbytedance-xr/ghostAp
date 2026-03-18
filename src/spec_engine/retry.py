import re
from dataclasses import dataclass

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
    return policy.retry_delay * (policy.backoff_multiplier**attempt)
