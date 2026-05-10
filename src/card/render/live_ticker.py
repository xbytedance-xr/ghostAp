"""Live ticker frame scheduler for lightweight card status animation."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from src.card.timers.scheduler import TimerHandle, TimerScheduler, get_timer_scheduler

DEFAULT_TICKER_FRAMES: tuple[str, ...] = ("🟢", "⚪")
# v2 design: 1.2 s frame interval matches the CSS `animation: blink 1.2s infinite` in UX mockups.
# Feishu Schema 2.0 has no CSS animation; we simulate it with emoji frame switching at this cadence.
DEFAULT_TICKER_INTERVAL: float = 1.2

# Frozen (archived) cards display a static pause marker instead of the last animation frame.
FROZEN_FRAME: str = "⏸"


class _Scheduler(Protocol):
    def schedule(self, delay: float, callback: Callable[[], None], *, session_id: str = "") -> TimerHandle: ...

    def cancel(self, handle: TimerHandle) -> None: ...


def frame_for_tick(tick: int, frames: Sequence[str] = DEFAULT_TICKER_FRAMES) -> str:
    """Return the ticker frame for an integer tick."""
    if not frames:
        return ""
    return frames[max(0, tick) % len(frames)]


@dataclass
class LiveTicker:
    """Small repeating timer that emits one visual frame per interval.

    The callback must stay lightweight; callers that need network I/O should
    enqueue their own work from the callback instead of blocking the shared
    timer thread.
    """

    session_id: str
    on_frame: Callable[[str], None]
    interval: float = 1.2
    frames: Sequence[str] = DEFAULT_TICKER_FRAMES
    scheduler: _Scheduler | None = None

    def __post_init__(self) -> None:
        self._scheduler: _Scheduler = self.scheduler or get_timer_scheduler()
        self._handle: TimerHandle | None = None
        self._tick = 0
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self, *, emit_now: bool = True) -> None:
        """Start emitting frames until stopped."""
        if self._running:
            return
        self._running = True
        if emit_now:
            self._emit_frame()
        self._schedule_next()

    def stop(self) -> None:
        """Stop future emissions and cancel the pending timer."""
        self._running = False
        if self._handle is not None:
            self._scheduler.cancel(self._handle)
            self._handle = None

    def _schedule_next(self) -> None:
        if not self._running:
            return
        delay = max(0.1, float(self.interval))
        self._handle = self._scheduler.schedule(delay, self._on_timer, session_id=self.session_id)

    def _on_timer(self) -> None:
        if not self._running:
            return
        self._emit_frame()
        self._schedule_next()

    def _emit_frame(self) -> None:
        frame = frame_for_tick(self._tick, self.frames)
        self._tick += 1
        if frame:
            self.on_frame(frame)
