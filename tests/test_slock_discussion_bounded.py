"""Tests for bounded discussion executor.

AC-R14: Concurrent discussion submissions beyond max_queue_size are rejected.
"""

from __future__ import annotations

import threading

import pytest

from src.slock_engine.bounded_executor import BoundedExecutor
from src.slock_engine.exceptions import ExecutorQueueFullError


class TestDiscussionBounded:
    """AC-R14: Discussion executor must reject overflow."""

    def test_overflow_raises_executor_queue_full_error(self):
        """Submit 20 tasks to executor with max_queue_size=6, expect rejections."""
        executor = BoundedExecutor(max_workers=2, max_queue_size=6)
        barrier = threading.Event()  # Block workers to fill queue

        def blocking_task():
            barrier.wait(timeout=10)
            return "done"

        submitted = 0
        rejected = 0

        for i in range(20):
            try:
                executor.submit(blocking_task)
                submitted += 1
            except ExecutorQueueFullError:
                rejected += 1

        # At most 2 workers + 6 queued = 8 can be accepted
        assert submitted <= 8
        assert rejected >= 12  # At least 12 should be rejected

        # Cleanup: unblock workers
        barrier.set()
        executor.shutdown(wait=True)

    def test_within_limit_succeeds(self):
        """Submissions within queue limit should succeed without error."""
        executor = BoundedExecutor(max_workers=2, max_queue_size=6)

        results = []
        for i in range(2):  # Only submit up to worker count
            future = executor.submit(lambda: "ok")
            results.append(future)

        for f in results:
            assert f.result(timeout=5) == "ok"

        executor.shutdown(wait=True)

    def test_enqueue_time_annotated_on_future(self):
        """Each submitted future must have an enqueue_time attribute."""
        executor = BoundedExecutor(max_workers=2, max_queue_size=6)

        future = executor.submit(lambda: "done")
        assert hasattr(future, "enqueue_time")
        assert isinstance(future.enqueue_time, float)
        assert future.enqueue_time > 0

        future.result(timeout=5)
        executor.shutdown(wait=True)

    def test_pending_count_tracks_inflight(self):
        """pending_count should reflect submitted but incomplete tasks."""
        executor = BoundedExecutor(max_workers=2, max_queue_size=6)
        barrier = threading.Event()

        def blocking_task():
            barrier.wait(timeout=10)

        executor.submit(blocking_task)
        executor.submit(blocking_task)

        assert executor.pending_count >= 2

        barrier.set()
        executor.shutdown(wait=True)

        assert executor.pending_count == 0

    def test_shutdown_rejects_further_submissions(self):
        """After shutdown, submit raises RuntimeError."""
        executor = BoundedExecutor(max_workers=2, max_queue_size=6)
        executor.shutdown(wait=True)

        with pytest.raises(RuntimeError):
            executor.submit(lambda: "never")
