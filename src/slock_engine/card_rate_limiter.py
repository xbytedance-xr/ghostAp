"""Card update rate limiter for Slock Engine.

Implements a per-message_id token bucket with 'merge latest' strategy
to prevent exceeding Feishu's 1 request/second/card rate limit.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

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
    """

    def __init__(
        self,
        send_fn: CardUpdateFn,
        min_interval: float = 1.0,
    ) -> None:
        self._send_fn = send_fn
        self._min_interval = min_interval
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        # message_id -> last send timestamp (monotonic)
        self._last_sent: dict[str, float] = {}
        # message_id -> pending update (waiting for interval to elapse)
        self._pending: dict[str, _PendingUpdate] = {}
        self._closed = False

    def update(self, message_id: str, payload: dict) -> None:
        """Submit a card update. May send immediately or queue for later."""
        if self._closed:
            return

        with self._lock:
            now = time.monotonic()
            last = self._last_sent.get(message_id, 0.0)
            elapsed = now - last

            if elapsed >= self._min_interval:
                # Safe to send immediately
                self._last_sent[message_id] = now
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

                # Schedule send after remaining interval
                delay = self._min_interval - elapsed
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
            self._last_sent[message_id] = time.monotonic()
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
