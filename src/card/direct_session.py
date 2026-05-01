"""DirectCardSession: bypass reduce/render for pre-built card JSON.

For interactive cards (buttons, selectors, inputs) that cannot be produced
by the standard CardState → render pipeline.  Wraps CardDelivery directly.

Usage:
    session = DirectCardSession(delivery, chat_id, reply_to=message_id)
    session.send(card_json)           # creates card
    session.send(updated_card_json)   # patches card (structure changed)
    mid = session.message_id          # get created message_id
    session.close()                   # cleanup bindings
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Optional

from src.card.delivery.engine import CardDelivery
from src.card.render.renderer import RenderedCard

logger = logging.getLogger(__name__)


def _compute_json_signature(card_json: dict) -> str:
    """Compute MD5 signature of the full card JSON for change detection."""
    raw = json.dumps(card_json, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class DirectCardSession:
    """Direct card session for pre-built card JSON.

    Bypasses reduce/render pipeline — delivers pre-built card JSON through
    CardDelivery for unified binding management and sequence tracking.

    Suitable for:
    - Interactive cards with buttons/selectors (WorktreeHandler)
    - Progress cards with manual update patterns (DiagnosticsHandler /diff)
    - Any card where the caller builds the full JSON externally
    """

    def __init__(
        self,
        delivery: CardDelivery,
        chat_id: str,
        *,
        session_id: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        self._delivery = delivery
        self._chat_id = chat_id
        self._session_id = session_id or str(uuid.uuid4())
        self._reply_to = reply_to
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def message_id(self) -> str | None:
        """Get the message_id of the first page (if created)."""
        binding = self._delivery.get_binding(self._session_id)
        if binding and binding.pages:
            page = binding.pages.get(0)
            if page:
                return page.message_id
        return None

    @property
    def closed(self) -> bool:
        return self._closed

    def send(self, card_json: dict | str) -> Optional[str]:
        """Send or update a card.

        First call creates the card; subsequent calls patch it.
        Returns message_id on success, None on failure.

        Args:
            card_json: Complete Feishu Schema 2.0 card JSON (dict or JSON string).
        """
        if self._closed:
            logger.debug("DirectCardSession %s: send after close, ignoring", self._session_id)
            return None

        if isinstance(card_json, str):
            card_json = json.loads(card_json)

        signature = _compute_json_signature(card_json)
        rendered = [
            RenderedCard(
                card_json=card_json,
                structure_signature=signature,
                active_element=None,
                page_index=0,
                total_pages=1,
            )
        ]

        self._delivery.deliver(
            session_id=self._session_id,
            chat_id=self._chat_id,
            rendered=rendered,
            reply_to=self._reply_to,
        )
        return self.message_id

    def close(self) -> None:
        """Finalize session: release bindings and sequences. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._delivery.close(self._session_id)
