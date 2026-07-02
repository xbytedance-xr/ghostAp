"""Tests for ProgressCoalescer — debounced progress card updates."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from src.workflow_engine.progress_coalescer import ProgressCoalescer


@pytest.fixture
def make_coalescer():
    """Factory fixture that tracks all created coalescers and stops them on teardown.

    Ensures daemon threads started by ProgressCoalescer are cleaned up even if
    a test fails mid-execution, preventing thread accumulation across the test run.
    """
    created: list[ProgressCoalescer] = []

    def _make(**kwargs) -> ProgressCoalescer:
        c = ProgressCoalescer(**kwargs)
        created.append(c)
        return c

    yield _make

    for c in created:
        try:
            c.stop()
        except Exception:
            pass


class TestProgressCoalescerBasic:
    """Basic functionality tests."""

    def test_enqueue_and_deliver(self, make_coalescer):
        """Enqueued snapshot is delivered to callback within debounce window."""
        callback = MagicMock()
        coalescer = make_coalescer(on_progress=callback, debounce_s=0.02)
        coalescer.enqueue({"phase": "test", "count": 1})
        # Wait for debounce + some margin
        time.sleep(0.06)
        assert callback.call_count >= 1
        # Latest snapshot delivered
        last_call = callback.call_args_list[-1]
        assert last_call[0][0] == {"phase": "test", "count": 1}

    def test_coalescing_only_latest_delivered(self, make_coalescer):
        """Rapid enqueues within one window deliver only the latest snapshot."""
        callback = MagicMock()
        coalescer = make_coalescer(on_progress=callback, debounce_s=0.05)
        # Enqueue 5 snapshots rapidly
        for i in range(5):
            coalescer.enqueue({"count": i})
        # Wait for one debounce cycle
        time.sleep(0.12)
        # Should have fired once (or at most twice due to timing)
        assert 1 <= callback.call_count <= 2
        # The latest value should be count=4
        last_call = callback.call_args_list[-1]
        assert last_call[0][0]["count"] == 4

    def test_stop_flushes_pending(self, make_coalescer):
        """stop() forces delivery of any pending snapshot."""
        callback = MagicMock()
        coalescer = make_coalescer(on_progress=callback, debounce_s=5.0)
        # Enqueue but don't wait — debounce is 5s
        coalescer.enqueue({"flushed": True})
        time.sleep(0.01)  # Ensure enqueue completes
        # stop() should force flush immediately
        coalescer.stop()
        assert callback.call_count >= 1
        last_call = callback.call_args_list[-1]
        assert last_call[0][0] == {"flushed": True}

    def test_stop_idempotent(self, make_coalescer):
        """Calling stop() multiple times is safe."""
        callback = MagicMock()
        coalescer = make_coalescer(on_progress=callback, debounce_s=0.1)
        coalescer.enqueue({"x": 1})
        coalescer.stop()
        coalescer.stop()  # Should not raise
        coalescer.stop()  # Should not raise

    def test_no_callback_when_empty(self, make_coalescer):
        """No callback fires if nothing was ever enqueued."""
        callback = MagicMock()
        coalescer = make_coalescer(on_progress=callback, debounce_s=0.02)
        time.sleep(0.05)  # Wait past debounce
        assert callback.call_count == 0

    def test_enqueue_after_stop_ignored(self, make_coalescer):
        """Enqueuing after stop is silently ignored — no crash, no delivery."""
        callback = MagicMock()
        coalescer = make_coalescer(on_progress=callback, debounce_s=0.02)
        coalescer.stop()
        initial_count = callback.call_count
        coalescer.enqueue({"should_not": "deliver"})
        time.sleep(0.05)
        assert callback.call_count == initial_count


class TestProgressCoalescerThreadSafety:
    """Concurrency and thread-safety tests."""

    def test_concurrent_enqueue_from_multiple_threads(self, make_coalescer):
        """Multiple threads enqueuing concurrently doesn't crash."""
        callback = MagicMock()
        coalescer = make_coalescer(on_progress=callback, debounce_s=0.05)
        errors = []
        barrier = threading.Barrier(8)

        def worker(thread_id):
            try:
                barrier.wait(timeout=2.0)
                for i in range(20):
                    coalescer.enqueue({"thread": thread_id, "i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # Stop flushes any pending snapshot so we can assert delivery
        coalescer.stop()
        assert not errors, f"Thread errors: {errors}"
        # At least one callback should have fired
        assert callback.call_count >= 1

    def test_callback_exception_does_not_crash(self, make_coalescer):
        """If callback raises, coalescer continues operating."""
        call_count = {"n": 0}

        def flaky_callback(snapshot):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated failure")

        coalescer = make_coalescer(on_progress=flaky_callback, debounce_s=0.02)
        coalescer.enqueue({"will": "fail"})
        time.sleep(0.05)
        # First call failed, enqueue again
        coalescer.enqueue({"will": "succeed"})
        time.sleep(0.05)
        # Should have attempted at least 2 deliveries
        assert call_count["n"] >= 2


class TestProgressCoalescerTiming:
    """Timing-related behavior tests."""

    def test_debounce_interval_respected(self, make_coalescer):
        """Callbacks are spaced by at least debounce_s."""
        timestamps = []

        def track_callback(snapshot):
            timestamps.append(time.monotonic())

        coalescer = make_coalescer(on_progress=track_callback, debounce_s=0.05)
        # Continuously enqueue over ~0.2s
        start = time.monotonic()
        while time.monotonic() - start < 0.2:
            coalescer.enqueue({"t": time.monotonic()})
            time.sleep(0.01)
        time.sleep(0.08)  # Let final delivery happen

        # Check spacing between consecutive callbacks
        if len(timestamps) >= 2:
            for i in range(1, len(timestamps)):
                gap = timestamps[i] - timestamps[i - 1]
                # Allow 20ms tolerance below debounce
                assert gap >= 0.03, f"Gap {gap:.3f}s too short at index {i}"

    def test_custom_debounce_value(self, make_coalescer):
        """Custom debounce_s is respected."""
        callback = MagicMock()
        coalescer = make_coalescer(on_progress=callback, debounce_s=0.08)
        coalescer.enqueue({"fast": True})
        time.sleep(0.03)  # Less than debounce
        # Should NOT have fired yet
        assert callback.call_count == 0
        time.sleep(0.08)  # Now past debounce
        assert callback.call_count >= 1
