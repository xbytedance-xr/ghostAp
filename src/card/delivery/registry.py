"""DeliveryRegistry: process-level registry for CardDelivery instances.

Extracted from CardDelivery ClassVar state to enable test isolation.
The module-level singleton `delivery_registry` is used by CardDelivery
for lifecycle tracking; tests can call `delivery_registry.reset()` to
cleanly restore state between test cases.

NOTE: atexit handler is automatically installed on first register() call.
Explicit `delivery_registry.install_atexit()` is still supported but no longer required.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.card.delivery.engine import CardDelivery

logger = logging.getLogger(__name__)


class DeliveryRegistry:
    """Process-level registry tracking all living CardDelivery instances.

    Responsibilities:
    - Track instances via explicit set + unregister (deterministic lifecycle)
    - Coordinate graceful shutdown across all instances
    - Provide drain_in_flight for process exit
    - Expose reset() for test isolation
    """

    def __init__(self) -> None:
        self._instances: set[CardDelivery] = set()
        self._shutdown_done: bool = False
        self._atexit_installed: bool = False
        self._lock: threading.Lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    def install_atexit(self) -> None:
        """Register atexit handler for graceful shutdown (idempotent).

        Call this once during application bootstrap (e.g. ws_client startup).
        Multiple calls are safe — only the first one registers the handler.
        """
        with self._lock:
            if self._atexit_installed:
                return
            self._atexit_installed = True

        def _atexit_shutdown():
            """Best-effort drain + shutdown on interpreter exit."""
            self.drain_in_flight(timeout=5)
            self.shutdown_all()

        atexit.register(_atexit_shutdown)

    @property
    def instances(self) -> frozenset[CardDelivery]:
        """Read-only snapshot of tracked instances (for monitoring/audit)."""
        with self._lock:
            return frozenset(self._instances)

    @property
    def shutdown_done(self) -> bool:
        """Whether shutdown_all() has already been called."""
        return self._shutdown_done

    def register(self, instance: CardDelivery) -> None:
        """Register a new CardDelivery instance.

        Automatically installs atexit handler on first registration to ensure
        graceful shutdown regardless of application entry point.
        """
        self.install_atexit()
        with self._lock:
            self._instances.add(instance)

    def unregister(self, instance: CardDelivery) -> None:
        """Unregister a CardDelivery instance (e.g. on shutdown)."""
        with self._lock:
            self._instances.discard(instance)

    def shutdown_all(self) -> None:
        """Shut down all living CardDelivery instances. Called during graceful shutdown."""
        with self._lock:
            if self._shutdown_done:
                return
            instances = list(self._instances)
            self._shutdown_done = True
        for instance in instances:
            try:
                instance._shutdown()
            except Exception:
                pass

    def drain_in_flight(self, timeout: float = 5.0) -> bool:
        """Wait for in-flight deliveries to finish across all living instances.

        Uses per-instance atomic fence+drain to avoid cross-instance deadlock:
        each instance is fenced and drained before moving to the next.

        Returns:
            True if all in-flight deliveries were drained successfully,
            False if timeout was reached.
        """
        deadline = time.monotonic() + timeout
        with self._lock:
            instances = list(self._instances)
        for instance in instances:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.debug("drain_in_flight: timeout reached")
                return False
            if not instance._drain(timeout=remaining):
                return False
        return True

    def reset(self) -> None:
        """Reset registry state for test isolation.

        Clears instance tracking and resets shutdown flag.
        Does NOT call shutdown on existing instances — caller is responsible
        for proper cleanup before calling reset.
        Does NOT unregister atexit handler (atexit module has no unregister API).
        """
        with self._lock:
            self._instances = set()
            self._shutdown_done = False


# Module-level singleton
delivery_registry = DeliveryRegistry()
