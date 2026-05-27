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
import threading
from typing import Callable, Optional

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
        self._sessions_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._periodic_updates: dict[str, threading.Timer] = {}  # msg_id -> timer
        self._periodic_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

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
            with self._sessions_lock:
                self._sessions[msg_id] = session
        return msg_id

    def update_card(self, message_id: str, card: dict | str) -> bool:
        """Update an existing card. Returns True on success."""
        card_dict = json.loads(card) if isinstance(card, str) else card
        with self._sessions_lock:
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
            with self._sessions_lock:
                self._sessions[message_id] = fallback
        return result is not None

    def schedule_periodic_update(
        self,
        message_id: str,
        card_builder: Callable[[], dict],
        interval: float = 5.0,
        max_updates: int = 60,
    ) -> None:
        """Schedule periodic card updates for a message.

        Args:
            message_id: The card message to update periodically.
            card_builder: Zero-arg callable that returns the latest card dict.
            interval: Seconds between updates (minimum 2.0 to respect rate limits).
            max_updates: Maximum number of updates before auto-stopping.
        """
        interval = max(interval, 2.0)  # Enforce minimum interval
        self.cancel_periodic_update(message_id)  # Cancel existing if any

        counter = {"remaining": max_updates}

        def _tick() -> None:
            if counter["remaining"] <= 0:
                self.cancel_periodic_update(message_id)
                return
            counter["remaining"] -= 1
            try:
                card = card_builder()
                self.update_card(message_id, card)
            except Exception:
                logger.exception("Periodic card update failed for %s", message_id)
            # Reschedule
            with self._periodic_lock:
                if message_id in self._periodic_updates:
                    timer = threading.Timer(interval, _tick)
                    timer.daemon = True
                    timer.name = f"slock-periodic-{message_id[:12]}"
                    timer.start()
                    self._periodic_updates[message_id] = timer

        timer = threading.Timer(interval, _tick)
        timer.daemon = True
        timer.name = f"slock-periodic-{message_id[:12]}"
        timer.start()
        with self._periodic_lock:
            self._periodic_updates[message_id] = timer

    def cancel_periodic_update(self, message_id: str) -> None:
        """Cancel periodic updates for a message."""
        with self._periodic_lock:
            timer = self._periodic_updates.pop(message_id, None)
        if timer:
            timer.cancel()

    def cancel_all_periodic_updates(self) -> None:
        """Cancel all periodic update timers."""
        with self._periodic_lock:
            timers = list(self._periodic_updates.values())
            self._periodic_updates.clear()
        for timer in timers:
            timer.cancel()

    def close(self) -> None:
        """Release all sessions and cancel periodic updates. Safe to call multiple times."""
        self.cancel_all_periodic_updates()
        for session in self._sessions.values():
            if not session.closed:
                session.close()
        self._sessions.clear()
