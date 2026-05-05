"""CardDelivery: unified delivery engine for Feishu card operations."""

from __future__ import annotations

import logging
import threading
import time

from src.card.delivery.types import MutationOutcome, SequenceConflictError, TransportError
from src.card.protocols import CardAPIClient

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.lock_pool import SessionLockPool
from src.card.delivery.page_mutator import PageMutator
from src.card.delivery.registry import DeliveryRegistry, delivery_registry
from src.card.delivery.sequence import SequenceManager
from src.card.delivery.ttl_set import TTLSet
from src.card.types import RenderedCard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outcome types (re-exported from src.card.delivery.types for backwards compat)
# ---------------------------------------------------------------------------

__all__ = ["CardDelivery", "MutationOutcome", "SequenceConflictError", "TransportError", "CardAPIClient"]


# ---------------------------------------------------------------------------
# CardDelivery engine
# ---------------------------------------------------------------------------


class CardDelivery:
    """Unified delivery engine.

    Merges the responsibilities of card creation + element update:
    - Decides operation type (create / update / element_content)
    - Manages sequence numbers for optimistic concurrency
    - Handles reconciliation on conflict

    Public API: deliver() and close() only.
    Lifecycle management (shutdown/drain) is accessed via DeliveryRegistry.

    Thread-safety: deliver() and close() are idempotent and concurrency-safe.
    After close(session_id) completes, subsequent deliver() calls for that
    session_id are no-ops.
    """

    def __init__(
        self,
        client: CardAPIClient,
        *,
        max_session_locks: int = 10_000,
        session_lock_ttl: float = 600.0,
        eviction_interval: float = 30.0,
        registry: "DeliveryRegistry | None" = None,
    ) -> None:
        self._client = client
        self._bindings = BindingStore()
        self._sequences = SequenceManager()
        self._closed_sessions = TTLSet(ttl=3600.0, max_size=50_000)
        self._mutator = PageMutator(client, self._bindings, self._sequences)

        # Delegate lock pool management
        self._lock_pool = SessionLockPool(
            max_locks=max_session_locks,
            lock_ttl=session_lock_ttl,
            eviction_interval=eviction_interval,
            has_active_binding=self._bindings.has,
            purge_callback=lambda: self._closed_sessions.purge(),
        )

        # Independent lock for _closed_sessions (decoupled from lock pool internals)
        self._closed_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

        self._max_session_locks = max_session_locks
        self._session_lock_ttl = session_lock_ttl
        self._eviction_interval = eviction_interval

        self._registry = registry or delivery_registry
        self._registry.register(self)

    def _shutdown(self) -> None:
        """Stop background eviction thread. Called via DeliveryRegistry on graceful shutdown."""
        self._lock_pool.shutdown()
        self._registry.unregister(self)

    @property
    def _accepting_work(self) -> bool:
        """Whether this delivery engine is accepting new work (read-only proxy to lock pool)."""
        return self._lock_pool.accepting_work

    def _drain(self, timeout: float = 5.0) -> bool:
        """Wait for all in-flight deliveries on this instance to finish.

        Returns True if all drained within *timeout*, False otherwise.
        """
        self._lock_pool.fence()
        return self._lock_pool.drain(timeout=timeout)

    def release_session_lock(self, session_id: str) -> None:
        """Release the delivery lock for *session_id* (idempotent).

        Public API — called by CardSession finalizer and close() to release
        the per-session delivery lock on session termination.
        """
        self._lock_pool.release(session_id)

    def __del__(self) -> None:
        """Defense-in-depth: stop eviction thread if instance leaks without explicit shutdown."""
        try:
            self._lock_pool.shutdown()
        except Exception:
            pass

    def __enter__(self) -> CardDelivery:
        return self

    def __exit__(self, *exc_info) -> None:
        self._shutdown()

    def deliver(
        self,
        session_id: str,
        chat_id: str,
        rendered: list[RenderedCard],
        *,
        reply_to: str | None = None,
    ) -> list[MutationOutcome]:
        """Deliver rendered cards to Feishu.

        Decision logic:
        - No binding → card.create
        - Signature changed → card.update
        - Only text changed → element_content
        - No change → skip

        Idempotent: returns empty list if session is already closed.

        Timeout: This method does NOT enforce its own timeout. The underlying
        Feishu API client is expected to raise TimeoutError (or similar) when
        a request exceeds its configured timeout (default 30s). Such exceptions
        are caught by PageMutator and surfaced as ``MutationOutcome(kind="reconcile")``.
        Callers should NOT wrap deliver() in an external timeout — doing so risks
        leaving per-session locks unreleased.
        """
        if not self._lock_pool.accepting_work:
            return [MutationOutcome(kind="rejected", message="delivery shutting down")]
        with self._closed_lock:
            if session_id in self._closed_sessions:
                return []
        # Acquire per-session lock (creates if needed, may evict LRU)
        try:
            session_lock = self._lock_pool.acquire(session_id)
        except RuntimeError:
            logger.error(
                "Session lock capacity exhausted, rejecting new session %s",
                session_id,
            )
            return [MutationOutcome(kind="rejected", message="session lock capacity exhausted")]

        self._lock_pool.enter_delivery()
        try:
            with session_lock:
                return self._deliver_unlocked(session_id, chat_id, rendered, reply_to=reply_to)
        finally:
            self._lock_pool.exit_delivery()

    def _deliver_unlocked(
        self,
        session_id: str,
        chat_id: str,
        rendered: list[RenderedCard],
        *,
        reply_to: str | None = None,
    ) -> list[MutationOutcome]:
        """Internal deliver implementation (caller holds per-session lock)."""
        # Second check under session lock: eliminates TOCTOU window between
        # the fast-path _closed_lock check and per-session lock acquisition.
        if session_id in self._closed_sessions:
            return []
        binding = self._bindings.get(session_id)
        outcomes: list[MutationOutcome] = []

        if binding is None:
            binding = self._bindings.create(session_id, chat_id)
            for card in rendered:
                outcome = self._create_page(session_id, chat_id, card, reply_to=reply_to)
                outcomes.append(outcome)
        else:
            for card in rendered:
                page_idx = card.page_index
                existing_page = binding.pages.get(page_idx)

                if existing_page is None:
                    outcome = self._create_page(session_id, chat_id, card, reply_to=reply_to)
                elif existing_page.signature != card.structure_signature:
                    outcome = self._update_page(session_id, existing_page, card)
                elif (
                    card.active_element is not None
                    and card.active_element.text != existing_page.last_text
                ):
                    outcome = self._stream_element(session_id, existing_page, card)
                else:
                    outcome = MutationOutcome(kind="skipped")
                outcomes.append(outcome)

            # Finalize stale pages (pages in binding but not in current rendered set)
            rendered_indices = {card.page_index for card in rendered}
            for stale_idx in list(binding.pages.keys()):
                if stale_idx not in rendered_indices:
                    self._finalize_page(session_id, binding.pages[stale_idx])

        return outcomes

    def close(self, session_id: str) -> None:
        """Finalize a session: remove bindings, sequences, and session lock.

        Idempotent: safe to call multiple times for the same session_id.
        """
        with self._closed_lock:
            if session_id in self._closed_sessions:
                return
            self._closed_sessions.add(session_id)

        # Acquire existing session lock (or create temporary) for cleanup serialization
        session_lock = self._lock_pool.get_existing(session_id)
        if session_lock is None:
            session_lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock

        with session_lock:
            binding = self._bindings.remove(session_id)
            if binding is not None:
                for page in binding.pages.values():
                    if page.card_id:
                        self._sequences.reset(page.card_id)

        # Remove lock entry
        self._lock_pool.release(session_id)

    def get_binding(self, session_id: str):
        """Get the current binding for inspection/testing."""
        return self._bindings.get(session_id)

    # ----- Internal operations (delegated to PageMutator) -----

    def _create_page(
        self,
        session_id: str,
        chat_id: str,
        card: RenderedCard,
        *,
        reply_to: str | None = None,
    ) -> MutationOutcome:
        """Create a new card page via API."""
        return self._mutator.create_page(session_id, chat_id, card, reply_to=reply_to)

    def _update_page(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> MutationOutcome:
        """Update card structure via PATCH API."""
        return self._mutator.update_page(session_id, page, card)

    def _stream_element(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> MutationOutcome:
        """Push text update via CardKit element_content API."""
        return self._mutator.stream_element(session_id, page, card)

    def _finalize_page(self, session_id: str, page: PageBinding) -> None:
        """Finalize a stale page."""
        self._mutator.finalize_page(session_id, page)
