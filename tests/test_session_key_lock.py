"""Tests for per-session-key lock in ACPSessionManager (TOCTOU prevention)."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.acp.manager import ACPSessionManager


class FakeSession:
    """Fake SyncSession for testing."""

    _instance_count = 0
    _count_lock = threading.Lock()

    def __init__(self):
        with FakeSession._count_lock:
            FakeSession._instance_count += 1
        self.session_id = f"fake-{FakeSession._instance_count}"
        self.last_active = time.time()
        self.message_count = 0
        self._closed = False

    def is_server_running(self):
        return not self._closed

    def close(self):
        self._closed = True

    def to_snapshot(self):
        return {"session_id": self.session_id}

    def load_local_history(self, sid):
        pass

    def describe_agent(self):
        return "test-agent"


def _make_starter(delay: float = 0.1):
    """Create a session_starter callable that returns (session, id, diag) tuple."""
    def starter(*, agent_type, cwd, startup_timeout, model_name=None, session_id=None, project_id=None, **kw):
        time.sleep(delay)  # Simulate startup time
        sess = FakeSession()
        return sess, sess.session_id, {}
    return starter


@pytest.fixture
def manager():
    """Create a minimal ACPSessionManager for testing."""
    FakeSession._instance_count = 0
    mgr = ACPSessionManager(
        agent_type="test",
        session_starter=_make_starter(0.05),
    )
    yield mgr
    mgr.cleanup_all()


class TestPerKeyLockSerialization:
    """Concurrent start_session calls for the same key should not leak sessions."""

    def test_concurrent_start_session_same_key_no_leak(self, manager):
        """Multiple threads starting session with same key: only one session survives."""
        results = []
        errors = []

        def start_thread():
            try:
                session = manager.start_session("chat1", project_id="proj1")
                results.append(session)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=start_thread) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Unexpected errors: {errors}"
        # All threads succeeded (each replaces the previous session)
        assert len(results) == 5

        # But only ONE session should be registered in the manager
        with manager._acquire_lock():
            session_count = len(manager._sessions)
        assert session_count == 1, f"Expected 1 active session, got {session_count}"

    def test_concurrent_start_session_different_keys_parallel(self, manager):
        """Different keys should not block each other."""
        start_times = {}
        end_times = {}

        def start_with_key(key_suffix):
            start_times[key_suffix] = time.monotonic()
            manager.start_session(f"chat{key_suffix}", project_id=f"proj{key_suffix}")
            end_times[key_suffix] = time.monotonic()

        threads = [threading.Thread(target=start_with_key, args=(i,)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # All 3 should have completed
        assert len(end_times) == 3

        # With parallelism, total time should be close to single-session time (~0.05s)
        total_wall = max(end_times.values()) - min(start_times.values())
        # Give generous margin for CI — should be well under 3 * 0.05s sequential
        assert total_wall < 1.0, f"Parallel execution took too long: {total_wall:.2f}s"

    def test_key_lock_cleanup_on_session_end(self, manager):
        """Per-key lock should be cleaned up when session ends."""
        manager.start_session("chat1", project_id="proj1")
        key = manager._session_key("chat1", "proj1")

        # Key lock should exist
        with manager._key_locks_lock:
            assert key in manager._key_locks

        # End session
        manager.end_session("chat1", project_id="proj1")

        # Key lock should be cleaned up
        with manager._key_locks_lock:
            assert key not in manager._key_locks


class TestKeyLockExceptionRecovery:
    """Key lock should be released even when _start_session_inner raises."""

    def test_key_lock_released_on_start_inner_exception(self):
        """If _start_session_inner raises, key lock must be released so next call proceeds."""
        call_count = {"n": 0}

        def patched_inner(self_mgr, key, chat_id, cwd, session_id, startup_timeout,
                          project_id, agent_type_override, model_name, thread_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("startup boom")
            # Second call succeeds
            sess = FakeSession()
            with self_mgr._acquire_lock():
                self_mgr._sessions[key] = sess
            return sess

        FakeSession._instance_count = 0
        mgr = ACPSessionManager(agent_type="test", session_starter=_make_starter(0.01))
        try:
            # Patch _start_session_inner to force a raise
            original = mgr._start_session_inner
            mgr._start_session_inner = lambda *args, **kw: patched_inner(mgr, *args, **kw)

            # First call should fail
            with pytest.raises(RuntimeError, match="startup boom"):
                mgr.start_session("chat1", project_id="proj1")

            # Second call should succeed (lock was released by finally block)
            session = mgr.start_session("chat1", project_id="proj1")
            assert session is not None
        finally:
            mgr.cleanup_all()

    def test_start_end_concurrent_no_crash(self):
        """start_session and end_session called concurrently should not crash or leak."""
        FakeSession._instance_count = 0
        mgr = ACPSessionManager(
            agent_type="test",
            session_starter=_make_starter(0.02),
        )
        errors = []

        def do_start():
            try:
                mgr.start_session("chat1", project_id="proj1")
            except Exception as e:
                errors.append(e)

        def do_end():
            try:
                time.sleep(0.1)  # Let start complete first
                mgr.end_session("chat1", project_id="proj1")
            except Exception as e:
                errors.append(e)

        try:
            t1 = threading.Thread(target=do_start)
            t2 = threading.Thread(target=do_end)
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)

            assert not errors, f"Unexpected errors: {errors}"
        finally:
            mgr.cleanup_all()

    def test_key_lock_timeout_raises(self):
        """If key_lock cannot be acquired within timeout, TimeoutError is raised."""
        FakeSession._instance_count = 0

        def slow_inner(self_mgr, key, *args, **kw):
            time.sleep(5)  # Hold the key lock for a long time
            sess = FakeSession()
            with self_mgr._acquire_lock():
                self_mgr._sessions[key] = sess
            return sess

        mgr = ACPSessionManager(agent_type="test", session_starter=_make_starter(0.01))
        mgr._start_session_inner = lambda *args, **kw: slow_inner(mgr, *args, **kw)
        errors = []

        def start_slow():
            try:
                mgr.start_session("chat1", project_id="proj1", startup_timeout=10)
            except Exception:
                pass

        def start_with_timeout():
            time.sleep(0.1)  # Ensure slow thread acquires lock first
            try:
                mgr.start_session("chat1", project_id="proj1", startup_timeout=1)
            except TimeoutError as e:
                errors.append(e)

        try:
            t1 = threading.Thread(target=start_slow)
            t2 = threading.Thread(target=start_with_timeout)
            t1.start()
            t2.start()
            t2.join(timeout=5)
            t1.join(timeout=10)

            assert len(errors) == 1
            assert "超时" in str(errors[0])
        finally:
            mgr.cleanup_all()
