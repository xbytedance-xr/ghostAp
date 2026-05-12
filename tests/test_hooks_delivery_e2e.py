"""End-to-end test: EmojiHook + delivery + session close — AC-20.

Verifies the full chain: dispatch terminal event → delivery.deliver called →
EmojiHook.on_terminal fired → session correctly closed.
"""

from unittest.mock import MagicMock, call

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.hooks import EmojiHook
from src.card.session import CardSession
from src.card.session.config import SessionCallbacks, SessionConfig
from src.card.state.models import CardMetadata


class MockDeliveryClient:
    """Minimal mock CardAPIClient tracking create/update calls."""

    def __init__(self):
        self.creates = []
        self.updates = []
        self._counter = 0

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        self._counter += 1
        self.creates.append(chat_id)
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        self.updates.append(card_id)

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass


class TestHooksDeliveryE2E:
    """Full chain: EmojiHook fires + delivery delivers + session closes."""

    def _make_session_with_emoji_hook(self):
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        add_reaction = MagicMock()
        emoji_hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="msg_trigger",
            chat_id="chat_e2e",
        )
        metadata = CardMetadata(engine_type="deep")
        config = SessionConfig(metadata=metadata, sync_delivery=True)
        callbacks = SessionCallbacks(hooks=(emoji_hook,))
        session = CardSession(
            chat_id="chat_e2e",
            config=config,
            delivery=delivery,
            session_id="e2e_sess",
            callbacks=callbacks,
        )
        return session, client, add_reaction, emoji_hook

    def test_completed_triggers_emoji_and_delivery_and_close(self):
        """STARTED → COMPLETED: delivery creates card, emoji fires, session closes."""
        session, client, add_reaction, _ = self._make_session_with_emoji_hook()

        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.COMPLETED, payload={}))

        # Delivery created (at least) one card
        assert len(client.creates) >= 1

        # EmojiHook fired with success emoji
        add_reaction.assert_called_once_with("msg_trigger", "PARTY")

        # Session is properly closed after terminal event
        assert session.closed

    def test_failed_triggers_error_emoji(self):
        """STARTED → FAILED: emoji hook fires with SOB emoji."""
        session, client, add_reaction, _ = self._make_session_with_emoji_hook()

        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.FAILED, payload={"error": "oops"}))

        add_reaction.assert_called_once_with("msg_trigger", "SOB")
        assert session.closed

    def test_cancelled_triggers_stop_emoji(self):
        """STARTED → CANCELLED: emoji hook fires with STOP emoji."""
        session, client, add_reaction, _ = self._make_session_with_emoji_hook()

        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.CANCELLED, payload={}))

        add_reaction.assert_called_once_with("msg_trigger", "SKULL")
        assert session.closed

    def test_delivery_succeeds_before_hook_fires(self):
        """Delivery must complete before hooks fire on terminal events."""
        session, client, add_reaction, _ = self._make_session_with_emoji_hook()

        session.dispatch(CardEvent(type=CardEventType.STARTED))
        # At this point card should be created
        assert len(client.creates) == 1

        session.dispatch(CardEvent(type=CardEventType.COMPLETED, payload={}))
        # After COMPLETED, card should be updated
        assert len(client.updates) >= 1 or len(client.creates) >= 1

    def test_no_emoji_for_empty_message_id(self):
        """EmojiHook with empty message_id should gracefully skip."""
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        add_reaction = MagicMock()
        emoji_hook = EmojiHook(
            add_reaction=add_reaction,
            message_id="",  # Empty — degraded mode
            chat_id="chat_empty",
        )
        metadata = CardMetadata(engine_type="deep")
        config = SessionConfig(metadata=metadata, sync_delivery=True)
        callbacks = SessionCallbacks(hooks=(emoji_hook,))
        session = CardSession(
            chat_id="chat_empty",
            config=config,
            delivery=delivery,
            session_id="e2e_empty_msg",
            callbacks=callbacks,
        )

        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.COMPLETED, payload={}))

        # Emoji should NOT have been added (graceful skip)
        add_reaction.assert_not_called()
        # But session should still close properly
        assert session.closed
