"""Unit tests for RepoLockManager."""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.repo_lock import AcquireResult, LockConflictError, RepoLockInfo, RepoLockManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def mgr():
    """Create an isolated RepoLockManager (no global singleton side-effects)."""
    m = RepoLockManager(idle_timeout=300, cleanup_interval=9999)
    yield m
    m.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRepoLockAcquire:

    def test_acquire_success(self, mgr: RepoLockManager):
        result = mgr.acquire("/tmp/repo1", "chat_A")
        assert result.success is True

    def test_acquire_reentrant(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        result = mgr.acquire("/tmp/repo1", "chat_A")
        assert result.success is True
        # Check refcount via list_locks
        locks = mgr.list_locks()
        assert len(locks) == 1
        assert locks[0].refcount == 2

    def test_acquire_conflict(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is False
        assert result.holder_chat_id == "chat_A"
        assert result.locked_since is not None

    def test_acquire_p2p_bypass(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        result = mgr.acquire("/tmp/repo1", "chat_B", is_p2p=True)
        assert result.success is True

    def test_release_and_reacquire(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.acquire("/tmp/repo1", "chat_A")  # refcount = 2
        mgr.release("/tmp/repo1", "chat_A")  # refcount = 1
        # Still held by chat_A
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is False

        mgr.release("/tmp/repo1", "chat_A")  # refcount = 0 → removed
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is True

    def test_force_release(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.force_release("/tmp/repo1")
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is True

    def test_path_normalization(self, mgr: RepoLockManager):
        """~/repo, $HOME/repo, $HOME/./repo should all resolve to the same key."""
        home = os.path.expanduser("~")
        real_home = os.path.realpath(home)

        mgr.acquire(f"{home}/test_repo_norm", "chat_A")
        # Same path via ~/
        result = mgr.acquire("~/test_repo_norm", "chat_A")
        assert result.success is True  # reentrant, same key

        # Different chat, same resolved path → conflict
        result2 = mgr.acquire(f"{real_home}/./test_repo_norm", "chat_B")
        assert result2.success is False

    def test_list_locks(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.acquire("/tmp/repo2", "chat_B")
        locks = mgr.list_locks()
        assert len(locks) == 2
        paths = {l.root_path for l in locks}
        assert "/tmp/repo1" in paths
        assert "/tmp/repo2" in paths

    def test_idle_timeout_cleanup(self, mgr: RepoLockManager):
        mgr._idle_timeout = 1  # 1 second timeout for testing
        mgr.acquire("/tmp/repo1", "chat_A")
        # Simulate a leaked lock entry with refcount=0 (e.g. bug in caller)
        with mgr._mu:
            mgr._locks["/tmp/repo1"].refcount = 0
        time.sleep(1.5)  # Wait for lock to become idle
        mgr._cleanup_idle()
        # Lock should be cleaned up
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is True

    def test_cleanup_skips_active_refcount_on_idle(self, mgr: RepoLockManager):
        """H-1 fix: _cleanup_idle must NOT evict entries with refcount > 0 via idle_timeout.

        Previously this test verified zombie-lock reclaim (refcount > 0 evicted by idle).
        After the H-1 fix, refcount > 0 entries are protected from idle_timeout eviction.
        """
        mgr._idle_timeout = 0  # everything is "idle"
        mgr._hard_timeout = 999999  # hard_timeout won't trigger
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.acquire("/tmp/repo1", "chat_A")  # refcount = 2
        mgr._cleanup_idle()
        # Lock should NOT be reclaimed — refcount > 0 is protected
        locks = mgr.list_locks()
        assert len(locks) == 1
        assert locks[0].refcount == 2
        # Another chat should still be blocked
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is False

    def test_cleanup_evicts_zero_refcount(self, mgr: RepoLockManager):
        """F-01: _cleanup_idle evicts entries with refcount <= 0 and idle."""
        mgr._idle_timeout = 0
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.release("/tmp/repo1", "chat_A")  # refcount = 0, removed by release
        # Re-acquire to get refcount=1, then force refcount=0 without release
        mgr.acquire("/tmp/repo2", "chat_B")
        # Manually set refcount to 0 for testing
        with mgr._mu:
            mgr._locks["/tmp/repo2"].refcount = 0
        mgr._cleanup_idle()
        assert mgr.list_locks() == []

    def test_cleanup_removes_token_mapping(self, mgr: RepoLockManager):
        """F-02: _cleanup_idle removes token mappings for evicted paths."""
        mgr._idle_timeout = 0
        mgr.acquire("/tmp/repo1", "chat_A")
        token = mgr.path_to_token("/tmp/repo1")
        assert token
        assert mgr.token_to_path(token) == "/tmp/repo1"
        # Force refcount to 0 so cleanup can evict
        with mgr._mu:
            mgr._locks["/tmp/repo1"].refcount = 0
        mgr._cleanup_idle()
        # Token mapping should be cleaned up
        assert mgr.token_to_path(token) is None
        # path_to_token cache should also be cleared
        with mgr._mu:
            assert "/tmp/repo1" not in mgr._path_to_token

    def test_release_cleans_token_mapping(self, mgr: RepoLockManager):
        """release() should clean up token mappings when refcount reaches 0."""
        mgr.acquire("/tmp/repo1", "chat_A")
        token = mgr.path_to_token("/tmp/repo1")
        assert token
        assert mgr.token_to_path(token) == "/tmp/repo1"
        mgr.release("/tmp/repo1", "chat_A")  # refcount 0 → entry removed
        # Token mapping should be cleaned up
        assert mgr.token_to_path(token) is None
        with mgr._mu:
            assert "/tmp/repo1" not in mgr._path_to_token

    def test_force_release_cleans_token_mapping(self, mgr: RepoLockManager):
        """force_release() should clean up token mappings."""
        mgr.acquire("/tmp/repo1", "chat_A")
        token = mgr.path_to_token("/tmp/repo1")
        assert token
        assert mgr.token_to_path(token) == "/tmp/repo1"
        mgr.force_release("/tmp/repo1")
        # Token mapping should be cleaned up
        assert mgr.token_to_path(token) is None
        with mgr._mu:
            assert "/tmp/repo1" not in mgr._path_to_token

    def test_cleanup_idle_skips_recently_touched(self, mgr: RepoLockManager):
        """AC-18: _cleanup_idle must NOT evict entries that are recently touched
        (simulating active operations keeping the lock alive via touch())."""
        mgr._idle_timeout = 1  # 1 second timeout for testing
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.acquire("/tmp/repo1", "chat_A")  # refcount = 2

        time.sleep(0.6)
        mgr.touch("/tmp/repo1", "chat_A")  # refresh last_active_time
        time.sleep(0.6)
        # Total elapsed > 1s from acquire, but touch was recent (0.6s ago)
        mgr._cleanup_idle()

        # Lock should still be held because touch refreshed last_active_time
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is False  # still held by chat_A

    def test_touch_prevents_timeout(self, mgr: RepoLockManager):
        mgr._idle_timeout = 1
        mgr.acquire("/tmp/repo1", "chat_A")
        time.sleep(0.6)
        mgr.touch("/tmp/repo1", "chat_A")
        time.sleep(0.6)
        mgr._cleanup_idle()
        # Lock should still be held because touch refreshed it
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is False

    def test_acquire_none_path(self, mgr: RepoLockManager):
        result = mgr.acquire("", "chat_A")
        assert result.success is True  # empty path → no-op success

    def test_release_wrong_chat_noop(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.release("/tmp/repo1", "chat_B")  # wrong chat → no-op
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is False  # still held by chat_A

    def test_cleanup_idle_skips_active_refcount(self, mgr: RepoLockManager):
        """H-1: refcount > 0 entries must NOT be evicted by idle_timeout alone."""
        mgr._idle_timeout = 0  # everything is "idle"
        mgr._hard_timeout = 999999  # hard_timeout won't trigger
        mgr.acquire("/tmp/repo1", "chat_A")  # refcount = 1
        mgr._cleanup_idle()
        # Lock must still be held
        locks = mgr.list_locks()
        assert len(locks) == 1
        assert locks[0].refcount == 1
        assert locks[0].chat_id == "chat_A"

    def test_cleanup_hard_timeout_evicts_active_lock(self, mgr: RepoLockManager, caplog):
        """H-1 safety valve: refcount > 0 but held > hard_timeout → force evict."""
        import logging

        mgr._idle_timeout = 999999  # idle won't trigger
        mgr._hard_timeout = 0  # everything exceeds hard_timeout
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.acquire("/tmp/repo1", "chat_A")  # refcount = 2

        with caplog.at_level(logging.CRITICAL, logger="src.repo_lock"):
            mgr._cleanup_idle()

        # Lock should be force-reclaimed by hard_timeout
        assert mgr.list_locks() == []
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is True

        # Verify CRITICAL log emitted for hard-timeout force-reclaim
        critical_msgs = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert any("hard-timeout force-reclaimed" in r.message for r in critical_msgs)

    def test_cleanup_phase3_warning_log(self, mgr: RepoLockManager, caplog):
        """Phase 3: active lock idle without touch emits WARNING but is NOT evicted."""
        import logging

        mgr._idle_timeout = 0  # everything is "idle"
        mgr._hard_timeout = 999999  # hard_timeout won't trigger
        mgr.acquire("/tmp/repo1", "chat_A")  # refcount = 1

        with caplog.at_level(logging.WARNING, logger="src.repo_lock"):
            mgr._cleanup_idle()

        # Lock must NOT be evicted (Phase 3 only warns)
        locks = mgr.list_locks()
        assert len(locks) == 1
        assert locks[0].refcount == 1

        # Verify WARNING log about idle active lock
        warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("active lock idle without touch" in r.message for r in warn_msgs)
        assert any("not evicted" in r.message.lower() for r in warn_msgs)

    def test_cleanup_hard_timeout_cleans_token_mapping(self, mgr: RepoLockManager):
        """Phase 2 hard_timeout eviction must also clean path↔token mappings."""
        mgr._idle_timeout = 999999
        mgr._hard_timeout = 0  # everything exceeds hard_timeout
        mgr.acquire("/tmp/repo1", "chat_A")
        token = mgr.path_to_token("/tmp/repo1")
        assert token
        assert mgr.token_to_path(token) == "/tmp/repo1"

        mgr._cleanup_idle()

        # Token mapping should be cleaned up after hard-timeout eviction
        assert mgr.token_to_path(token) is None
        with mgr._mu:
            assert "/tmp/repo1" not in mgr._path_to_token

    def test_cleanup_idle_evicts_zero_refcount(self, mgr: RepoLockManager):
        """Regression: refcount == 0 + idle > timeout → still evicted normally."""
        mgr._idle_timeout = 0
        mgr._hard_timeout = 999999
        mgr.acquire("/tmp/repo1", "chat_A")
        # Manually force refcount to 0 (simulating a bug in caller)
        with mgr._mu:
            mgr._locks["/tmp/repo1"].refcount = 0
        mgr._cleanup_idle()
        assert mgr.list_locks() == []
        result = mgr.acquire("/tmp/repo1", "chat_B")
        assert result.success is True


class TestPathToken:
    """Tests for RepoLockManager.path_to_token / token_to_path."""

    def test_path_to_token_roundtrip(self, mgr: RepoLockManager):
        token = mgr.path_to_token("/tmp/repo1")
        assert token  # non-empty
        assert len(token) == 32  # sha256 hex prefix
        resolved = mgr.token_to_path(token)
        assert resolved == "/tmp/repo1"

    def test_path_to_token_deterministic(self, mgr: RepoLockManager):
        t1 = mgr.path_to_token("/tmp/repo1")
        t2 = mgr.path_to_token("/tmp/repo1")
        assert t1 == t2

    def test_path_to_token_different_paths(self, mgr: RepoLockManager):
        t1 = mgr.path_to_token("/tmp/repo1")
        t2 = mgr.path_to_token("/tmp/repo2")
        assert t1 != t2

    def test_token_to_path_unknown(self, mgr: RepoLockManager):
        assert mgr.token_to_path("nonexistent") is None

    def test_path_to_token_empty(self, mgr: RepoLockManager):
        assert mgr.path_to_token("") == ""

    def test_token_to_path_empty(self, mgr: RepoLockManager):
        assert mgr.token_to_path("") is None


class TestGetLockInfo:
    """Tests for RepoLockManager.get_lock_info O(1) lookup."""

    def test_get_lock_info_returns_info(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        info = mgr.get_lock_info("/tmp/repo1")
        assert info is not None
        assert info.chat_id == "chat_A"
        assert info.refcount == 1
        assert info.root_path == "/tmp/repo1"
        assert info.idle_seconds >= 0

    def test_get_lock_info_returns_none_when_not_locked(self, mgr: RepoLockManager):
        info = mgr.get_lock_info("/tmp/repo1")
        assert info is None

    def test_get_lock_info_after_release(self, mgr: RepoLockManager):
        mgr.acquire("/tmp/repo1", "chat_A")
        mgr.release("/tmp/repo1", "chat_A")
        info = mgr.get_lock_info("/tmp/repo1")
        assert info is None

    def test_get_lock_info_empty_path(self, mgr: RepoLockManager):
        info = mgr.get_lock_info("")
        assert info is None

    def test_get_lock_info_normalizes_path(self, mgr: RepoLockManager):
        home = os.path.expanduser("~")
        mgr.acquire(f"{home}/test_info_repo", "chat_A")
        info = mgr.get_lock_info("~/test_info_repo")
        assert info is not None
        assert info.chat_id == "chat_A"


# ---------------------------------------------------------------------------
# is_p2p thread-local → RepoLock propagation tests
# ---------------------------------------------------------------------------


class TestIsP2PThreadLocalPropagation:
    """Verify that the is_p2p thread-local value propagates correctly through
    handler methods to RepoLockManager.hold / acquire, solving the broken
    parameter chain issue.
    """

    def test_programming_handle_message_reads_threadlocal_p2p(self):
        """handle_message() without explicit is_p2p should read thread-local."""
        from unittest.mock import MagicMock, patch
        from src.thread import set_current_is_p2p

        set_current_is_p2p(True)
        try:
            mock_ctx = MagicMock()
            mock_ctx.settings.thread_programming_enabled = False
            mock_ctx.settings.coco_execution_timeout = 30
            mock_ctx.settings.card_collapsible_enabled = False

            from src.feishu.handlers.programming import CocoModeHandler
            handler = CocoModeHandler(mock_ctx)

            # Stub out methods that are not under test
            handler.reply_message = MagicMock()
            handler.reply_message_with_id = MagicMock(return_value="reply_1")
            handler.add_reaction = MagicMock()
            handler.register_message_project = MagicMock()
            handler.record_mode_transition = MagicMock()
            handler.inject_bridge_context = MagicMock(side_effect=lambda t, p: t)
            handler.get_working_dir = MagicMock(return_value="/tmp/test")
            handler.ensure_request_id = MagicMock(return_value="req-1")

            # Mock project with root_path
            mock_project = MagicMock()
            mock_project.project_id = "proj-1"
            mock_project.root_path = "/tmp/test"

            # Mock repo_lock_manager on ctx
            mock_lock_mgr = MagicMock()
            mock_ctx.repo_lock_manager = mock_lock_mgr

            # Mock session so handle_message doesn't try to enter_mode
            mock_session = MagicMock()
            mock_session.session_id = "sess-1"
            mock_session.message_count = 1
            mock_session.last_query = "test"
            mock_ctx.coco_manager.get_session.return_value = mock_session

            # Mock streaming
            mock_streaming = MagicMock()
            mock_streaming.create_streaming_card.return_value = None
            mock_ctx.streaming_manager_factory.return_value = mock_streaming
            handler.get_streaming_manager = MagicMock(return_value=mock_streaming)

            # Call handle_message WITHOUT is_p2p — should read True from thread-local
            handler.handle_message("msg-1", "chat-1", "hello", project=mock_project)

            # is_p2p=True → lock acquire is skipped entirely (guard: `not is_p2p`)
            mock_lock_mgr.acquire.assert_not_called()
        finally:
            set_current_is_p2p(False)

    def test_programming_handle_message_threadlocal_false(self):
        """Thread-local is_p2p=False should call repo_lock_mgr.acquire()."""
        from unittest.mock import MagicMock
        from src.thread import set_current_is_p2p

        # is_p2p is now purely thread-local; no explicit parameter on handle_message
        set_current_is_p2p(False)
        try:
            mock_ctx = MagicMock()
            mock_ctx.settings.thread_programming_enabled = False
            mock_ctx.settings.coco_execution_timeout = 30
            mock_ctx.settings.card_collapsible_enabled = False

            from src.feishu.handlers.programming import CocoModeHandler
            handler = CocoModeHandler(mock_ctx)

            handler.reply_message = MagicMock()
            handler.reply_message_with_id = MagicMock(return_value="reply_1")
            handler.add_reaction = MagicMock()
            handler.register_message_project = MagicMock()
            handler.record_mode_transition = MagicMock()
            handler.inject_bridge_context = MagicMock(side_effect=lambda t, p: t)
            handler.get_working_dir = MagicMock(return_value="/tmp/test")
            handler.ensure_request_id = MagicMock(return_value="req-1")

            mock_project = MagicMock()
            mock_project.project_id = "proj-1"
            mock_project.root_path = "/tmp/test"

            mock_lock_mgr = MagicMock()
            mock_lock_result = MagicMock()
            mock_lock_result.success = True
            mock_lock_mgr.acquire.return_value = mock_lock_result
            mock_ctx.repo_lock_manager = mock_lock_mgr

            mock_session = MagicMock()
            mock_session.session_id = "sess-1"
            mock_session.message_count = 1
            mock_session.last_query = "test"
            mock_ctx.coco_manager.get_session.return_value = mock_session

            mock_streaming = MagicMock()
            mock_streaming.create_streaming_card.return_value = None
            handler.get_streaming_manager = MagicMock(return_value=mock_streaming)

            handler.handle_message("msg-1", "chat-1", "hello", project=mock_project)

            # is_p2p=False → acquire should be called
            mock_lock_mgr.acquire.assert_called_once()
        finally:
            set_current_is_p2p(False)

    def test_engine_base_reads_threadlocal_p2p(self):
        """_safe_execute_engine() without explicit is_p2p should read thread-local."""
        from unittest.mock import MagicMock
        from src.thread import set_current_is_p2p

        set_current_is_p2p(True)
        try:
            mock_ctx = MagicMock()

            from src.feishu.handlers.engine_base import BaseEngineHandler
            handler = BaseEngineHandler(mock_ctx)

            mock_project = MagicMock()
            mock_project.root_path = "/tmp/test"

            mock_lock_mgr = MagicMock()
            mock_lock_result = MagicMock()
            mock_lock_result.success = True
            mock_lock_mgr.hold.return_value.__enter__ = MagicMock(return_value=mock_lock_result)
            mock_lock_mgr.hold.return_value.__exit__ = MagicMock(return_value=False)
            mock_ctx.repo_lock_manager = mock_lock_mgr

            handler.reply_message = MagicMock()

            executor = MagicMock()

            # Call without is_p2p — should read True from thread-local
            handler._safe_execute_engine(
                executor_func=executor,
                task_id="task-1",
                chat_id="chat-1",
                message_id="msg-1",
                project=mock_project,
                engine_name="TestEngine",
                reporter=MagicMock(),
                request_id="req-1",
            )

            mock_lock_mgr.hold.assert_called_once()
            call_kwargs = mock_lock_mgr.hold.call_args
            assert call_kwargs[1].get("is_p2p") is True
        finally:
            set_current_is_p2p(False)

    def test_system_worktree_execute_reads_threadlocal_p2p(self):
        """handle_worktree_execute() without explicit is_p2p should read thread-local."""
        from unittest.mock import MagicMock
        from src.thread import set_current_is_p2p

        set_current_is_p2p(True)
        try:
            mock_ctx = MagicMock()
            mock_ctx.settings.thread_programming_enabled = False

            from src.feishu.handlers.system import SystemHandler
            handler = SystemHandler(mock_ctx)

            mock_project = MagicMock()
            mock_project.project_id = "proj-1"
            mock_project.root_path = "/tmp/test"
            mock_ctx.project_manager.get_active_project.return_value = mock_project

            mock_lock_mgr = MagicMock()
            mock_lock_result = MagicMock()
            mock_lock_result.success = True
            mock_lock_mgr.hold.return_value.__enter__ = MagicMock(return_value=mock_lock_result)
            mock_lock_mgr.hold.return_value.__exit__ = MagicMock(return_value=False)
            mock_ctx.repo_lock_manager = mock_lock_mgr

            handler.reply_message = MagicMock()
            handler.send_message = MagicMock(return_value="progress-mid")
            handler.patch_message = MagicMock()

            # Mock worktree manager
            mock_wt_mgr = MagicMock()
            mock_state = MagicMock()
            mock_state.units = []
            mock_state.last_error = None
            mock_state.merge_entry_ready = False
            mock_wt_mgr.get_state.return_value = mock_state
            mock_wt_mgr.execute_goal.return_value = mock_state
            handler._worktree_manager = MagicMock(return_value=mock_wt_mgr)

            # Call without is_p2p — should read True from thread-local
            handler.handle_worktree_execute("msg-1", "chat-1", "fix bug", project=mock_project)

            mock_lock_mgr.hold.assert_called_once()
            call_kwargs = mock_lock_mgr.hold.call_args
            assert call_kwargs[1].get("is_p2p") is True
        finally:
            set_current_is_p2p(False)

    def test_default_threadlocal_is_false(self):
        """When thread-local is not set, is_p2p should default to False."""
        from unittest.mock import MagicMock
        from src.thread import set_current_is_p2p
        from src.thread.manager import _current_is_p2p

        # Ensure thread-local is cleared
        if hasattr(_current_is_p2p, "value"):
            delattr(_current_is_p2p, "value")
        try:
            mock_ctx = MagicMock()

            from src.feishu.handlers.engine_base import BaseEngineHandler
            handler = BaseEngineHandler(mock_ctx)

            mock_project = MagicMock()
            mock_project.root_path = "/tmp/test"

            mock_lock_mgr = MagicMock()
            mock_lock_result = MagicMock()
            mock_lock_result.success = True
            mock_lock_mgr.hold.return_value.__enter__ = MagicMock(return_value=mock_lock_result)
            mock_lock_mgr.hold.return_value.__exit__ = MagicMock(return_value=False)
            mock_ctx.repo_lock_manager = mock_lock_mgr

            handler.reply_message = MagicMock()

            handler._safe_execute_engine(
                executor_func=MagicMock(),
                task_id="task-1",
                chat_id="chat-1",
                message_id="msg-1",
                project=mock_project,
                engine_name="TestEngine",
                reporter=MagicMock(),
                request_id="req-1",
            )

            mock_lock_mgr.hold.assert_called_once()
            call_kwargs = mock_lock_mgr.hold.call_args
            assert call_kwargs[1].get("is_p2p") is False
        finally:
            set_current_is_p2p(False)

    def test_sandbox_executor_no_independent_lock(self):
        """SandboxExecutor.execute() should NOT acquire repo lock independently.

        After the refactor, SandboxExecutor only logs a warning sentinel if the
        lock is held by a different chat — it no longer calls hold().
        """
        from unittest.mock import MagicMock, patch
        from src.sandbox.executor import SandboxExecutor

        mock_lock_mgr = MagicMock()
        mock_lock_mgr.get_lock_info.return_value = None  # no lock held

        with patch("src.sandbox.executor.get_repo_lock_manager", return_value=mock_lock_mgr, create=True):
            with patch("src.repo_lock.get_repo_lock_manager", return_value=mock_lock_mgr):
                mock_subprocess = MagicMock()
                mock_process = MagicMock()
                mock_process.returncode = 0
                mock_process.stdout = ""
                mock_process.stderr = ""
                mock_subprocess.run.return_value = mock_process

                executor = SandboxExecutor(subprocess_executor=mock_subprocess)
                result = executor.execute("echo test", cwd="/tmp/test", chat_id="chat-1")

                # hold() should NOT be called — no independent lock acquisition
                mock_lock_mgr.hold.assert_not_called()
                assert result.success is True


# ---------------------------------------------------------------------------
# hold() context manager tests (replaces former RepoLockGuard tests)
# ---------------------------------------------------------------------------


class TestRepoLockHold:
    """Tests for RepoLockManager.hold() — context-manager lock coordination."""

    def test_hold_no_root_path(self, mgr: RepoLockManager):
        """When root_path is empty, hold() still succeeds (p2p-like bypass)."""
        # hold() with empty path should not raise; body can execute freely.
        body = MagicMock(return_value="ok")
        with mgr.hold("", "chat_A") as lock_result:
            result = body() if lock_result.success else None
        # Empty path → acquire returns success immediately (no-op lock)
        assert body.called

    def test_hold_success(self, mgr: RepoLockManager):
        """Body executes under lock via hold() and lock is released on exit."""
        body = MagicMock(return_value=42)
        with mgr.hold("/tmp/repo", "chat_A") as lock_result:
            assert lock_result.success
            result = body()
        assert result == 42
        body.assert_called_once()
        # Lock should be released after context exit
        assert mgr.list_locks() == []

    def test_hold_conflict(self, mgr: RepoLockManager):
        """When another chat holds the lock, hold() raises LockConflictError."""
        mgr.acquire("/tmp/repo", "chat_A")
        body = MagicMock()
        with pytest.raises(LockConflictError) as exc_info:
            with mgr.hold("/tmp/repo", "chat_B"):
                body()
        body.assert_not_called()
        assert exc_info.value.holder_chat_id == "chat_A"

    def test_hold_conflict_raises_lock_conflict_error(self, mgr: RepoLockManager):
        """hold() raises LockConflictError with correct attributes on conflict."""
        mgr.acquire("/tmp/repo", "chat_A")
        with pytest.raises(LockConflictError) as exc_info:
            with mgr.hold("/tmp/repo", "chat_B"):
                pass
        assert exc_info.value.holder_chat_id == "chat_A"
        assert exc_info.value.root_path == "/tmp/repo"

    def test_hold_p2p_bypass(self, mgr: RepoLockManager):
        """With is_p2p=True, lock conflict is bypassed via hold()."""
        mgr.acquire("/tmp/repo", "chat_A")
        body = MagicMock(return_value="bypassed")
        with mgr.hold("/tmp/repo", "chat_B", is_p2p=True) as lock_result:
            assert lock_result.success
            result = body()
        assert result == "bypassed"
        body.assert_called_once()

    def test_hold_body_exception_releases_lock(self, mgr: RepoLockManager):
        """When body raises, lock is still released on context exit."""
        with pytest.raises(ValueError, match="boom"):
            with mgr.hold("/tmp/repo", "chat_A") as lock_result:
                assert lock_result.success
                raise ValueError("boom")
        # Lock must be released despite the exception
        assert mgr.list_locks() == []


# ---------------------------------------------------------------------------
# Lazy cleanup thread tests
# ---------------------------------------------------------------------------


class TestLazyCleanupThread:
    """Task 23: RepoLockManager cleanup thread is lazily started."""

    def test_no_thread_on_construction(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999)
        try:
            assert m._cleanup_thread is None
        finally:
            m.shutdown()

    def test_thread_starts_on_first_acquire(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999)
        try:
            m.acquire("/tmp/repo", "chat_A")
            assert m._cleanup_thread is not None
            assert m._cleanup_thread.is_alive()
        finally:
            m.shutdown()

    def test_shutdown_clears_thread_ref(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999)
        m.acquire("/tmp/repo", "chat_A")
        assert m._cleanup_thread is not None
        m.shutdown()
        assert m._cleanup_thread is None
# Concurrency tests
# ---------------------------------------------------------------------------


class TestRepoLockConcurrency:
    """Thread-safety tests for RepoLockManager under concurrent access."""

    def test_concurrent_acquire_same_path(self, mgr: RepoLockManager):
        """Only one chat wins when multiple threads race to acquire the same path."""
        results: dict[str, AcquireResult] = {}
        barrier = threading.Barrier(10)

        def try_acquire(chat_id):
            barrier.wait()
            results[chat_id] = mgr.acquire("/tmp/contested", chat_id)

        threads = [threading.Thread(target=try_acquire, args=(f"chat_{i}",)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        winners = [cid for cid, r in results.items() if r.success]
        losers = [cid for cid, r in results.items() if not r.success]
        assert len(winners) == 1
        assert len(losers) == 9

    def test_concurrent_acquire_release(self, mgr: RepoLockManager):
        """Rapid acquire/release cycles from multiple threads don't corrupt state."""
        errors = []

        def cycle(chat_id, path):
            try:
                for _ in range(50):
                    r = mgr.acquire(path, chat_id)
                    if r.success:
                        mgr.release(path, chat_id)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=cycle, args=(f"chat_{i}", f"/tmp/repo_{i % 3}"))
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"

    def test_concurrent_hold_execute(self, mgr: RepoLockManager):
        """hold() from multiple threads on different paths is safe."""
        results = []

        def run(idx):
            with mgr.hold(f"/tmp/repo_{idx}", f"chat_{idx}") as lock_result:
                results.append(idx)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == list(range(10))

    def test_cleanup_concurrent_with_acquire_release(self, mgr: RepoLockManager):
        """_cleanup_idle running concurrently with acquire/release must not corrupt state."""
        mgr._idle_timeout = 0
        mgr._hard_timeout = 0
        errors = []

        def cleanup_loop():
            try:
                for _ in range(50):
                    mgr._cleanup_idle()
            except Exception as e:
                errors.append(e)

        def acquire_release_loop(chat_id, path):
            try:
                for _ in range(50):
                    r = mgr.acquire(path, chat_id)
                    if r.success:
                        mgr.touch(path, chat_id)
                        mgr.release(path, chat_id)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=cleanup_loop)]
        threads += [
            threading.Thread(target=acquire_release_loop, args=(f"chat_{i}", f"/tmp/repo_{i % 3}"))
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Unexpected errors: {errors}"


class TestRepoLockInfoFrozen:
    """AC-R03: RepoLockInfo must be immutable (frozen dataclass)."""

    def test_frozen_raises_on_assignment(self):
        info = RepoLockInfo(
            root_path="/tmp/test",
            chat_id="chat1",
            refcount=1,
            acquired_at=100.0,
            last_active_time=200.0,
            idle_seconds=5.0,
        )
        with pytest.raises(AttributeError):
            info.chat_id = "chat2"
        with pytest.raises(AttributeError):
            info.refcount = 99


class TestCleanupLoopResilience:
    """_cleanup_loop must survive exceptions from _cleanup_idle."""

    def test_cleanup_loop_survives_exception(self, mgr: RepoLockManager):
        """If _cleanup_idle raises, the loop continues and processes the next cycle."""
        call_count = 0

        original_cleanup_idle = mgr._cleanup_idle

        def flaky_cleanup():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated failure")
            # Second call succeeds, then stop the loop
            mgr._stop_event.set()
            original_cleanup_idle()

        mgr._cleanup_interval = 0.01  # fast iteration for testing
        mgr._cleanup_idle = flaky_cleanup  # type: ignore[assignment]

        # Run _cleanup_loop directly (blocks until _stop_event is set)
        mgr._cleanup_loop()

        assert call_count == 2, f"Expected 2 calls, got {call_count}"

    def test_cleanup_loop_logs_error_on_exception(self, mgr: RepoLockManager, caplog):
        """Exception in _cleanup_idle is logged via logger.error with traceback."""
        import logging

        def failing_cleanup():
            mgr._stop_event.set()
            raise RuntimeError("boom")

        mgr._cleanup_interval = 0.01
        mgr._cleanup_idle = failing_cleanup  # type: ignore[assignment]

        with caplog.at_level(logging.ERROR, logger="src.repo_lock"):
            mgr._cleanup_loop()

        assert any("RepoLock: cleanup error" in r.message for r in caplog.records)
        assert any("boom" in (r.exc_text or "") for r in caplog.records)


# ---------------------------------------------------------------------------
# Hard-timeout callback tests
# ---------------------------------------------------------------------------


class TestHardTimeoutCallback:
    """Verify on_hard_timeout_reclaim callback invocation and exception safety."""

    def test_callback_invoked_on_hard_timeout(self):
        """Phase 2 hard-timeout eviction invokes the registered callback with (path, chat_id)."""
        reclaimed: list[tuple[str, str]] = []

        def on_reclaim(path: str, chat_id: str) -> None:
            reclaimed.append((path, chat_id))

        m = RepoLockManager(
            idle_timeout=999999,
            cleanup_interval=9999,
            hard_timeout=0,
            on_hard_timeout_reclaim=on_reclaim,
        )
        try:
            m.acquire("/tmp/repo_cb", "chat_X")
            m._cleanup_idle()

            assert len(reclaimed) == 1
            assert reclaimed[0] == ("/tmp/repo_cb", "chat_X")
            # Lock should be evicted
            assert m.list_locks() == []
        finally:
            m.shutdown()

    def test_callback_exception_does_not_crash_cleanup(self, caplog):
        """If the callback raises, cleanup still completes and the error is logged."""
        import logging

        def bad_callback(path: str, chat_id: str) -> None:
            raise RuntimeError("callback exploded")

        m = RepoLockManager(
            idle_timeout=999999,
            cleanup_interval=9999,
            hard_timeout=0,
            on_hard_timeout_reclaim=bad_callback,
        )
        try:
            m.acquire("/tmp/repo_cb2", "chat_Y")

            with caplog.at_level(logging.ERROR, logger="src.repo_lock"):
                m._cleanup_idle()  # must not raise

            # Lock should still be evicted despite callback failure
            assert m.list_locks() == []

            # Error should be logged
            assert any("SimpleEvent callback failed" in r.message for r in caplog.records)
        finally:
            m.shutdown()


# ---------------------------------------------------------------------------
# AC-R02: Orphan token cleanup + capacity limit
# ---------------------------------------------------------------------------

class TestOrphanTokenCleanup:
    """Verify _cleanup_idle removes orphan token mappings (Phase 4)."""

    def test_cleanup_removes_orphan_tokens(self):
        """path_to_token without acquire creates orphan; cleanup removes it."""
        m = RepoLockManager(idle_timeout=1, cleanup_interval=999)
        try:
            # Create a token mapping without acquiring a lock
            token = m.path_to_token("/tmp/orphan_repo")
            assert token
            assert m.token_to_path(token) is not None

            # Run cleanup — should purge the orphan mapping
            m._cleanup_idle()

            assert m.token_to_path(token) is None
            with m._mu:
                assert len(m._path_to_token) == 0
                assert len(m._token_to_path) == 0
        finally:
            m.shutdown()

    def test_cleanup_preserves_active_lock_tokens(self):
        """Tokens for paths with active locks must NOT be cleaned."""
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999)
        try:
            m.acquire("/tmp/active_repo", "chatA")
            token = m.path_to_token("/tmp/active_repo")
            # Also create an orphan
            orphan_token = m.path_to_token("/tmp/orphan_repo2")

            m._cleanup_idle()

            # Active token survives
            assert m.token_to_path(token) is not None
            # Orphan is removed
            assert m.token_to_path(orphan_token) is None
        finally:
            m.shutdown()


class TestTokenMappingCapacityLimit:
    """Verify path_to_token enforces _TOKEN_MAP_CAPACITY."""

    def test_capacity_limit_purges_orphans(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999)
        # Lower capacity for testing
        m._TOKEN_MAP_CAPACITY = 10
        try:
            # Fill up with orphan tokens (no acquire)
            for i in range(10):
                m.path_to_token(f"/tmp/cap_repo_{i}")
            with m._mu:
                assert len(m._token_to_path) == 10

            # Next token should trigger orphan purge (all are orphans)
            new_token = m.path_to_token("/tmp/cap_repo_new")
            assert new_token
            with m._mu:
                # Only the newly added one should remain
                assert len(m._token_to_path) == 1
        finally:
            m.shutdown()

    def test_capacity_limit_with_active_locks(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999)
        m._TOKEN_MAP_CAPACITY = 5
        try:
            # Acquire 5 locks and map them (fills capacity with non-orphans)
            for i in range(5):
                m.acquire(f"/tmp/locked_repo_{i}", "chatA")
                m.path_to_token(f"/tmp/locked_repo_{i}")

            # Adding one more when capacity is full of active locks
            # should return token without caching
            extra_token = m.path_to_token("/tmp/extra_repo")
            assert extra_token  # token is still returned
            # But it should NOT be cached (capacity full, no orphans to purge)
            assert m.token_to_path(extra_token) is None
        finally:
            m.shutdown()


# ======================================================================
# FS-24: RepoLock double-release tests
# ======================================================================


class TestRepoLockDoubleRelease:
    """Verify that double-releasing a repo lock is safe (no exception, no side-effects)."""

    def test_double_release_same_chat(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999, hard_timeout=9999)
        try:
            result = m.acquire("/tmp/dr_repo", "chat_a")
            assert result.success
            m.release("/tmp/dr_repo", "chat_a")
            # Second release should be a no-op (entry already removed)
            m.release("/tmp/dr_repo", "chat_a")
            assert m.get_lock_info("/tmp/dr_repo") is None
        finally:
            m.shutdown()

    def test_release_wrong_chat_is_noop(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999, hard_timeout=9999)
        try:
            m.acquire("/tmp/dr_repo2", "chat_a")
            # Another chat tries to release — should be silently ignored
            m.release("/tmp/dr_repo2", "chat_b")
            info = m.get_lock_info("/tmp/dr_repo2")
            assert info is not None
            assert info.chat_id == "chat_a"
        finally:
            m.shutdown()

    def test_release_nonexistent_path_is_noop(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999, hard_timeout=9999)
        try:
            # Release a path that was never acquired
            m.release("/tmp/never_acquired", "chat_x")
            # Should not raise
        finally:
            m.shutdown()

    def test_force_release_already_released(self):
        m = RepoLockManager(idle_timeout=999, cleanup_interval=999, hard_timeout=9999)
        try:
            m.acquire("/tmp/fr_repo", "chat_a")
            m.force_release("/tmp/fr_repo")
            # Double force_release — should be safe
            m.force_release("/tmp/fr_repo")
            assert m.get_lock_info("/tmp/fr_repo") is None
        finally:
            m.shutdown()
