"""Contract tests for lock ordering partial order invariants.

These tests act as guardrails for future simplification of src/utils/lock_order.py,
src/chat_lock.py, and src/repo_lock.py. They assert structural invariants of the
lock hierarchy (enum membership, ordering semantics, violation detection) without
testing internal implementation details.

Protected regression scenarios:
- LockLevel enum must have exactly 6 members with stable name→value mapping
- Acquiring locks in ascending order must not trigger violations
- Acquiring locks in descending order must trigger RuntimeError in strict mode
- Factory functions must return correct wrapper types
"""

from __future__ import annotations

import threading

import pytest

from src.utils.lock_order import (
    LockLevel,
    _OrderedLock,
    _OrderedRLock,
    disable_lock_order_check,
    enable_lock_order_check,
    ordered_lock,
    ordered_rlock,
)


# ---------------------------------------------------------------------------
# Enum stability assertions
# ---------------------------------------------------------------------------

_EXPECTED_MEMBERS = {
    "ENGINE_MANAGER": -1,
    "ENGINE_INSTANCE": 0,
    "PROJECT_MANAGER": 1,
    "CHAT_LOCK_CTX": 2,
    "CHAT_LOCK_MGR": 3,
    "REPO_LOCK": 4,
}


class TestLockLevelEnumStability:
    """Assert LockLevel enum membership and values are stable."""

    def test_member_count(self) -> None:
        """LockLevel must have exactly 6 members."""
        assert len(LockLevel) == 6

    def test_member_names_and_values(self) -> None:
        """Each member name must map to the expected integer value."""
        actual = {member.name: member.value for member in LockLevel}
        assert actual == _EXPECTED_MEMBERS

    def test_values_are_strictly_ascending(self) -> None:
        """Values must form a strict total order from -1 to 4."""
        values = sorted(member.value for member in LockLevel)
        assert values == [-1, 0, 1, 2, 3, 4]

    def test_all_members_are_int(self) -> None:
        """All LockLevel members must be integers (IntEnum contract)."""
        for member in LockLevel:
            assert isinstance(member.value, int)


# ---------------------------------------------------------------------------
# Ordering semantics
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _strict_lock_check():
    """Enable strict lock-order checking for the duration of a test."""
    enable_lock_order_check(strict=True)
    try:
        yield
    finally:
        disable_lock_order_check()


class TestLockOrderingSemantics:
    """Assert correct ordering detection behavior."""

    @pytest.mark.usefixtures("_strict_lock_check")
    def test_ascending_acquisition_no_violation(self) -> None:
        """Acquiring locks in ascending level order must not raise."""
        locks = [ordered_lock(level) for level in sorted(LockLevel, key=lambda x: x.value)]
        for lock in locks:
            lock.acquire()
        # Release in reverse order
        for lock in reversed(locks):
            lock.release()

    @pytest.mark.usefixtures("_strict_lock_check")
    def test_descending_acquisition_raises_in_strict(self) -> None:
        """Acquiring a lower-level lock while holding a higher-level lock must raise RuntimeError."""
        outer = ordered_lock(LockLevel.REPO_LOCK, name="test_outer")
        inner = ordered_lock(LockLevel.ENGINE_MANAGER, name="test_inner")
        outer.acquire()
        try:
            with pytest.raises(RuntimeError, match="Lock ordering violation"):
                inner.acquire()
        finally:
            outer.release()

    @pytest.mark.usefixtures("_strict_lock_check")
    def test_equal_level_acquisition_raises_in_strict(self) -> None:
        """Acquiring a lock at the same level as one already held must raise RuntimeError."""
        lock_a = ordered_lock(LockLevel.PROJECT_MANAGER, name="a")
        lock_b = ordered_lock(LockLevel.PROJECT_MANAGER, name="b")
        lock_a.acquire()
        try:
            with pytest.raises(RuntimeError, match="Lock ordering violation"):
                lock_b.acquire()
        finally:
            lock_a.release()

    @pytest.mark.usefixtures("_strict_lock_check")
    def test_rlock_reentrant_no_violation(self) -> None:
        """RLock re-acquisition by same thread at same level must NOT raise (reentrant)."""
        rlock = ordered_rlock(LockLevel.ENGINE_INSTANCE, name="reentrant_test")
        rlock.acquire()
        try:
            # Second acquisition by same thread should succeed (RLock semantics)
            rlock.acquire()
            rlock.release()
        finally:
            rlock.release()


# ---------------------------------------------------------------------------
# Factory function type contracts
# ---------------------------------------------------------------------------


class TestFactoryFunctions:
    """Assert factory functions return correct wrapper types."""

    def test_ordered_lock_returns_ordered_lock_type(self) -> None:
        lock = ordered_lock(LockLevel.REPO_LOCK)
        assert isinstance(lock, _OrderedLock)

    def test_ordered_rlock_returns_ordered_rlock_type(self) -> None:
        rlock = ordered_rlock(LockLevel.ENGINE_INSTANCE)
        assert isinstance(rlock, _OrderedRLock)

    def test_ordered_lock_supports_context_manager(self) -> None:
        """_OrderedLock must support 'with' statement."""
        lock = ordered_lock(LockLevel.CHAT_LOCK_CTX)
        with lock:
            pass  # Must not raise

    def test_ordered_rlock_supports_context_manager(self) -> None:
        """_OrderedRLock must support 'with' statement."""
        rlock = ordered_rlock(LockLevel.CHAT_LOCK_MGR)
        with rlock:
            pass  # Must not raise

    def test_ordered_lock_has_locked_method(self) -> None:
        """_OrderedLock must expose locked() for threading.Lock compatibility."""
        lock = ordered_lock(LockLevel.REPO_LOCK)
        assert hasattr(lock, "locked")
        assert lock.locked() is False
