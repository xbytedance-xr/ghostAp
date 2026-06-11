"""Periodic heartbeat for Spec BUILD phase — keeps the card footer alive.

During BUILD, the agent primarily executes tool calls with long gaps between
events. This heartbeat dispatches a lightweight progress_updated CardEvent
every N seconds so the Feishu card shows elapsed time and activity status.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from src.card.timers.scheduler import TimerHandle, get_timer_scheduler

DEFAULT_HEARTBEAT_INTERVAL: float = 5.0


@dataclass
class BuildHeartbeat:
    """Repeating timer that emits elapsed-time progress during BUILD phase."""

    session_id: str
    on_tick: Callable[[float, str], None]
    interval: float = DEFAULT_HEARTBEAT_INTERVAL

    def __post_init__(self) -> None:
        self._scheduler = get_timer_scheduler()
        self._handle: TimerHandle | None = None
        self._running = False
        self._last_event_at: float = time.monotonic()
        self._activity: str = "thinking"

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._last_event_at = time.monotonic()
        self._schedule_next()

    def stop(self) -> None:
        self._running = False
        if self._handle is not None:
            self._scheduler.cancel(self._handle)
            self._handle = None

    def reset(self, activity: str = "thinking") -> None:
        """Reset idle timer on receiving a new ACP event."""
        self._last_event_at = time.monotonic()
        self._activity = activity

    def _schedule_next(self) -> None:
        if not self._running:
            return
        delay = max(0.5, float(self.interval))
        self._handle = self._scheduler.schedule(
            delay, self._on_timer, session_id=self.session_id
        )

    def _on_timer(self) -> None:
        if not self._running:
            return
        elapsed = time.monotonic() - self._last_event_at
        self.on_tick(elapsed, self._activity)
        self._schedule_next()
