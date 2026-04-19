"""Shared review exception-handling helpers for SpecEngine & LoopEngine.

Centralizes:
- Fallback suggestion text generation (timeout / empty / normal errors)
- Exponential-backoff cooldown computation for circuit breakers
- Adaptive (progressive) review timeout computation
"""


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
) -> int:
    """Compute progressively shorter review timeout after consecutive timeouts.

    Formula: ``max(base_timeout // 2**n, min_timeout)``

    Examples (base=120, min=30):
        n=0 → 120
        n=1 → 60
        n=2 → 30
        n=3 → 30  (floored)
    """
    n = max(0, int(consecutive_timeouts))
    return max(base_timeout // (2 ** n), min_timeout)
