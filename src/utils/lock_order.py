"""Runtime lock-ordering violation detector.

Implements the partial order defined in ``docs/adr-lock-ordering.md``::

    ProjectManager._lock  (level 1, outermost)
      → ProjectContext._chat_lock  (level 2)
        → ChatLockManager._mu  (level 3)
          → RepoLockManager._mu  (level 4, innermost)

Each thread tracks the *highest* level it currently holds.  Acquiring a lock
whose level is ≤ the current max constitutes a potential deadlock and is
logged as a warning.

Usage::

    from src.utils.lock_order import ordered_lock, LockLevel

    class MyManager:
        def __init__(self):
            self._mu = ordered_lock(LockLevel.REPO_LOCK)

        def do_work(self):
            with self._mu:
                ...

The wrapper is zero-cost when ``GHOSTAP_LOCK_ORDER_CHECK`` env-var is unset
or falsy (the default).  Set it to ``1`` to enable checking in development /
CI.
"""

from __future__ import annotations

import logging
import os
import threading
from enum import IntEnum
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lock levels — lower numeric value = outer (acquired first).
# ---------------------------------------------------------------------------


class LockLevel(IntEnum):
    """Canonical lock ordering levels.

    Values correspond to the ADR-defined partial order.  Lower = outer.
    """

    PROJECT_MANAGER = 1  # ProjectManager._lock (RLock)
    CHAT_LOCK_CTX = 2  # ProjectContext._chat_lock
    CHAT_LOCK_MGR = 3  # ChatLockManager._mu
    REPO_LOCK = 4  # RepoLockManager._mu


# ---------------------------------------------------------------------------
# Thread-local bookkeeping
# ---------------------------------------------------------------------------

_tls = threading.local()

_ENABLED: Optional[bool] = None


def _is_enabled() -> bool:
    """Return True when lock-order checking is active (cached)."""
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = os.environ.get("GHOSTAP_LOCK_ORDER_CHECK", "").strip() in {"1", "true", "yes"}
    return _ENABLED


def enable_lock_order_check() -> None:
    """Programmatically enable lock-order checking (useful for tests)."""
    global _ENABLED
    _ENABLED = True


def disable_lock_order_check() -> None:
    """Programmatically disable lock-order checking."""
    global _ENABLED
    _ENABLED = False


def _get_held() -> list[int]:
    """Return the per-thread held-levels stack."""
    held = getattr(_tls, "held", None)
    if held is None:
        held = []
        _tls.held = held
    return held


def _on_acquire(level: int, name: str) -> None:
    """Called just before a lock is acquired."""
    held = _get_held()
    if held:
        max_held = max(held)
        if level <= max_held:
            logger.warning(
                "Lock ordering violation: acquiring %s (level=%d) while holding level=%d "
                "(thread=%s). See docs/adr-lock-ordering.md.",
                name,
                level,
                max_held,
                threading.current_thread().name,
            )
    held.append(level)


def _on_release(level: int) -> None:
    """Called just after a lock is released."""
    held = _get_held()
    try:
        held.remove(level)
    except ValueError:
        pass  # defensive


# ---------------------------------------------------------------------------
# Ordered lock wrapper
# ---------------------------------------------------------------------------


class _OrderedLock:
    """Thin wrapper around ``threading.Lock`` with ordering checks."""

    __slots__ = ("_lock", "_level", "_name")

    def __init__(self, level: LockLevel, name: str = ""):
        self._lock = threading.Lock()
        self._level = int(level)
        self._name = name or level.name

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if _is_enabled():
            _on_acquire(self._level, self._name)
        return self._lock.acquire(blocking=blocking, timeout=timeout)

    def release(self) -> None:
        try:
            self._lock.release()
        finally:
            if _is_enabled():
                _on_release(self._level)

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()

    # threading.Lock protocol
    def locked(self) -> bool:
        return self._lock.locked()


class _OrderedRLock:
    """Thin wrapper around ``threading.RLock`` with ordering checks.

    Only checks ordering on the *first* (outermost) acquisition per thread.
    """

    __slots__ = ("_lock", "_level", "_name", "_owners")

    def __init__(self, level: LockLevel, name: str = ""):
        self._lock = threading.RLock()
        self._level = int(level)
        self._name = name or level.name
        self._owners: dict[int, int] = {}  # thread-ident → depth

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        tid = threading.get_ident()
        depth = self._owners.get(tid, 0)
        if depth == 0 and _is_enabled():
            _on_acquire(self._level, self._name)
        result = self._lock.acquire(blocking=blocking, timeout=timeout)
        if result:
            self._owners[tid] = depth + 1
        return result

    def release(self) -> None:
        tid = threading.get_ident()
        depth = self._owners.get(tid, 1)
        try:
            self._lock.release()
        finally:
            new_depth = depth - 1
            if new_depth <= 0:
                self._owners.pop(tid, None)
                if _is_enabled():
                    _on_release(self._level)
            else:
                self._owners[tid] = new_depth

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


def ordered_lock(level: LockLevel, *, name: str = "") -> _OrderedLock:
    """Create a ``threading.Lock``-compatible object with ordering checks."""
    return _OrderedLock(level, name)


def ordered_rlock(level: LockLevel, *, name: str = "") -> _OrderedRLock:
    """Create a ``threading.RLock``-compatible object with ordering checks."""
    return _OrderedRLock(level, name)
