"""Stream throttle for engine renderers.

Extracted from BaseRenderer for single-responsibility — controls how often
streaming text/plan updates are forwarded to the card delivery layer.
"""

from __future__ import annotations

import time
from typing import Optional


class StreamThrottle:
    """Lightweight stream/plan throttle for engine renderers.

    NOT thread-safe. Caller must guarantee single-thread access per instance.

    Throttle semantics:
    - check_throttle(text_len, force) → bool
    - update_stream_state(text_len)
    - check_plan_throttle(plan_content, force) → bool
    - update_plan_state(plan_content)
    """

    def __init__(self, min_interval: float, min_chars: int, *, mobile_min_interval: float | None = None) -> None:
        self._min_interval = min_interval
        self._mobile_min_interval = mobile_min_interval or min_interval * 1.5
        self._min_chars = min_chars
        self.last_stream_ts: float = 0.0
        self.last_stream_text_len: int = 0
        self.last_plan_ts: float = 0.0
        self.last_plan_content: str = ""

    def check_throttle(
        self,
        text_len: int,
        force: bool = False,
        min_interval: Optional[float] = None,
        min_new_chars: Optional[int] = None,
        mobile: bool = False,
    ) -> bool:
        """Return True if update should proceed, False if throttled.

        Adaptive low-rate logic: when generation rate < 50 chars/s, use 0.8s
        interval instead of the configured interval to provide faster feedback
        during slow generation.

        Args:
            mobile: If True, use the mobile_min_interval (longer) to reduce
                API pressure from mobile clients with limited display area.
        """
        if force:
            return True
        now = time.monotonic()
        if min_interval is None:
            min_interval = self._mobile_min_interval if mobile else self._min_interval
        if min_new_chars is None:
            min_new_chars = self._min_chars

        # Adaptive low-rate: use shorter interval when generation is slow
        # Only activates when using default interval (not caller-overridden)
        # and only reduces interval (never increases it)
        elapsed = now - self.last_stream_ts
        new_chars = text_len - self.last_stream_text_len
        if elapsed > 0 and self.last_stream_ts > 0 and min_interval > 0.8:
            rate = new_chars / elapsed
            if rate < 50:
                min_interval = 0.8

        if elapsed < min_interval and new_chars < min_new_chars:
            return False
        return True

    def update_stream_state(self, text_len: int) -> None:
        """Update throttle state after a stream update."""
        self.last_stream_ts = time.monotonic()
        self.last_stream_text_len = text_len

    def check_plan_throttle(self, plan_content: str, force: bool = False, min_interval: float = 1.5) -> bool:
        """Return True if plan update should proceed."""
        if force:
            return True
        now = time.monotonic()
        if plan_content and (plan_content != self.last_plan_content or (now - self.last_plan_ts) > min_interval):
            return True
        return False

    def update_plan_state(self, plan_content: str) -> None:
        """Update plan throttle state."""
        self.last_plan_ts = time.monotonic()
        self.last_plan_content = plan_content
