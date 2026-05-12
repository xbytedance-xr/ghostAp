"""Test delivery pool RuntimeError fallback — AC-18.

When the delivery thread pool is shut down and submit raises RuntimeError,
the session should gracefully fall back to synchronous delivery without crashing.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.session import CardSession
from src.card.session.config import SessionConfig
from src.card.state.models import CardMetadata


class MockDeliveryClient:
    """Minimal mock for CardAPIClient."""

    def __init__(self):
        self.creates = []

    def create_card(self, chat_id, card_json, *, reply_to=None, idempotency_key=None):
        self.creates.append(chat_id)
        return ("msg_1", "card_1")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass


class TestDeliveryPoolRuntimeErrorFallback:
    """When pool.submit raises RuntimeError, delivery falls back to sync."""

    def _make_session(self, *, sync_delivery=False):
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(engine_type="deep")
        config = SessionConfig(metadata=metadata, sync_delivery=sync_delivery)
        session = CardSession(
            chat_id="chat_pool_test",
            config=config,
            delivery=delivery,
            session_id="pool_fallback_test",
        )
        return session, client

    def test_pool_shutdown_falls_back_to_sync_delivery(self):
        """RuntimeError from pool.submit → synchronous delivery succeeds."""
        session, client = self._make_session(sync_delivery=False)

        # Patch get_delivery_pool to return a mock that raises RuntimeError on submit
        mock_pool = MagicMock()
        mock_pool.submit.side_effect = RuntimeError("cannot schedule new futures after shutdown")

        with patch("src.card.delivery.pool.get_delivery_pool", return_value=mock_pool):
            # This should NOT raise — it falls back to sync delivery
            session.dispatch(CardEvent(type=CardEventType.STARTED))

        # Verify delivery still happened (synchronous fallback)
        assert len(client.creates) == 1

    def test_pool_shutdown_delivers_terminal_event(self):
        """Terminal event delivery also works under pool shutdown fallback."""
        session, client = self._make_session(sync_delivery=False)

        mock_pool = MagicMock()
        mock_pool.submit.side_effect = RuntimeError("pool shut down")

        with patch("src.card.delivery.pool.get_delivery_pool", return_value=mock_pool):
            session.dispatch(CardEvent(type=CardEventType.STARTED))
            session.dispatch(CardEvent(type=CardEventType.COMPLETED, payload={}))

        # Both events delivered successfully via sync fallback
        assert len(client.creates) >= 1
