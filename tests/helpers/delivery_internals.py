"""Test helper for inspecting CardDelivery / SessionLockPool internals.

Centralizes all access to private pool attributes so that refactoring
the pool only requires updating this single file.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.card.delivery.engine import CardDelivery
    from src.card.delivery.lock_pool import SessionLockPool


class DeliveryInspector:
    """Facade that exposes SessionLockPool internals for test assertions.

    Usage::

        inspector = DeliveryInspector.from_delivery(delivery)
        # or
        inspector = DeliveryInspector(pool)
    """

    def __init__(self, pool: SessionLockPool) -> None:
        self._pool = pool

    @classmethod
    def from_delivery(cls, delivery: CardDelivery) -> DeliveryInspector:
        """Create inspector from a CardDelivery instance."""
        return cls(delivery._lock_pool)

    # ----- Attribute access -----

    @property
    def lock(self) -> threading.Lock:
        """The global pool lock."""
        return self._pool._lock

    @property
    def session_locks(self) -> dict[str, threading.RLock]:
        """The session_id -> RLock mapping."""
        return self._pool._session_locks

    @property
    def timestamps(self) -> OrderedDict[str, float]:
        """The session_id -> monotonic timestamp LRU mapping."""
        return self._pool._timestamps

    @property
    def eviction_thread(self) -> threading.Thread:
        """The background eviction thread."""
        return self._pool._eviction_thread

    @property
    def eviction_stop(self) -> threading.Event:
        """The stop event for the eviction thread."""
        return self._pool._eviction_stop

    @property
    def has_active_binding(self) -> Callable[[str], bool]:
        """The binding checker callback."""
        return self._pool._has_active_binding

    @has_active_binding.setter
    def has_active_binding(self, value: Callable[[str], bool]) -> None:
        """Set the binding checker callback (for test setup)."""
        self._pool._has_active_binding = value

    @property
    def in_flight_count(self) -> int:
        """Current in-flight delivery count."""
        return self._pool._in_flight_count

    @property
    def accepting_work_event(self) -> threading.Event:
        """The underlying accepting_work Event."""
        return self._pool._accepting_work

    @property
    def in_flight_condition(self) -> threading.Condition:
        """The in-flight condition variable."""
        return self._pool._in_flight_condition

    # ----- Method delegates -----

    def lru_evict_oldest(self) -> None:
        """Delegate to pool._lru_evict_oldest(). Caller MUST hold pool._lock."""
        self._pool._lru_evict_oldest()

    def evict_stale_two_phase(self) -> int:
        """Delegate to pool._evict_stale_two_phase()."""
        return self._pool._evict_stale_two_phase()
