"""LazyCardChannel — Adapter for rate-limited card delivery with retry.

Extracted from engine.py to be a standalone, testable, PEP-8 compliant class.
Uses the shared RetryPolicy/get_retry_delay from src.utils.retry for backoff.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from ..utils.retry import RetryPolicy, get_retry_delay

logger = logging.getLogger(__name__)

# Policy tuned for card delivery: fast retries with short delays.
_CARD_RETRY_POLICY = RetryPolicy(
    max_retries=3,
    retry_delay=0.3,
    backoff_multiplier=2.0,
    max_delay=5.0,
    jitter_factor=0.0,
)


class LazyCardChannel:
    """Adapter that delegates card send/update to engine callbacks with retry.

    Distinguishes between retryable errors (network timeouts / exceptions)
    and non-retryable results (business logic returning None on first call).
    """

    def __init__(
        self,
        send_fn_getter: Callable[[], Optional[Callable[..., Any]]],
        update_fn_getter: Callable[[], Optional[Callable[..., Any]]],
        *,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> None:
        """Initialize with lazy function getters.

        Args:
            send_fn_getter: Returns the current send_card callback (or None).
            update_fn_getter: Returns the current update_card callback (or None).
            retry_policy: Optional override for backoff parameters.
        """
        self._send_fn_getter = send_fn_getter
        self._update_fn_getter = update_fn_getter
        self._policy = retry_policy or _CARD_RETRY_POLICY

    def send_card(self, card: Any, *, reply_to: Optional[str] = None) -> Optional[str]:
        """Send a new card. Returns message_id or None."""
        fn = self._send_fn_getter()
        if not fn:
            return None
        return self._retry(fn, card)

    def update_card(self, message_id: str, card: Any) -> bool:
        """Update an existing card by message_id. Returns success."""
        fn = self._update_fn_getter()
        if not fn:
            return False
        result = self._retry(fn, message_id, card)
        return bool(result)

    def _retry(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Retry with exponential backoff via shared RetryPolicy.

        - Exceptions (network timeout, connection error): retry up to max_retries
        - fn returning None/False on first attempt: do NOT retry (business logic)
        """
        for attempt in range(self._policy.max_retries):
            try:
                result = fn(*args)
                if result is not None and result is not False:
                    return result
                # Business logic returned None/False — don't retry
                return result
            except (OSError, TimeoutError, ConnectionError) as e:
                # Retryable network errors
                if attempt == self._policy.max_retries - 1:
                    logger.warning(
                        "Card callback failed after %d retries: %s",
                        self._policy.max_retries, e,
                    )
                    return None
                delay = get_retry_delay(attempt, self._policy)
                time.sleep(delay)
            except Exception as e:
                # Non-retryable application errors — fail immediately
                logger.warning("Card callback raised non-retryable error: %s", str(e))
                return None
        return None
