"""CardDelivery: unified delivery engine for Feishu card operations."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Literal, Protocol

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.sequence import SequenceManager
from src.card.render.renderer import RenderedCard

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols: abstract Feishu API client
# ---------------------------------------------------------------------------


class CardAPIClient(Protocol):
    """Protocol for Feishu card API operations."""

    def create_card(
        self, chat_id: str, card_json: dict, *, reply_to: str | None = None
    ) -> tuple[str, str]:
        """Create a card message. Returns (message_id, card_id)."""
        ...

    def update_card(self, card_id: str, card_json: dict, *, sequence: int = 0) -> None:
        """Update (PATCH) a card by card_id."""
        ...

    def update_element(self, card_id: str, element_id: str, content: str, *, sequence: int = 0) -> None:
        """Update a single element's content (element_content API)."""
        ...


# ---------------------------------------------------------------------------
# Outcome types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MutationOutcome:
    """Result of a card mutation attempt."""

    kind: Literal["applied", "reconcile", "skipped"]
    message: str = ""


class SequenceConflictError(Exception):
    """Raised when Feishu returns 300317 (sequence conflict)."""

    def __init__(self, next_floor: int = 0):
        self.next_floor = next_floor
        super().__init__(f"Sequence conflict, floor={next_floor}")


class TransportError(Exception):
    """Raised on 5xx / timeout from Feishu API."""
    pass


# ---------------------------------------------------------------------------
# CardDelivery engine
# ---------------------------------------------------------------------------


class CardDelivery:
    """Unified delivery engine.

    Merges the responsibilities of card creation + element update:
    - Decides operation type (create / update / element_content)
    - Manages sequence numbers for optimistic concurrency
    - Handles reconciliation on conflict
    """

    def __init__(self, client: CardAPIClient) -> None:
        self._client = client
        self._bindings = BindingStore()
        self._sequences = SequenceManager()
        self._lock = threading.Lock()

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
        """
        binding = self._bindings.get(session_id)
        outcomes: list[MutationOutcome] = []

        if binding is None:
            # First delivery: create all pages
            binding = self._bindings.create(session_id, chat_id)
            for card in rendered:
                outcome = self._create_page(
                    session_id, chat_id, card, reply_to=reply_to
                )
                outcomes.append(outcome)
        else:
            # Subsequent delivery: compare with existing
            for card in rendered:
                page_idx = card.page_index
                existing_page = binding.pages.get(page_idx)

                if existing_page is None:
                    # New page appeared (pagination grew)
                    outcome = self._create_page(
                        session_id, chat_id, card, reply_to=reply_to
                    )
                elif existing_page.signature != card.structure_signature:
                    # Structure changed → full update
                    outcome = self._update_page(session_id, existing_page, card)
                elif (
                    card.active_element is not None
                    and card.active_element.text != existing_page.last_text
                ):
                    # Only text changed → element_content streaming
                    outcome = self._stream_element(session_id, existing_page, card)
                else:
                    # No change
                    outcome = MutationOutcome(kind="skipped")
                outcomes.append(outcome)

            # Mark stale pages (if page count decreased)
            for stale_idx in range(len(rendered), len(binding.pages)):
                self._finalize_page(session_id, binding.pages[stale_idx])

        return outcomes

    def close(self, session_id: str) -> None:
        """Finalize a session: remove bindings and sequences."""
        binding = self._bindings.remove(session_id)
        if binding is not None:
            for page in binding.pages.values():
                if page.card_id:
                    self._sequences.reset(page.card_id)

    def get_binding(self, session_id: str):
        """Get the current binding for inspection/testing."""
        return self._bindings.get(session_id)

    # ----- Internal operations -----

    def _create_page(
        self,
        session_id: str,
        chat_id: str,
        card: RenderedCard,
        *,
        reply_to: str | None = None,
    ) -> MutationOutcome:
        """Create a new card page via API."""
        try:
            message_id, card_id = self._client.create_card(
                chat_id, card.card_json, reply_to=reply_to
            )
            # Record binding
            last_text = card.active_element.text if card.active_element else ""
            self._bindings.set_page(
                session_id=session_id,
                page_index=card.page_index,
                message_id=message_id,
                card_id=card_id,
                signature=card.structure_signature,
                last_text=last_text,
            )
            return MutationOutcome(kind="applied", message=f"created:{message_id}")
        except Exception as e:
            logger.warning("Card create failed: %s", e)
            return MutationOutcome(kind="reconcile", message=str(e))

    def _update_page(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> MutationOutcome:
        """Update card structure via PATCH API."""
        try:
            seq = self._sequences.next_sequence(page.card_id)
            self._client.update_card(page.card_id, card.card_json, sequence=seq)
            # Update binding
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            if card.active_element:
                self._bindings.update_text(session_id, page.page_index, card.active_element.text)
            return MutationOutcome(kind="applied", message=f"updated:{page.card_id}")
        except SequenceConflictError as e:
            self._sequences.raise_floor(page.card_id, e.next_floor)
            logger.debug("Sequence conflict on %s, raised floor to %d", page.card_id, e.next_floor)
            return MutationOutcome(kind="reconcile", message="sequence_conflict")
        except TransportError as e:
            logger.warning("Transport error updating %s: %s", page.card_id, e)
            return MutationOutcome(kind="reconcile", message=str(e))
        except Exception as e:
            logger.warning("Card update failed: %s", e)
            return MutationOutcome(kind="reconcile", message=str(e))

    def _stream_element(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> MutationOutcome:
        """Push text update via CardKit element_content API (if available).

        Falls back to full PATCH if element update fails.
        """
        if card.active_element is None:
            return MutationOutcome(kind="skipped")

        try:
            seq = self._sequences.next_sequence(page.card_id)
            self._client.update_element(
                page.card_id,
                card.active_element.element_id,
                card.active_element.text,
                sequence=seq,
            )
            # Update text binding only (signature unchanged)
            self._bindings.update_text(session_id, page.page_index, card.active_element.text)
            return MutationOutcome(kind="applied", message=f"element:{page.card_id}")
        except SequenceConflictError as e:
            self._sequences.raise_floor(page.card_id, e.next_floor)
            logger.debug("Element sequence conflict on %s, falling back to full update", page.card_id)
            return self._update_page(session_id, page, card)
        except Exception as e:
            logger.debug("Element update failed (%s), falling back to full update", e)
            return self._update_page(session_id, page, card)

    def _finalize_page(self, session_id: str, page: PageBinding) -> None:
        """Finalize a stale page (optional: send final update disabling streaming)."""
        # Currently just clean up sequence
        if page.card_id:
            self._sequences.reset(page.card_id)
