"""LazyCardChannel — Adapter for rate-limited card delivery with retry.

Extracted from engine.py to be a standalone, testable, PEP-8 compliant class.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class LazyCardChannel:
    """Adapter that delegates card send/update to engine callbacks with retry.

    Distinguishes between retryable errors (network timeouts / exceptions)
    and non-retryable results (business logic returning None on first call).
    """

    _MAX_RETRIES = 3
    _BASE_DELAY = 0.3  # seconds

    def __init__(
        self,
        send_fn_getter: Callable[[], Optional[Callable[..., Any]]],
        update_fn_getter: Callable[[], Optional[Callable[..., Any]]],
    ) -> None:
        """Initialize with lazy function getters.

        Args:
            send_fn_getter: Returns the current send_card callback (or None).
            update_fn_getter: Returns the current update_card callback (or None).
        """
        self._send_fn_getter = send_fn_getter
        self._update_fn_getter = update_fn_getter

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
        """Retry with exponential backoff, distinguishing error types.

        - Exceptions (network timeout, connection error): retry up to MAX_RETRIES
        - fn returning None/False on first attempt: do NOT retry (business logic)
        """
        for attempt in range(self._MAX_RETRIES):
            try:
                result = fn(*args)
                if result is not None and result is not False:
                    return result
                # Business logic returned None/False — don't retry
                if attempt == 0:
                    return result
                # On subsequent attempts after exception recovery, treat as final
                return result
            except (OSError, TimeoutError, ConnectionError) as e:
                # Retryable network errors
                if attempt == self._MAX_RETRIES - 1:
                    logger.warning(
                        "Card callback failed after %d retries: %s",
                        self._MAX_RETRIES, e,
                    )
                    return None
                time.sleep(self._BASE_DELAY * (2 ** attempt))
            except Exception as e:
                # Non-retryable application errors — fail immediately
                logger.warning("Card callback raised non-retryable error: %s", str(e))
                return None
        return None
