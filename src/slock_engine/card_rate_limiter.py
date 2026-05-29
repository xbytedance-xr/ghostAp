"""Card update rate limiter for Slock Engine.

Implements a per-message_id throttle with 'merge latest' strategy
to prevent exceeding Feishu's 1 request/second/card rate limit.

Uses TokenBucketLimiter from src.utils.rate_limit as the underlying
rate-limiting primitive (capacity=1, fill_rate=1/min_interval).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..utils.rate_limit import TokenBucketLimiter

logger = logging.getLogger(__name__)

# Type alias for the actual send function: (message_id, payload) -> bool
CardUpdateFn = Callable[[str, dict], bool]


@dataclass
class _PendingUpdate:
    """Tracks a pending card update for a specific message_id."""

    payload: dict
    timer: Optional[threading.Timer] = field(default=None, repr=False)


class CardRateLimiter:
    """Per-message_id rate limiter with 'merge latest' strategy.

    - Minimum interval between updates to the same message_id is `min_interval` seconds.
    - If a new update arrives while one is pending, the old payload is discarded
      and replaced with the new one (merge latest).
    - Thread-safe via a single lock.
    - Uses TokenBucketLimiter (capacity=1, fill_rate=1/min_interval) per message_id.
    """

    def __init__(
        self,
        send_fn: CardUpdateFn,
        min_interval: float = 1.0,
    ) -> None:
        self._send_fn = send_fn
        self._min_interval = min_interval
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        # message_id -> TokenBucketLimiter (capacity=1 token, refills at 1/min_interval)
        self._buckets: dict[str, TokenBucketLimiter] = {}
        # message_id -> pending update (waiting for interval to elapse)
        self._pending: dict[str, _PendingUpdate] = {}
        self._closed = False

    def _get_bucket(self, message_id: str) -> TokenBucketLimiter:
        """Get or create the token bucket for a message_id."""
        bucket = self._buckets.get(message_id)
        if bucket is None:
            bucket = TokenBucketLimiter(capacity=3, fill_rate=1.0 / self._min_interval)
            self._buckets[message_id] = bucket
        return bucket

    def update(self, message_id: str, payload: dict) -> None:
        """Submit a card update. May send immediately or queue for later."""
        if self._closed:
            return

        with self._lock:
            bucket = self._get_bucket(message_id)

            if bucket.acquire():
                # Token available — send immediately
                self._send_now(message_id, payload)
            else:
                # Must wait — replace any existing pending payload (merge latest)
                pending = self._pending.get(message_id)
                if pending is not None:
                    # Cancel old timer, replace payload
                    if pending.timer is not None:
                        pending.timer.cancel()
                    pending.payload = payload
                else:
                    pending = _PendingUpdate(payload=payload)
                    self._pending[message_id] = pending

                # Schedule send after min_interval
                # The bucket will have refilled by then.
                delay = self._min_interval
                timer = threading.Timer(delay, self._flush_one, args=(message_id,))
                timer.daemon = True
                pending.timer = timer
                timer.start()

    def _send_now(self, message_id: str, payload: dict) -> None:
        """Execute the actual send (outside lock if possible for performance)."""
        try:
            self._send_fn(message_id, payload)
        except Exception:
            logger.exception("CardRateLimiter: send failed for msg_id=%s", message_id)

    def _flush_one(self, message_id: str) -> None:
        """Timer callback: send the latest pending payload for a message_id."""
        if self._closed:
            return

        with self._lock:
            pending = self._pending.pop(message_id, None)
            if pending is None:
                return
            bucket = self._get_bucket(message_id)
            # Force-acquire to mark the bucket as just-used
            bucket.acquire()
            payload = pending.payload

        # Send outside lock to avoid holding lock during I/O
        self._send_now(message_id, payload)

    def flush_all(self) -> None:
        """Flush all pending updates immediately. Call on engine shutdown."""
        with self._lock:
            self._closed = True
            pending_items = list(self._pending.items())
            self._pending.clear()

        for message_id, pending in pending_items:
            if pending.timer is not None:
                pending.timer.cancel()
            self._send_now(message_id, pending.payload)

    @property
    def pending_count(self) -> int:
        """Number of message_ids with pending updates (for monitoring)."""
        with self._lock:
            return len(self._pending)

