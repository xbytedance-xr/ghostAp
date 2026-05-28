"""Tests for StaticCardSession: send/close lifecycle, edge cases, delivery failures."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.card.session.static import StaticCardSession


class FakeDelivery:
    """Minimal mock for CardDelivery."""

    def __init__(self, *, fail: bool = False):
        self._fail = fail
        self.deliver_calls: list[dict] = []
        self.close_calls: list[str] = []

    def deliver(self, session_id, chat_id, rendered, *, reply_to=None, reply_in_thread=None):
        if self._fail:
            raise RuntimeError("delivery failed")
        self.deliver_calls.append({"session_id": session_id, "chat_id": chat_id, "rendered": rendered})
        return []

    def close(self, session_id):
        self.close_calls.append(session_id)

    def get_binding(self, session_id):
        return None


class TestStaticSessionLifecycle:
    """Test send/close lifecycle."""

    def test_send_creates_card(self):
        """First send() should call deliver with rendered card."""
        delivery = FakeDelivery()
        session = StaticCardSession(delivery=delivery, chat_id="chat1")

        session.send({"header": {"title": "Test"}})
        assert len(delivery.deliver_calls) == 1
        assert delivery.deliver_calls[0]["chat_id"] == "chat1"

    def test_send_returns_none_after_close(self):
        """send() after close() should return None without calling deliver."""
        delivery = FakeDelivery()
        session = StaticCardSession(delivery=delivery, chat_id="chat1")
        session.close()

        result = session.send({"body": "test"})
        assert result is None
        assert len(delivery.deliver_calls) == 0

    def test_close_is_idempotent(self):
        """Multiple close() calls should only call delivery.close() once."""
        delivery = FakeDelivery()
        session = StaticCardSession(delivery=delivery, chat_id="chat1")

        session.close()
        session.close()
        session.close()

        assert len(delivery.close_calls) == 1
        assert session.closed is True

    def test_session_id_generated(self):
        """session_id should be auto-generated when not provided."""
        delivery = FakeDelivery()
        session = StaticCardSession(delivery=delivery, chat_id="chat1")
        assert session.session_id is not None
        assert len(session.session_id) > 0

    def test_session_id_custom(self):
        """Custom session_id should be used when provided."""
        delivery = FakeDelivery()
        session = StaticCardSession(delivery=delivery, chat_id="chat1", session_id="my-session")
        assert session.session_id == "my-session"


class TestStaticSessionJsonParsing:
    """Test JSON string input parsing."""

    def test_json_string_input(self):
        """send() should accept JSON string and parse it."""
        delivery = FakeDelivery()
        session = StaticCardSession(delivery=delivery, chat_id="chat1")

        card_dict = {"header": {"title": "Test"}, "body": []}
        session.send(json.dumps(card_dict))

        assert len(delivery.deliver_calls) == 1
        rendered = delivery.deliver_calls[0]["rendered"][0]
        assert rendered._card_json == card_dict

    def test_invalid_json_string_raises(self):
        """send() with invalid JSON string should raise."""
        delivery = FakeDelivery()
        session = StaticCardSession(delivery=delivery, chat_id="chat1")

        with pytest.raises(json.JSONDecodeError):
            session.send("not valid json {{{")


class TestStaticSessionDeliveryFailure:
    """Test delivery failure paths."""

    def test_delivery_exception_returns_none(self):
        """send() should return None when delivery raises, not propagate."""
        delivery = FakeDelivery(fail=True)
        session = StaticCardSession(delivery=delivery, chat_id="chat1")

        result = session.send({"test": True})
        assert result is None
        assert not session.closed  # Session stays open

    def test_close_delivery_exception_swallowed(self):
        """close() should not propagate delivery.close() exceptions."""
        delivery = FakeDelivery()
        delivery.close = MagicMock(side_effect=RuntimeError("close failed"))
        session = StaticCardSession(delivery=delivery, chat_id="chat1")

        # Should not raise
        session.close()
        assert session.closed is True


class TestStaticSessionBinding:
    """Test message_id property with valid binding."""

    def test_send_with_binding_returns_message_id(self):
        """When get_binding returns a valid binding, message_id property returns the ID."""

        class FakePageBinding:
            def __init__(self):
                self.pages = {0: type("Page", (), {"message_id": "msg_abc123"})()}

        class BindingDelivery(FakeDelivery):
            def get_binding(self, session_id):
                return FakePageBinding()

        delivery = BindingDelivery()
        session = StaticCardSession(delivery=delivery, chat_id="chat1")

        result = session.send({"header": {"title": "Test"}})
        assert result == "msg_abc123"
        assert session.message_id == "msg_abc123"
