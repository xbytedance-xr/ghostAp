"""AC-23: End-to-end lock lifecycle integration test.

Uses real RepoLockManager instances to verify the complete chain:
  handler context → lock acquire → mock engine execute → lock release

No mocking of RepoLockManager internals — validates the full lifecycle.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.config import Settings
from src.repo_lock import (
    AcquireResult,
    LockConflictError,
    RepoLockManager,
    _reset_repo_lock_manager_for_testing,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Ensure singleton is fresh for each test."""
    _reset_repo_lock_manager_for_testing()
    yield
    _reset_repo_lock_manager_for_testing()


@pytest.fixture()
def mgr():
    """Create a RepoLockManager with short timeouts for test speed."""
    return RepoLockManager(
        idle_timeout=300,
        cleanup_interval=600,  # Long to avoid interference
        hard_timeout=3600,
    )


class TestLockE2ELifecycle:
    """E2E: handler→acquire→engine execute→release full chain."""

    def test_acquire_execute_release_success(self, mgr: RepoLockManager):
        """Happy path: acquire → simulate engine work → release → lock freed."""
        path = "/tmp/test_project"
        chat_id = "chat_001"

        # 1. Acquire lock (simulating handler context entry)
        result = mgr.acquire(path, chat_id, sender_id="user_001")
        assert result.success is True

        # 2. Simulate engine execution (touch to keep alive)
        mgr.touch(path, chat_id)
        time.sleep(0.01)  # Simulate some work

        # 3. Verify lock is held
        info = mgr.get_lock_info(path)
        assert info is not None
        assert info.chat_id == chat_id

        # 4. Release lock (simulating handler context exit)
        mgr.release(path, chat_id)

        # 5. Verify lock is freed
        info = mgr.get_lock_info(path)
        assert info is None

    def test_acquire_conflict_then_release_resolves(self, mgr: RepoLockManager):
        """Two chats: chatA acquires, chatB conflicts, chatA releases → chatB can acquire."""
        path = "/tmp/shared_repo"
        chat_a = "chat_a"
        chat_b = "chat_b"

        # chatA acquires
        result_a = mgr.acquire(path, chat_a)
        assert result_a.success is True

        # chatB tries to acquire → conflict
        result_b = mgr.acquire(path, chat_b)
        assert result_b.success is False
        assert result_b.holder_chat_id == chat_a

        # chatA releases
        mgr.release(path, chat_a)

        # chatB can now acquire
        result_b2 = mgr.acquire(path, chat_b)
        assert result_b2.success is True

        # Cleanup
        mgr.release(path, chat_b)

    def test_hold_context_manager_auto_releases(self, mgr: RepoLockManager):
        """with mgr.hold(): auto-releases even on exception."""
        path = "/tmp/ctx_repo"
        chat_id = "chat_ctx"

        class _FakeEngineError(Exception):
            pass

        # Simulate engine crash inside hold()
        with pytest.raises(_FakeEngineError):
            with mgr.hold(path, chat_id) as result:
                assert result.success is True
                info = mgr.get_lock_info(path)
                assert info is not None
                raise _FakeEngineError("engine crashed")

        # Lock should be auto-released
        info = mgr.get_lock_info(path)
        assert info is None

    def test_hold_conflict_raises_by_default(self, mgr: RepoLockManager):
        """hold() raises LockConflictError when another chat holds the lock."""
        path = "/tmp/conflict_repo"
        mgr.acquire(path, "chat_holder")

        with pytest.raises(LockConflictError) as exc_info:
            with mgr.hold(path, "chat_requester"):
                pass  # Should not reach here

        assert "chat_holder" in str(exc_info.value)
        mgr.release(path, "chat_holder")

    def test_hold_conflict_with_on_conflict_callback(self, mgr: RepoLockManager):
        """hold() with on_conflict yields failed result instead of raising."""
        path = "/tmp/callback_repo"
        mgr.acquire(path, "chat_holder")

        conflict_results = []

        def on_conflict(result: AcquireResult):
            conflict_results.append(result)

        with mgr.hold(path, "chat_requester", on_conflict=on_conflict) as result:
            assert result.success is False

        assert len(conflict_results) == 1
        assert conflict_results[0].holder_chat_id == "chat_holder"
        mgr.release(path, "chat_holder")

    def test_reentrant_acquire_with_engine_chain(self, mgr: RepoLockManager):
        """Same chat acquires multiple times (reentrant) → refcount increments."""
        path = "/tmp/reentrant_repo"
        chat_id = "chat_reentrant"

        # First acquire (handler level)
        r1 = mgr.acquire(path, chat_id)
        assert r1.success is True

        # Second acquire (engine level, same chat)
        r2 = mgr.acquire(path, chat_id)
        assert r2.success is True

        # Release once — lock should still be held (refcount=1)
        mgr.release(path, chat_id)
        assert mgr.get_lock_info(path) is not None

        # Release again — lock freed
        mgr.release(path, chat_id)
        assert mgr.get_lock_info(path) is None

    def test_blocked_chats_notified_on_release(self, mgr: RepoLockManager):
        """When chatA releases, previously blocked chatB gets notified via on_release."""
        path = "/tmp/notify_repo"
        chat_a = "chat_a_notify"
        chat_b = "chat_b_notify"

        notifications = []

        def on_release(released_path, blocked_chats):
            notifications.append((released_path, blocked_chats))

        mgr.on_release.subscribe(on_release)

        # chatA acquires
        mgr.acquire(path, chat_a)

        # chatB tries → blocked
        mgr.acquire(path, chat_b)

        # chatA releases → should notify chatB
        mgr.release(path, chat_a)

        assert len(notifications) == 1
        assert chat_b in notifications[0][1]

    def test_force_release_frees_lock(self, mgr: RepoLockManager):
        """Admin force_release immediately frees the lock."""
        path = "/tmp/force_repo"
        mgr.acquire(path, "chat_holder")

        assert mgr.get_lock_info(path) is not None
        mgr.force_release(path)
        assert mgr.get_lock_info(path) is None

    def test_p2p_bypass_does_not_acquire(self, mgr: RepoLockManager):
        """P2P mode bypasses lock — no actual lock acquired."""
        path = "/tmp/p2p_repo"
        result = mgr.acquire(path, "chat_p2p", is_p2p=True)
        assert result.success is True
        # No lock should be held
        assert mgr.get_lock_info(path) is None

    def test_concurrent_handler_lifecycle(self, mgr: RepoLockManager):
        """Multiple threads simulate handler lifecycles on different repos."""
        errors = []
        barrier = threading.Barrier(5)

        def handler_cycle(idx):
            try:
                path = f"/tmp/concurrent_repo_{idx}"
                chat_id = f"chat_{idx}"
                barrier.wait(timeout=5)
                with mgr.hold(path, chat_id) as result:
                    assert result.success is True
                    time.sleep(0.01)
                # After hold, lock should be released
                assert mgr.get_lock_info(path) is None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=handler_cycle, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent handler cycles failed: {errors}"
