"""Unit tests for _BoundedIOExecutor bounded queue behavior.

Verifies:
- Queue full discards oldest task with WARNING log
- shutdown rejects new submissions with RuntimeError
- Normal operation processes tasks in FIFO order
"""

from __future__ import annotations

import logging
import threading
import time

import pytest

from src.slock_engine.escalation_manager import _BoundedIOExecutor


class TestQueueFullDiscardsOldest:
    """AC-3: When queue is full, oldest task is discarded with WARNING."""

    def test_submit_when_full_discards_oldest(self, caplog):
        """Submit 17 tasks to maxsize=16 queue, oldest is discarded."""
        # Use a blocker so tasks pile up in queue
        blocker = threading.Event()
        execution_order = []

        def blocking_task():
            blocker.wait(timeout=10)
            execution_order.append("blocker")

        def numbered_task(n):
            execution_order.append(n)

        executor = _BoundedIOExecutor(max_queue_size=16)

        # First task blocks the consumer
        executor.submit(blocking_task)
        time.sleep(0.1)  # Let consumer pick it up

        # Fill the queue with 16 tasks (queue maxsize=16)
        for i in range(16):
            executor.submit(numbered_task, i)

        # Now queue is full. Submit one more — should discard oldest (task 0)
        with caplog.at_level(logging.WARNING):
            executor.submit(numbered_task, 99)

        # Check warning was logged
        assert any("discarding oldest task" in r.message for r in caplog.records), (
            f"Expected 'discarding oldest' warning, got: {[r.message for r in caplog.records]}"
        )

        # Release blocker and let everything drain
        blocker.set()
        time.sleep(2.0)
        executor.shutdown(wait=True)

        # Task 0 should have been discarded, task 99 should be present
        assert 0 not in execution_order, "Task 0 should have been discarded"
        assert 99 in execution_order, "Task 99 should have been executed"

    def test_submit_does_not_block(self):
        """Even when queue is full, submit returns immediately."""
        blocker = threading.Event()

        def blocking_task():
            blocker.wait(timeout=30)

        executor = _BoundedIOExecutor(max_queue_size=4)

        # Block consumer
        executor.submit(blocking_task)
        time.sleep(0.1)

        # Fill queue
        for i in range(4):
            executor.submit(lambda: None)

        # This should NOT block — measure time
        start = time.perf_counter()
        executor.submit(lambda: None)  # queue full, discards oldest
        elapsed = time.perf_counter() - start

        assert elapsed < 0.1, f"submit blocked for {elapsed:.3f}s, should be instant"

        blocker.set()
        executor.shutdown(wait=True)


class TestShutdownRejectsNewSubmissions:
    """AC-6: After shutdown, submit raises RuntimeError."""

    def test_submit_after_shutdown_raises(self):
        executor = _BoundedIOExecutor(max_queue_size=16)
        executor.shutdown(wait=True)

        with pytest.raises(RuntimeError, match="shut down"):
            executor.submit(lambda: None)

    def test_shutdown_wait_drains_queue(self):
        """shutdown(wait=True) waits for pending tasks to complete."""
        results = []

        def task(n):
            time.sleep(0.1)
            results.append(n)

        executor = _BoundedIOExecutor(max_queue_size=16)
        for i in range(3):
            executor.submit(task, i)

        executor.shutdown(wait=True)
        assert sorted(results) == [0, 1, 2]


class TestFIFOOrder:
    """Normal operation processes tasks in FIFO order."""

    def test_tasks_execute_in_order(self):
        results = []

        def task(n):
            results.append(n)

        executor = _BoundedIOExecutor(max_queue_size=16)
        for i in range(5):
            executor.submit(task, i)

        time.sleep(2.0)
        executor.shutdown(wait=True)
        assert results == [0, 1, 2, 3, 4]
