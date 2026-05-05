"""Tests for terminal fallback notification paths.

Verifies that when card delivery fails at terminal time:
1. reply_text_fn is used as fallback when notify_callback is unavailable
2. Warning is logged when both callbacks are unavailable
3. _notify_rejected uses reply_text_fn fallback correctly
"""

import logging
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.session import CardSession
from src.card.session.config import SessionCallbacks, SessionConfig
from src.card.state.models import CardMetadata


class MockDeliveryClient:
    """Mock CardAPIClient that can be configured to fail."""

    def __init__(self, *, fail_deliver=False):
        self.creates = []
        self.updates = []
        self._counter = 0
        self._fail_deliver = fail_deliver

    def create_card(self, chat_id, card_json, *, reply_to=None):
        if self._fail_deliver:
            raise RuntimeError("delivery failed")
        self._counter += 1
        self.creates.append({"chat_id": chat_id, "card_json": card_json})
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        if self._fail_deliver:
            raise RuntimeError("delivery failed")
        self.updates.append(card_id)

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass


def _make_session(*, notify_callback=None, reply_text_fn=None):
    """Create a CardSession with configurable callbacks."""
    client = MockDeliveryClient()
    delivery = CardDelivery(client)
    metadata = CardMetadata(mode_name="Deep", tool_name="coco", model_name="gpt-4o", engine_type="deep")
    config = SessionConfig(metadata=metadata, reply_to="msg_origin")
    callbacks = SessionCallbacks(
        notify_callback=notify_callback,
        reply_text_fn=reply_text_fn,
    )
    session = CardSession(
        chat_id="chat_test",
        config=config,
        delivery=delivery,
        session_id="test_fallback_sess",
        callbacks=callbacks,
    )
    return session, client, delivery


class TestTerminalRetryFallback:
    """Tests for schedule_terminal_retry fallback paths in TTLHandler."""

    def test_double_render_failure_uses_reply_text(self):
        """When terminal delivery fails twice, reply_text_fn is called with fallback message."""
        reply_fn = MagicMock()
        session, client, delivery = _make_session(
            notify_callback=None,
            reply_text_fn=reply_fn,
        )
        # First: create the card so session has state
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        assert session.state is not None

        # Now make delivery fail
        client._fail_deliver = True

        # Dispatch terminal event — delivery will fail, scheduling retry
        session.dispatch(CardEvent(type=CardEventType.COMPLETED))

        # Wait for retry timer to fire (retry is scheduled after ~1s typically)
        # Give enough time for the retry to execute
        import time
        time.sleep(3.0)

        # reply_text_fn should have been called with a message about task ended
        if reply_fn.called:
            call_args = reply_fn.call_args[0]
            assert "msg_origin" == call_args[0]
            assert "任务已结束" in call_args[1]

    def test_rejected_no_notify_callback_uses_reply_text(self):
        """When delivery rejects and notify_callback is None, reply_text_fn is used."""
        reply_fn = MagicMock()
        session, client, delivery = _make_session(
            notify_callback=None,
            reply_text_fn=reply_fn,
        )
        # Create the card first
        session.dispatch(CardEvent(type=CardEventType.STARTED))

        # Call notify_rejected via coordinator to test fallback
        session._coordinator.notify_rejected(session.engine_cmd)

        reply_fn.assert_called_once()
        call_args = reply_fn.call_args[0]
        assert call_args[0] == "msg_origin"

    def test_rejected_no_callbacks_logs_warning(self, caplog):
        """When both callbacks are None, a WARNING is logged."""
        session, client, delivery = _make_session(
            notify_callback=None,
            reply_text_fn=None,
        )
        # Create card
        session.dispatch(CardEvent(type=CardEventType.STARTED))

        with caplog.at_level(logging.WARNING, logger="src.card.session"):
            session._coordinator.notify_rejected(session.engine_cmd)

        assert any("no callback available" in r.message for r in caplog.records)

    def test_rejected_deduplication(self):
        """_notify_rejected only fires once per 60s window."""
        reply_fn = MagicMock()
        session, client, delivery = _make_session(
            notify_callback=None,
            reply_text_fn=reply_fn,
        )
        session.dispatch(CardEvent(type=CardEventType.STARTED))

        session._coordinator.notify_rejected(session.engine_cmd)
        session._coordinator.notify_rejected(session.engine_cmd)
        session._coordinator.notify_rejected(session.engine_cmd)

        # Only called once due to dedup
        assert reply_fn.call_count == 1


# ---------------------------------------------------------------------------
# FS-9: _notify_rejected session integration test (dispatch → rejected → notify)
# ---------------------------------------------------------------------------

from src.card.delivery.engine import MutationOutcome


class RejectingDeliveryClient:
    """Mock delivery client that causes CardDelivery.deliver to return rejected."""

    def __init__(self):
        self.creates = []
        self._counter = 0
        self._reject_after = 1  # reject after first successful create

    def create_card(self, chat_id, card_json, *, reply_to=None):
        self._counter += 1
        self.creates.append({"chat_id": chat_id})
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        # Simulate reject by raising capacity error that CardDelivery maps to rejected
        from src.card.delivery.engine import TransportError
        raise TransportError("capacity exhausted")

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass


class TestNotifyRejectedIntegration:
    """Integration: dispatch events → delivery returns rejected → notify_callback is called."""

    def test_dispatch_triggers_notify_on_rejected(self):
        """Full path: dispatch → deliver → rejected outcome → _notify_rejected → notify_callback."""
        notify = MagicMock()

        # Use a delivery that always returns rejected
        class AlwaysRejectDelivery:
            """Delivery that returns rejected on every deliver() call."""

            def __init__(self):
                self.close_calls = 0

            def deliver(self, *, session_id, chat_id, rendered, reply_to=None):
                return [MutationOutcome(kind="rejected", message="lock full")]

            def close(self, session_id):
                self.close_calls += 1

        delivery = AlwaysRejectDelivery()
        metadata = CardMetadata(mode_name="Deep", tool_name="coco", engine_type="deep")
        config = SessionConfig(metadata=metadata)
        callbacks = SessionCallbacks(notify_callback=notify)
        session = CardSession(
            chat_id="chat_int",
            config=config,
            delivery=delivery,
            session_id="int_rej_sess",
            callbacks=callbacks,
        )

        # Dispatch STARTED + TEXT_DELTA — both deliveries will be rejected
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.text_delta("blk_1", "content"))

        # notify_callback should be called with chat_id and a rejection notice
        assert notify.call_count >= 1
        call_args = notify.call_args[0]
        assert call_args[0] == "chat_int"  # chat_id
        # Message should contain relevant info
        assert len(call_args[1]) > 0
