"""PageMutator: encapsulates card page mutation operations (create/update/stream/finalize)."""

from __future__ import annotations

import logging

from src.card.delivery.binding import BindingStore, PageBinding
from src.card.delivery.sequence import SequenceManager
from src.card.delivery.types import MutationOutcome, SequenceConflictError, TransportError
from src.card.types import RenderedCard

logger = logging.getLogger(__name__)


class PageMutator:
    """Handles page-level card mutations against the Feishu API.

    Stateless with respect to session lifecycle — all state lives in
    the injected collaborators (client, bindings, sequences).
    CardDelivery retains ownership of these collaborators' lifecycles.
    """

    def __init__(self, client, bindings: BindingStore, sequences: SequenceManager) -> None:
        self._client = client
        self._bindings = bindings
        self._sequences = sequences

    def create_page(
        self,
        session_id: str,
        chat_id: str,
        card: RenderedCard,
        *,
        reply_to: str | None = None,
    ) -> "MutationOutcome":
        """Create a new card page via API."""

        try:
            card_payload = card.to_feishu_json()
            is_streaming = card_payload.get("config", {}).get("streaming_mode", False)

            if is_streaming:
                try:
                    card_id = self._client.create_streaming_card(card_payload)
                    message_id = self._client.send_card_reference(
                        chat_id, card_id, reply_to=reply_to
                    )
                except Exception:
                    logger.debug("Streaming card creation failed, falling back to IM API")
                    message_id, card_id = self._client.create_card(
                        chat_id, card_payload, reply_to=reply_to
                    )
            else:
                message_id, card_id = self._client.create_card(
                    chat_id, card_payload, reply_to=reply_to
                )

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
            logger.warning("Card create failed: %s", str(e))
            return MutationOutcome(kind="reconcile", message=str(e))

    def update_page(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> "MutationOutcome":
        """Update card structure via PATCH API."""

        try:
            seq = self._sequences.next_sequence(page.card_id)
            self._client.update_card(page.card_id, card.to_feishu_json(), sequence=seq)
            self._bindings.update_signature(session_id, page.page_index, card.structure_signature)
            if card.active_element:
                self._bindings.update_text(session_id, page.page_index, card.active_element.text)
            return MutationOutcome(kind="applied", message=f"updated:{page.card_id}")
        except SequenceConflictError as e:
            self._sequences.raise_floor(page.card_id, e.next_floor)
            logger.debug("Sequence conflict on %s, raised floor to %d", page.card_id, e.next_floor)
            return MutationOutcome(kind="reconcile", message="sequence_conflict")
        except TransportError as e:
            if e.is_permanent:
                logger.warning(
                    "Permanent transport error on %s (code=%d), removing binding to force recreation",
                    page.card_id, e.code,
                )
                self._bindings.remove_page(session_id, page.page_index)
                self._sequences.reset(page.card_id)
                return MutationOutcome(kind="reconcile", message=f"permanent:{e.code}")
            logger.warning("Transport error updating %s: %s", page.card_id, str(e))
            return MutationOutcome(kind="reconcile", message=str(e))
        except Exception as e:
            logger.warning("Card update failed: %s", str(e))
            return MutationOutcome(kind="reconcile", message=str(e))

    def stream_element(
        self, session_id: str, page: PageBinding, card: RenderedCard
    ) -> "MutationOutcome":
        """Push text update via CardKit element_content API."""

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
            self._bindings.update_text(session_id, page.page_index, card.active_element.text)
            return MutationOutcome(kind="applied", message=f"element:{page.card_id}")
        except SequenceConflictError as e:
            self._sequences.raise_floor(page.card_id, e.next_floor)
            logger.debug("Element sequence conflict on %s, falling back to full update", page.card_id)
            return self.update_page(session_id, page, card)
        except TransportError as e:
            if e.is_permanent:
                logger.warning(
                    "Permanent transport error on %s (code=%d), removing binding to force recreation",
                    page.card_id, e.code,
                )
                self._bindings.remove_page(session_id, page.page_index)
                self._sequences.reset(page.card_id)
                return MutationOutcome(kind="reconcile", message=f"permanent:{e.code}")
            logger.debug("Element update failed (%s), falling back to full update", str(e))
            return self.update_page(session_id, page, card)
        except Exception as e:
            logger.debug("Element update failed (%s), falling back to full update", str(e))
            return self.update_page(session_id, page, card)

    def finalize_page(self, session_id: str, page: PageBinding) -> None:
        """Finalize a stale page."""
        if page.card_id:
            self._sequences.reset(page.card_id)
        self._bindings.remove_page(session_id, page.page_index)
