"""Tests for CardDelivery __enter__/__exit__ context manager protocol."""

from unittest.mock import patch

import pytest

from src.card.delivery.engine import CardDelivery


class MockClient:
    """Minimal CardAPIClient mock."""

    def create_card(self, chat_id, card_json, *, reply_to=None, reply_in_thread=None, idempotency_key=None):
        return ("msg_1", "card_1")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass

    def create_streaming_card(self, card_json):
        return "card_1"

    def send_card_reference(self, chat_id, card_id, *, reply_to=None, reply_in_thread=None, idempotency_key=None):
        return "msg_1"


class TestCardDeliveryContextManager:
    def test_normal_exit_calls_shutdown(self):
        """Normal exit from context manager should call _shutdown()."""
        client = MockClient()
        delivery = CardDelivery(client, max_session_locks=10, eviction_interval=999.0)

        with patch.object(delivery, "_shutdown") as instance_shutdown:
            with delivery:
                pass  # Normal exit
            instance_shutdown.assert_called_once()

    def test_exception_exit_calls_shutdown(self):
        """Exception exit from context manager should still call _shutdown()."""
        client = MockClient()
        delivery = CardDelivery(client, max_session_locks=10, eviction_interval=999.0)

        with patch.object(delivery, "_shutdown") as instance_shutdown:
            with pytest.raises(RuntimeError):
                with delivery:
                    raise RuntimeError("test error")
            instance_shutdown.assert_called_once()

    def test_enter_returns_self(self):
        """__enter__ should return the delivery instance itself."""
        client = MockClient()
        delivery = CardDelivery(client, max_session_locks=10, eviction_interval=999.0)
        try:
            with delivery as d:
                assert d is delivery
        finally:
            delivery._shutdown()

    def test_nested_context_idempotent(self):
        """Multiple shutdown calls (nested contexts) should not raise."""
        client = MockClient()
        delivery = CardDelivery(client, max_session_locks=10, eviction_interval=999.0)
        with delivery:
            pass
        # Second shutdown should be a no-op (thread already stopped)
        delivery._shutdown()  # Should not raise
