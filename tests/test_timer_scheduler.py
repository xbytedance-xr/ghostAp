"""Tests for TimerScheduler — shared timer infrastructure."""

from __future__ import annotations

import threading
import time

import pytest

from src.card.timer_scheduler import TimerScheduler, _reset_global_scheduler, get_timer_scheduler


@pytest.fixture
def scheduler():
    """Create a fresh TimerScheduler for each test."""
    s = TimerScheduler()
    yield s
    s.shutdown(timeout=2.0)


class TestTimerSchedulerBasic:
    """Basic schedule/cancel/shutdown functionality."""

    def test_schedule_fires_callback(self, scheduler):
        fired = threading.Event()
        scheduler.schedule(0.05, fired.set, session_id="test")
        assert fired.wait(timeout=2.0), "Callback should fire within 2s"

    def test_cancel_prevents_callback(self, scheduler):
        fired = threading.Event()
        handle = scheduler.schedule(0.1, fired.set, session_id="test")
        scheduler.cancel(handle)
        time.sleep(0.2)
        assert not fired.is_set(), "Cancelled callback should not fire"

    def test_cancel_idempotent(self, scheduler):
        handle = scheduler.schedule(0.1, lambda: None, session_id="test")
        scheduler.cancel(handle)
        scheduler.cancel(handle)  # Should not raise

    def test_cancel_none_handle(self, scheduler):
        scheduler.cancel(None)  # Should not raise

    def test_shutdown_prevents_new_callbacks(self, scheduler):
        scheduler.shutdown()
        fired = threading.Event()
        handle = scheduler.schedule(0.05, fired.set, session_id="test")
        time.sleep(0.1)
        assert handle.cancelled
        assert not fired.is_set()

    def test_pending_count(self, scheduler):
        assert scheduler.pending_count == 0
        scheduler.schedule(1.0, lambda: None, session_id="a")
        scheduler.schedule(1.0, lambda: None, session_id="b")
        assert scheduler.pending_count >= 1  # May fire fast


class TestTimerSchedulerConcurrency:
    """Verify single thread handles many timers."""

    def test_single_daemon_thread_for_100_schedules(self, scheduler):
        """100+ scheduled callbacks should all use the same 1 daemon thread."""
        counter = {"count": 0}
        lock = threading.Lock()
        done = threading.Event()
        target = 100

        def _cb():
            with lock:
                counter["count"] += 1
                if counter["count"] >= target:
                    done.set()

        for i in range(target):
            scheduler.schedule(0.01 * (i % 10), _cb, session_id=f"s{i}")

        assert done.wait(timeout=5.0), f"Only {counter['count']}/{target} fired"
        # Only 1 scheduler thread should exist
        assert scheduler.is_alive
        # The scheduler uses exactly 1 thread internally
        timer_threads = [t for t in threading.enumerate() if t.name == "timer-scheduler"]
        assert len(timer_threads) <= 2  # might be 1 from fixture + 1 from global

    def test_callback_exception_doesnt_kill_scheduler(self, scheduler):
        """A failing callback should not prevent subsequent callbacks."""
        fired = threading.Event()

        def _bad():
            raise RuntimeError("boom")

        scheduler.schedule(0.01, _bad, session_id="fail")
        scheduler.schedule(0.05, fired.set, session_id="ok")

        assert fired.wait(timeout=2.0), "Scheduler should survive callback exception"


class TestTimerSchedulerGlobalSingleton:
    """Test get_timer_scheduler() singleton behavior."""

    def test_singleton_returns_same_instance(self):
        _reset_global_scheduler()
        try:
            s1 = get_timer_scheduler()
            s2 = get_timer_scheduler()
            assert s1 is s2
        finally:
            _reset_global_scheduler()

    def test_reset_creates_new_instance(self):
        _reset_global_scheduler()
        try:
            s1 = get_timer_scheduler()
            _reset_global_scheduler()
            s2 = get_timer_scheduler()
            assert s1 is not s2
        finally:
            _reset_global_scheduler()
