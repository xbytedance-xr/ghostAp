"""Tests for drain_in_flight under high concurrency.

Verifies that SessionLockPool.drain() correctly waits for all in-flight
deliveries to complete, even with 12+ concurrent threads.
"""

import threading
import time

import pytest

from src.card.delivery.lock_pool import SessionLockPool


class TestDrainHighConcurrency:
    """drain() with 12+ concurrent in-flight deliveries."""

    def _make_pool(self) -> SessionLockPool:
        return SessionLockPool(
            max_locks=10_000,
            lock_ttl=600.0,
            eviction_interval=9999.0,
            has_active_binding=lambda _sid: False,
        )

    def test_drain_waits_for_12_concurrent_deliveries(self):
        """drain() blocks until all 12 concurrent deliveries exit."""
        pool = self._make_pool()
        num_threads = 12
        barrier = threading.Barrier(num_threads + 1, timeout=10)
        delivery_done = threading.Event()

        def delivery_worker(delay: float):
            pool.enter_delivery()
            try:
                barrier.wait()  # synchronize start
                time.sleep(delay)
            finally:
                pool.exit_delivery()

        threads = []
        for i in range(num_threads):
            t = threading.Thread(
                target=delivery_worker,
                args=(0.02 + i * 0.005,),  # staggered delays 20-75ms
            )
            threads.append(t)
            t.start()

        barrier.wait()  # all threads are in-flight

        # Verify in-flight count matches
        with pool._in_flight_condition:
            assert pool._in_flight_count == num_threads

        # drain should wait for all to finish
        drained = pool.drain(timeout=5.0)
        assert drained is True

        with pool._in_flight_condition:
            assert pool._in_flight_count == 0

        for t in threads:
            t.join(timeout=2)

        pool.shutdown()

    def test_drain_returns_false_on_timeout(self):
        """drain() returns False if deliveries don't finish in time."""
        pool = self._make_pool()

        pool.enter_delivery()
        # Don't exit — simulate stuck delivery

        drained = pool.drain(timeout=0.05)
        assert drained is False

        # Cleanup
        pool.exit_delivery()
        pool.shutdown()

    def test_fence_then_drain_blocks_new_work(self):
        """fence() + drain() pattern: new work rejected, existing finishes."""
        pool = self._make_pool()

        # Start a delivery
        pool.enter_delivery()

        # Fence: stop accepting new work
        pool.fence()
        assert pool.accepting_work is False

        # Simulate existing delivery finishing
        def finish_later():
            time.sleep(0.03)
            pool.exit_delivery()

        t = threading.Thread(target=finish_later)
        t.start()

        drained = pool.drain(timeout=2.0)
        assert drained is True

        t.join(timeout=2)
        pool.shutdown()

    def test_concurrent_enter_exit_no_negative_count(self):
        """Rapid concurrent enter/exit never drives count negative."""
        pool = self._make_pool()
        num_threads = 20
        iterations = 100
        errors: list[str] = []

        def worker():
            for _ in range(iterations):
                pool.enter_delivery()
                # tiny yield to increase interleaving
                time.sleep(0)
                pool.exit_delivery()
                with pool._in_flight_condition:
                    if pool._in_flight_count < 0:
                        errors.append(f"Negative count: {pool._in_flight_count}")
                        return

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == []
        with pool._in_flight_condition:
            assert pool._in_flight_count == 0

        pool.shutdown()

    def test_drain_after_all_exit_returns_immediately(self):
        """drain() returns True immediately if no in-flight deliveries."""
        pool = self._make_pool()

        start = time.monotonic()
        drained = pool.drain(timeout=5.0)
        elapsed = time.monotonic() - start

        assert drained is True
        assert elapsed < 0.1  # should be nearly instant

        pool.shutdown()
