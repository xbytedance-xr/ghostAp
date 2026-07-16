"""CardDelivery: unified delivery engine for Feishu card operations."""

from __future__ import annotations

import dataclasses
import logging
import re
import threading

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.lock_pool import SessionLockPool
from src.card.delivery.page_mutator import PageMutator, sanitize_card_text_for_audit
from src.card.delivery.registry import DeliveryRegistry, delivery_registry
from src.card.delivery.sequence import SequenceManager
from src.card.delivery.ttl_set import TTLSet
from src.card.delivery.types import MutationOutcome, SequenceConflictError, TransportError
from src.card.protocols import CardAPIClient
from src.card.types import RenderedCard

logger = logging.getLogger(__name__)

_RECREATE_OUTCOME_PREFIX = "recreate:"


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
        reply_in_thread: bool | None = None,
        is_terminal: bool = False,
    ) -> list[MutationOutcome]:
        """Deliver rendered cards to Feishu.

        Decision logic:
        - No binding → card.create
        - Signature changed → card.update
        - Only text changed → element_content
        - No change → skip

        Pagination follows an append-only message contract: pages are created in
        ascending order, a page is flushed once when it becomes history, and only
        the highest visible page continues to receive live or terminal updates.
        This preserves Feishu message chronology even when rendered page counts
        grow or shrink.

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
                return self._deliver_unlocked(session_id, chat_id, rendered, reply_to=reply_to, reply_in_thread=reply_in_thread, is_terminal=is_terminal)
        finally:
            self._lock_pool.exit_delivery()

    def _deliver_unlocked(
        self,
        session_id: str,
        chat_id: str,
        rendered: list[RenderedCard],
        *,
        reply_to: str | None = None,
        reply_in_thread: bool | None = None,
        is_terminal: bool = False,
    ) -> list[MutationOutcome]:
        """Internal deliver implementation (caller holds per-session lock)."""
        # Second check under session lock: eliminates TOCTOU window between
        # the fast-path _closed_lock check and per-session lock acquisition.
        if session_id in self._closed_sessions:
            return []
        binding = self._bindings.get(session_id)
        outcomes: list[MutationOutcome] = []
        ordered = sorted(rendered, key=lambda card: card.page_index)
        if not ordered:
            return outcomes
        latest_rendered_idx = ordered[-1].page_index

        if binding is None:
            binding = self._bindings.create(session_id, chat_id)
            for card in ordered:
                outcome = self._create_page(session_id, chat_id, card, reply_to=reply_to, reply_in_thread=reply_in_thread)
                outcomes.append(outcome)
                if outcome.kind != "applied":
                    break
                if card.page_index < latest_rendered_idx:
                    self._bindings.mark_frozen(session_id, card.page_index)
            return outcomes

        if not binding.pages:
            for card in ordered:
                outcome = self._create_page(session_id, chat_id, card, reply_to=reply_to, reply_in_thread=reply_in_thread)
                outcomes.append(outcome)
                if outcome.kind != "applied":
                    break
                if card.page_index < latest_rendered_idx:
                    self._bindings.mark_frozen(session_id, card.page_index)
            return outcomes

        message_high_watermark = max(binding.pages)

        # Renderer compaction can reduce its page count. Never delete or reuse an
        # older Feishu message: move the renderer's newest state onto the existing
        # highest message and preserve all lower bindings as immutable history.
        if latest_rendered_idx < message_high_watermark:
            latest_card = self._remap_latest_card(
                ordered[-1],
                page_index=message_high_watermark,
                total_pages=message_high_watermark + 1,
            )
            latest_page = binding.pages[message_high_watermark]
            self._bindings.mark_frozen(
                session_id,
                message_high_watermark,
                frozen=False,
            )
            outcomes.append(self._mutate_page(session_id, latest_page, latest_card))
            return outcomes

        for card in ordered:
            page_idx = card.page_index
            existing_page = binding.pages.get(page_idx)

            if (
                page_idx < message_high_watermark
                or (existing_page is not None and existing_page.is_frozen)
            ):
                outcomes.append(MutationOutcome(kind="skipped", message="history_page_frozen"))
                continue

            if existing_page is None:
                outcome = self._create_page(
                    session_id,
                    chat_id,
                    card,
                    reply_to=reply_to,
                    reply_in_thread=reply_in_thread,
                )
            else:
                outcome = self._mutate_page(session_id, existing_page, card)
            outcomes.append(outcome)

            if self._is_recreate_outcome(outcome):
                break
            if outcome.kind == "reconcile":
                break

            # When pagination grows, the previous live page is first flushed with
            # its boundary content above, then becomes immutable. Intermediate new
            # pages are likewise frozen; only the newest page remains live.
            if page_idx < latest_rendered_idx:
                self._bindings.mark_frozen(session_id, page_idx)

        return outcomes

    @staticmethod
    def _remap_latest_card(
        card: RenderedCard,
        *,
        page_index: int,
        total_pages: int,
    ) -> RenderedCard:
        """Move compacted output to the newest message and keep its page label truthful."""
        payload = card.to_feishu_json()
        header = payload.get("header")
        if isinstance(header, dict):
            for field in ("title", "subtitle"):
                value = header.get(field)
                if not isinstance(value, dict) or not isinstance(value.get("content"), str):
                    continue
                value["content"] = re.sub(
                    r"(?:\s*·\s*)?页\s+\d+/\d+",
                    "",
                    value["content"],
                ).strip()
            title = header.get("title")
            if isinstance(title, dict) and isinstance(title.get("content"), str):
                title["content"] = (
                    f"{title['content']} · 页 {page_index + 1}/{total_pages}"
                )

        return dataclasses.replace(
            card,
            _card_json=payload,
            structure_signature=(
                f"{card.structure_signature}:message-page:{page_index + 1}/{total_pages}"
            ),
            page_index=page_index,
            total_pages=total_pages,
        )

    def _mutate_page(
        self,
        session_id: str,
        page: PageBinding,
        card: RenderedCard,
    ) -> MutationOutcome:
        """Apply the smallest safe mutation to one existing page."""
        if page.signature != card.structure_signature:
            return self._update_page(session_id, page, card)
        if (
            card.active_element is not None
            and sanitize_card_text_for_audit(card.active_element.text) != page.last_text
        ):
            return self._stream_element(session_id, page, card)
        return MutationOutcome(kind="skipped")

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
        reply_in_thread: bool | None = None,
    ) -> MutationOutcome:
        """Create a new card page via API."""
        return self._mutator.create_page(session_id, chat_id, card, reply_to=reply_to, reply_in_thread=reply_in_thread)

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

    @staticmethod
    def _is_recreate_outcome(outcome: MutationOutcome) -> bool:
        return outcome.kind == "reconcile" and outcome.message.startswith(_RECREATE_OUTCOME_PREFIX)
