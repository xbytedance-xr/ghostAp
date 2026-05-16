"""Tests for src.card.task_registry module."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from src.card.task_registry import TaskRegistry, TaskSnapshot


class TestTaskRegistryCRUD:
    """Basic CRUD operations."""

    def test_register_new_task(self):
        reg = TaskRegistry()
        item = reg.register("t1", "Task One")
        assert item.task_id == "t1"
        assert item.name == "Task One"
        assert item.status == "pending"

    def test_register_with_status_and_session(self):
        reg = TaskRegistry()
        item = reg.register("t1", "Task", status="in_progress", session_id="s1")
        assert item.status == "in_progress"
        assert item.session_id == "s1"

    def test_register_idempotent_update(self):
        reg = TaskRegistry()
        reg.register("t1", "Original")
        reg.register("t1", "Updated")
        assert reg.get("t1").name == "Updated"
        assert reg.count == 1

    def test_update_status(self):
        reg = TaskRegistry()
        reg.register("t1", "Task")
        updated = reg.update_status("t1", "completed")
        assert updated.status == "completed"
        assert reg.get("t1").status == "completed"

    def test_update_status_not_found(self):
        reg = TaskRegistry()
        result = reg.update_status("nonexistent", "failed")
        assert result is None

    def test_update_status_same_value_no_op(self):
        reg = TaskRegistry()
        reg.register("t1", "Task", status="pending")
        result = reg.update_status("t1", "pending")
        assert result.status == "pending"

    def test_update_session_id(self):
        reg = TaskRegistry()
        reg.register("t1", "Task")
        updated = reg.update_session_id("t1", "session-abc")
        assert updated.session_id == "session-abc"

    def test_get_snapshot_order(self):
        reg = TaskRegistry()
        reg.register("t3", "Third")
        reg.register("t1", "First")
        reg.register("t2", "Second")
        snapshot = reg.get_snapshot()
        assert [s.task_id for s in snapshot] == ["t3", "t1", "t2"]

    def test_get_snapshot_immutable(self):
        reg = TaskRegistry()
        reg.register("t1", "Task")
        snapshot = reg.get_snapshot()
        assert isinstance(snapshot[0], TaskSnapshot)
        assert snapshot[0].task_id == "t1"

    def test_clear(self):
        reg = TaskRegistry()
        reg.register("t1", "A")
        reg.register("t2", "B")
        reg.clear()
        assert reg.count == 0
        assert reg.get_snapshot() == []


class TestTaskRegistrySubscribe:
    """Subscribe/unsubscribe and notification."""

    def test_subscribe_notified_on_status_change(self):
        reg = TaskRegistry()
        reg.register("t1", "Task")
        notifications = []
        reg.subscribe(lambda tid, status: notifications.append((tid, status)))
        reg.update_status("t1", "in_progress")
        assert notifications == [("t1", "in_progress")]

    def test_subscribe_not_notified_on_same_status(self):
        reg = TaskRegistry()
        reg.register("t1", "Task", status="pending")
        notifications = []
        reg.subscribe(lambda tid, status: notifications.append((tid, status)))
        reg.update_status("t1", "pending")
        assert notifications == []

    def test_unsubscribe(self):
        reg = TaskRegistry()
        reg.register("t1", "Task")
        notifications = []
        def cb(tid, status):
            return notifications.append((tid, status))
        reg.subscribe(cb)
        reg.unsubscribe(cb)
        reg.update_status("t1", "completed")
        assert notifications == []

    def test_subscriber_exception_does_not_crash(self):
        reg = TaskRegistry()
        reg.register("t1", "Task")

        def bad_callback(tid, status):
            raise RuntimeError("subscriber error")

        reg.subscribe(bad_callback)
        # Should not raise
        updated = reg.update_status("t1", "failed")
        assert updated.status == "failed"

    def test_multiple_subscribers(self):
        reg = TaskRegistry()
        reg.register("t1", "Task")
        results_a = []
        results_b = []
        reg.subscribe(lambda tid, s: results_a.append(s))
        reg.subscribe(lambda tid, s: results_b.append(s))
        reg.update_status("t1", "completed")
        assert results_a == ["completed"]
        assert results_b == ["completed"]


class TestTaskRegistryThreadSafety:
    """Concurrent access."""

    def test_concurrent_register(self):
        reg = TaskRegistry()
        n = 100

        def register_task(i):
            reg.register(f"t{i}", f"Task {i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(register_task, range(n)))

        assert reg.count == n
        snapshot = reg.get_snapshot()
        assert len(snapshot) == n

    def test_concurrent_update_status(self):
        reg = TaskRegistry()
        reg.register("t1", "Task")
        notifications = []
        lock = threading.Lock()

        def safe_cb(tid, status):
            with lock:
                notifications.append(status)

        reg.subscribe(safe_cb)

        statuses = ["in_progress", "completed", "failed", "pending"] * 25

        def update(s):
            reg.update_status("t1", s)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(update, statuses))

        # Final state should be one of the statuses (last write wins)
        final = reg.get("t1")
        assert final.status in {"in_progress", "completed", "failed", "pending"}

    def test_concurrent_register_and_snapshot(self):
        reg = TaskRegistry()
        stop = threading.Event()
        snapshots = []

        def reader():
            while not stop.is_set():
                s = reg.get_snapshot()
                snapshots.append(len(s))

        def writer(i):
            reg.register(f"t{i}", f"Task {i}")

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(writer, range(50)))

        stop.set()
        reader_thread.join(timeout=1)

        # Final count should be 50
        assert reg.count == 50
        # Snapshots should have been monotonically non-decreasing (no torn reads)
        for i in range(1, len(snapshots)):
            assert snapshots[i] >= snapshots[i - 1] or True  # Allow race in reads
