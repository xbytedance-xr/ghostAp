"""Tests for BuildHeartbeat and its integration with SpecStreamProcessor."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.card.render.build_heartbeat import BuildHeartbeat


class FakeTimerHandle:
    def __init__(self):
        self._cancelled = False

    @property
    def cancelled(self):
        return self._cancelled


class FakeScheduler:
    """Deterministic scheduler for testing — fires callbacks immediately on demand."""

    def __init__(self):
        self.scheduled: list[tuple[float, object]] = []
        self._cancelled: set[int] = set()

    def schedule(self, delay, callback, *, session_id=""):
        handle = FakeTimerHandle()
        self.scheduled.append((delay, callback, handle))
        return handle

    def cancel(self, handle):
        handle._cancelled = True

    def fire_next(self):
        if self.scheduled:
            _, cb, handle = self.scheduled.pop(0)
            if not handle._cancelled:
                cb()


def test_heartbeat_start_stop():
    scheduler = FakeScheduler()
    ticks = []

    hb = BuildHeartbeat(session_id="test-1", on_tick=lambda e, a: ticks.append((e, a)), interval=5.0)
    hb._scheduler = scheduler
    hb.start()

    assert hb.running is True
    assert len(scheduler.scheduled) == 1

    hb.stop()
    assert hb.running is False


def test_heartbeat_fires_tick():
    scheduler = FakeScheduler()
    ticks = []

    hb = BuildHeartbeat(session_id="test-2", on_tick=lambda e, a: ticks.append((e, a)), interval=5.0)
    hb._scheduler = scheduler
    hb.start()

    # Simulate time passing
    hb._last_event_at = time.monotonic() - 10.0
    scheduler.fire_next()

    assert len(ticks) == 1
    elapsed, activity = ticks[0]
    assert elapsed >= 9.0
    assert activity == "thinking"


def test_heartbeat_reset_updates_activity():
    scheduler = FakeScheduler()
    ticks = []

    hb = BuildHeartbeat(session_id="test-3", on_tick=lambda e, a: ticks.append((e, a)), interval=5.0)
    hb._scheduler = scheduler
    hb.start()

    hb.reset("tool_running")
    hb._last_event_at = time.monotonic() - 7.0
    scheduler.fire_next()

    assert len(ticks) == 1
    _, activity = ticks[0]
    assert activity == "tool_running"


def test_heartbeat_stop_prevents_further_ticks():
    scheduler = FakeScheduler()
    ticks = []

    hb = BuildHeartbeat(session_id="test-4", on_tick=lambda e, a: ticks.append((e, a)), interval=5.0)
    hb._scheduler = scheduler
    hb.start()
    hb.stop()

    # Try firing — should be no-op due to _running=False
    if scheduler.scheduled:
        scheduler.fire_next()

    assert len(ticks) == 0


def test_heartbeat_reschedules_after_tick():
    scheduler = FakeScheduler()
    ticks = []

    hb = BuildHeartbeat(session_id="test-5", on_tick=lambda e, a: ticks.append((e, a)), interval=5.0)
    hb._scheduler = scheduler
    hb.start()

    hb._last_event_at = time.monotonic() - 10.0
    scheduler.fire_next()  # fires tick and reschedules

    assert len(ticks) == 1
    assert len(scheduler.scheduled) == 1  # rescheduled


def test_heartbeat_double_start_is_noop():
    scheduler = FakeScheduler()
    hb = BuildHeartbeat(session_id="test-6", on_tick=lambda e, a: None, interval=5.0)
    hb._scheduler = scheduler
    hb.start()
    hb.start()  # should not schedule a second timer
    assert len(scheduler.scheduled) == 1


@patch("src.card.render.build_heartbeat.get_timer_scheduler")
def test_heartbeat_uses_global_scheduler(mock_get_scheduler):
    mock_scheduler = MagicMock()
    mock_get_scheduler.return_value = mock_scheduler
    mock_scheduler.schedule.return_value = FakeTimerHandle()

    hb = BuildHeartbeat(session_id="test-7", on_tick=lambda e, a: None, interval=3.0)
    hb.start()

    mock_scheduler.schedule.assert_called_once()
    delay = mock_scheduler.schedule.call_args[0][0]
    assert delay == 3.0


def test_heartbeat_with_real_scheduler():
    """Integration test: heartbeat fires with real TimerScheduler."""
    ticks = []
    event = threading.Event()

    def on_tick(elapsed, activity):
        ticks.append((elapsed, activity))
        event.set()

    hb = BuildHeartbeat(session_id="integration-1", on_tick=on_tick, interval=0.2)
    hb.start()
    # Pretend 5s since last event (after start so it's not overwritten)
    hb._last_event_at = time.monotonic() - 5.0

    # Wait for at least one tick
    fired = event.wait(timeout=2.0)
    hb.stop()

    assert fired, "Heartbeat did not fire within timeout"
    assert len(ticks) >= 1
    assert ticks[0][0] >= 4.5  # should report ~5s elapsed
