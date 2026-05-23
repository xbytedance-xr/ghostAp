"""Tests for the async audit log writer (_AuditLogWriter) in MemoryManager.

Covers:
- AC15: append_audit_log is non-blocking (returns in < 5ms)
- AC16: 100 concurrent threads each write one entry; all entries flushed
- AC23: graceful shutdown drains all queued entries to disk
- NFR02: backpressure behavior when the queue is full (maxsize=1000)
"""

from __future__ import annotations

import os
import queue
import threading
import time

import pytest

from src.slock_engine.memory_manager import MemoryManager


@pytest.fixture
def mm(tmp_path):
    """Create a MemoryManager backed by a temporary directory and shut it down after test."""
    manager = MemoryManager(base_path=str(tmp_path))
    yield manager
    manager.shutdown()


def _audit_log_path(tmp_path) -> str:
    """Return the expected audit log file path."""
    return os.path.join(str(tmp_path), "global", "AUDIT_LOG.md")


def _count_lines(path: str) -> int:
    """Return the number of lines in a file, or 0 if the file does not exist."""
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for _ in f)


class TestAuditLogNonblocking:
    """AC15: append_audit_log should return quickly without blocking on disk I/O."""

    def test_audit_log_nonblocking(self, mm, tmp_path):
        """append_audit_log must return in under 5ms (generous margin vs 1ms spec)."""
        start = time.perf_counter()
        mm.append_audit_log(
            operator_id="agent-1",
            action="test_action",
            target="target-1",
            detail="non-blocking check",
        )
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 5.0, (
            f"append_audit_log took {elapsed_ms:.2f}ms, expected < 5ms"
        )


class TestAuditLogConcurrent:
    """AC16: 100 concurrent threads writing audit logs must all be flushed."""

    def test_audit_log_concurrent_100(self, mm, tmp_path):
        """Spawn 100 threads each calling append_audit_log once.

        After shutdown, the file must contain exactly 102 lines:
        2 header lines (table header + separator) + 100 data rows.
        """
        num_threads = 100
        barrier = threading.Barrier(num_threads)
        errors: list[Exception] = []

        def _writer(idx: int) -> None:
            try:
                barrier.wait(timeout=5)
                mm.append_audit_log(
                    operator_id=f"agent-{idx}",
                    action="concurrent_write",
                    target=f"target-{idx}",
                    detail=f"entry {idx}",
                )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer, args=(i,), name=f"writer-{i}")
            for i in range(num_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Trigger flush by shutting down the writer
        mm.shutdown()

        assert not errors, f"Thread errors: {errors}"

        log_path = _audit_log_path(tmp_path)
        assert os.path.exists(log_path), "Audit log file was not created"

        line_count = _count_lines(log_path)
        assert line_count == 102, (
            f"Expected 102 lines (2 header + 100 data), got {line_count}"
        )


class TestAuditLogGracefulShutdown:
    """AC23: shutdown must drain all queued entries to disk."""

    def test_audit_log_graceful_shutdown(self, mm, tmp_path):
        """Enqueue multiple entries, then call shutdown. All must appear on disk."""
        num_entries = 30

        for i in range(num_entries):
            mm.append_audit_log(
                operator_id=f"op-{i}",
                action="shutdown_test",
                target=f"t-{i}",
                detail=f"detail-{i}",
            )

        # shutdown() should block until the consumer drains remaining items
        mm.shutdown()

        log_path = _audit_log_path(tmp_path)
        assert os.path.exists(log_path), "Audit log file was not created"

        line_count = _count_lines(log_path)
        expected = 2 + num_entries  # 2 header lines + data rows
        assert line_count == expected, (
            f"Expected {expected} lines (2 header + {num_entries} data), got {line_count}"
        )

        # Verify content: each entry should be present
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
        for i in range(num_entries):
            assert f"op-{i}" in content, f"Entry op-{i} not found in audit log"


class TestAuditLogBackpressure:
    """NFR02: When the queue is full, enqueue must block (not silently drop)."""

    def test_audit_log_backpressure(self, tmp_path):
        """Fill the queue to capacity (1000), then verify next enqueue blocks.

        We directly test the _AuditLogWriter to avoid MemoryManager side effects.
        The consumer thread is deliberately stalled by setting the shutdown event
        AFTER filling the queue, so it does not drain during the fill phase.
        """
        from src.slock_engine.memory_manager import _AuditLogWriter

        writer = _AuditLogWriter(base_path=str(tmp_path))

        # Pause the consumer by filling the queue as fast as possible.
        # The consumer may drain some items, so we use a direct queue reference.
        # Instead, we create a writer with a stalled consumer by monkey-patching.
        writer.shutdown()  # stop the normal consumer

        # Create a fresh writer whose consumer we control
        writer2 = _AuditLogWriter.__new__(_AuditLogWriter)
        writer2._base_path = str(tmp_path)
        writer2._queue = queue.Queue(maxsize=1000)
        writer2._shutdown_event = threading.Event()
        # Do NOT start the consumer thread -- queue will never drain
        writer2._thread = threading.Thread(target=lambda: None, daemon=True)
        writer2._thread.start()
        writer2._thread.join()  # thread exits immediately; queue never drained

        # Fill the queue to capacity
        for i in range(1000):
            writer2._queue.put_nowait(f"row-{i}\n")

        assert writer2._queue.full(), "Queue should be full after 1000 puts"

        # The next enqueue should block and eventually raise queue.Full
        # because the underlying put has timeout=5. We use a short timeout thread
        # to verify blocking behavior without waiting the full 5 seconds.
        blocked = threading.Event()
        released = threading.Event()

        def _try_enqueue():
            blocked.set()
            try:
                writer2.enqueue("overflow-row\n")
                released.set()  # should NOT reach here within our test window
            except queue.Full:
                released.set()

        t = threading.Thread(target=_try_enqueue, daemon=True)
        t.start()

        # Wait for the thread to start attempting the enqueue
        blocked.wait(timeout=2)
        # Give it a short window -- it should NOT complete because queue is full
        completed_quickly = released.wait(timeout=0.5)

        assert not completed_quickly, (
            "enqueue() returned immediately on a full queue; expected it to block"
        )

        # Cleanup: drain one item so the thread can finish, then join
        try:
            writer2._queue.get_nowait()
        except queue.Empty:
            pass
        t.join(timeout=6)
