"""SlockCardChannel — unified card delivery for Slock engine.

Routes Slock card send/update through StaticCardSession → CardDelivery,
automatically gaining:
- Payload truncation (30KB / 200 node guard)
- Sequence conflict resolution (Feishu code 300317 handling)
- Binding management and delivery tracking

Usage:
    channel = SlockCardChannel(handler.get_card_delivery(), chat_id)
    msg_id = channel.send_card(card_dict, reply_to=message_id)
    channel.update_card(msg_id, updated_card_dict)
    channel.close()
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from ..card.delivery.engine import CardDelivery
from ..card.session.static import StaticCardSession

logger = logging.getLogger(__name__)


class SlockCardChannel:
    """Slock card delivery channel backed by StaticCardSession.

    Each sent card gets its own StaticCardSession for independent lifecycle
    management. Updates route through the session that created the card.
    """

    def __init__(self, delivery: CardDelivery, chat_id: str) -> None:
        self._delivery = delivery
        self._chat_id = chat_id
        self._sessions: dict[str, StaticCardSession] = {}

    def send_card(
        self, card: dict | str, *, reply_to: str | None = None
    ) -> Optional[str]:
        """Send a new card via CardDelivery. Returns message_id or None."""
        card_dict = json.loads(card) if isinstance(card, str) else card
        session = StaticCardSession(
            self._delivery, self._chat_id, reply_to=reply_to
        )
        msg_id = session.send(card_dict)
        if msg_id:
            self._sessions[msg_id] = session
        return msg_id

    def update_card(self, message_id: str, card: dict | str) -> bool:
        """Update an existing card. Returns True on success."""
        card_dict = json.loads(card) if isinstance(card, str) else card
        session = self._sessions.get(message_id)
        if session and not session.closed:
            result = session.send(card_dict)
            return result is not None

        # Card not created through this channel — create a new session
        # and inject it for future updates
        fallback = StaticCardSession(self._delivery, self._chat_id)
        # Directly deliver as update (session handles create-vs-patch internally)
        result = fallback.send(card_dict)
        if result:
            self._sessions[message_id] = fallback
        return result is not None

    def close(self) -> None:
        """Release all sessions. Safe to call multiple times."""
        for session in self._sessions.values():
            if not session.closed:
                session.close()
        self._sessions.clear()
