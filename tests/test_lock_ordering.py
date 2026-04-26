"""FS-07 / FS-01: Tests for src/utils/lock_order.py — lock ordering detection."""

from __future__ import annotations

import logging
import threading

import pytest

from src.utils.lock_order import (
    LockLevel,
    _get_held,
    _on_acquire,
    _on_release,
    disable_lock_order_check,
    enable_lock_order_check,
    ordered_lock,
    ordered_rlock,
)


@pytest.fixture(autouse=True)
def _enable_and_cleanup():
    """Enable lock-order checking and clear thread-local state."""
    enable_lock_order_check()
    _get_held().clear()
    yield
    _get_held().clear()
    disable_lock_order_check()


class TestLockLevel:
    def test_ordering_values(self):
        assert LockLevel.PROJECT_MANAGER < LockLevel.CHAT_LOCK_CTX
        assert LockLevel.CHAT_LOCK_CTX < LockLevel.CHAT_LOCK_MGR
        assert LockLevel.CHAT_LOCK_MGR < LockLevel.REPO_LOCK

    def test_all_levels_distinct(self):
        values = [int(v) for v in LockLevel]
        assert len(values) == len(set(values))


class TestOrderedLock:
    def test_basic_acquire_release(self):
        lock = ordered_lock(LockLevel.REPO_LOCK)
        lock.acquire()
        assert lock.locked()
        lock.release()
        assert not lock.locked()

    def test_context_manager(self):
        lock = ordered_lock(LockLevel.REPO_LOCK)
        with lock:
            assert lock.locked()
        assert not lock.locked()

    def test_correct_order_no_warning(self, caplog):
        outer = ordered_lock(LockLevel.PROJECT_MANAGER, name="outer")
        inner = ordered_lock(LockLevel.REPO_LOCK, name="inner")
        with caplog.at_level(logging.WARNING):
            with outer:
                with inner:
                    pass
        assert "Lock ordering violation" not in caplog.text

    def test_reverse_order_logs_warning(self, caplog):
        outer = ordered_lock(LockLevel.REPO_LOCK, name="inner_first")
        inner = ordered_lock(LockLevel.PROJECT_MANAGER, name="outer_second")
        with caplog.at_level(logging.WARNING):
            with outer:
                with inner:
                    pass
        assert "Lock ordering violation" in caplog.text
        assert "outer_second" in caplog.text

    def test_same_level_logs_warning(self, caplog):
        lock_a = ordered_lock(LockLevel.REPO_LOCK, name="a")
        lock_b = ordered_lock(LockLevel.REPO_LOCK, name="b")
        with caplog.at_level(logging.WARNING):
            with lock_a:
                with lock_b:
                    pass
        assert "Lock ordering violation" in caplog.text


class TestOrderedRLock:
    def test_reentrant_no_double_warning(self, caplog):
        rlock = ordered_rlock(LockLevel.PROJECT_MANAGER, name="pm")
        with caplog.at_level(logging.WARNING):
            with rlock:
                with rlock:
                    pass
        assert "Lock ordering violation" not in caplog.text

    def test_rlock_then_inner_correct_order(self, caplog):
        outer = ordered_rlock(LockLevel.PROJECT_MANAGER, name="pm")
        inner = ordered_lock(LockLevel.REPO_LOCK, name="repo")
        with caplog.at_level(logging.WARNING):
            with outer:
                with inner:
                    pass
        assert "Lock ordering violation" not in caplog.text


class TestDisabledChecking:
    def test_no_warning_when_disabled(self, caplog):
        disable_lock_order_check()
        outer = ordered_lock(LockLevel.REPO_LOCK, name="inner_first")
        inner = ordered_lock(LockLevel.PROJECT_MANAGER, name="outer_second")
        with caplog.at_level(logging.WARNING):
            with outer:
                with inner:
                    pass
        assert "Lock ordering violation" not in caplog.text


class TestIntegrationWithRealManagers:
    def test_project_manager_lock_level(self):
        """ProjectManager._lock should be at level 1."""
        from src.project.manager import ProjectManager
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("GHOSTAP_LOCK_ORDER_CHECK", "1")
            pm = ProjectManager()
            assert pm._lock._level == int(LockLevel.PROJECT_MANAGER)

    def test_repo_lock_manager_lock_level(self):
        """RepoLockManager._mu should be at level 4."""
        from src.repo_lock import RepoLockManager, _reset_repo_lock_manager_for_testing
        try:
            mgr = RepoLockManager(idle_timeout=999, cleanup_interval=999, hard_timeout=9999)
            assert mgr._mu._level == int(LockLevel.REPO_LOCK)
        finally:
            mgr.shutdown()
