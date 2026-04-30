"""Tests for CardSession orchestration layer."""

import threading

import pytest

from src.card.delivery.engine import CardDelivery, MutationOutcome
from src.card.events import CardEvent, CardEventType
from src.card.render.renderer import RenderedCard
from src.card.session import CardSession, CardSessionFactory
from src.card.state.models import CardMetadata, CardState


class MockDeliveryClient:
    """Mock CardAPIClient for testing."""

    def __init__(self):
        self.creates = []
        self.updates = []
        self.elements = []
        self._counter = 0

    def create_card(self, chat_id, card_json, *, reply_to=None):
        self._counter += 1
        self.creates.append({"chat_id": chat_id, "card_json": card_json})
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        self.updates.append(card_id)

    def update_element(self, card_id, element_id, content, *, sequence=0):
        self.elements.append(element_id)


class TestCardSessionDispatch:
    """Core dispatch behavior."""

    def _make_session(self):
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(mode_name="Coco", tool_name="coco", model_name="gpt-4o")
        session = CardSession(
            chat_id="chat_1",
            metadata=metadata,
            delivery=delivery,
            session_id="test_sess",
        )
        return session, client, delivery

    def test_dispatch_started_creates_card(self):
        session, client, _ = self._make_session()
        event = CardEvent(type=CardEventType.STARTED)
        session.dispatch(event)

        assert len(client.creates) == 1
        assert session.state is not None
        assert session.state.terminal == "running"

    def test_dispatch_text_delta_updates_state(self):
        session, client, _ = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.TEXT_STARTED))
        session.dispatch(CardEvent(
            type=CardEventType.TEXT_DELTA,
            payload={"text": "Hello "}
        ))

        state = session.state
        assert state is not None
        # Should have at least one text block
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert len(text_blocks) >= 1
        assert "Hello" in text_blocks[-1].content

    def test_dispatch_tool_started_updates_structure(self):
        session, client, _ = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(
            type=CardEventType.TOOL_STARTED,
            payload={"tool_name": "bash", "block_id": "tc1"}
        ))

        state = session.state
        tool_blocks = [b for b in state.blocks if b.kind == "tool_call"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].tool_name == "bash"

    def test_dispatch_completed_closes_session(self):
        session, client, _ = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.COMPLETED))

        assert session.closed is True
        assert session.state.terminal == "completed"

    def test_dispatch_after_close_ignored(self):
        session, client, _ = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.COMPLETED))

        # This should be ignored
        session.dispatch(CardEvent(
            type=CardEventType.TEXT_DELTA,
            payload={"text": "ghost text", "block_id": "_active_text"}
        ))
        # State shouldn't change after close
        assert session.state.terminal == "completed"


class TestCardSessionLifecycle:
    """Full lifecycle tests."""

    def _make_session(self):
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(mode_name="Coco", tool_name="coco", model_name="gpt-4o")
        session = CardSession(
            chat_id="chat_1",
            metadata=metadata,
            delivery=delivery,
            session_id="test_sess",
        )
        return session, client

    def test_full_lifecycle(self):
        """Complete flow: started → text → tool → text → completed."""
        session, client = self._make_session()

        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.dispatch(CardEvent(type=CardEventType.TEXT_STARTED))
        session.dispatch(CardEvent(type=CardEventType.TEXT_DELTA, payload={"text": "Analyzing...", "block_id": "_active_text"}))
        session.dispatch(CardEvent(type=CardEventType.TEXT_DONE))
        session.dispatch(CardEvent(
            type=CardEventType.TOOL_STARTED,
            payload={"tool_name": "bash", "block_id": "tc1"}
        ))
        session.dispatch(CardEvent(
            type=CardEventType.TOOL_DONE,
            payload={"block_id": "tc1", "tool_output": "result"}
        ))
        session.dispatch(CardEvent(type=CardEventType.TEXT_STARTED))
        session.dispatch(CardEvent(type=CardEventType.TEXT_DELTA, payload={"text": "Done!", "block_id": "_active_text"}))
        session.dispatch(CardEvent(type=CardEventType.TEXT_DONE))
        session.dispatch(CardEvent(type=CardEventType.COMPLETED))

        assert session.closed
        state = session.state
        assert state.terminal == "completed"
        assert len(state.blocks) >= 3  # At least: text + tool + text
        # Verify text was actually written into state
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert any("Analyzing" in b.content for b in text_blocks)
        assert any("Done" in b.content for b in text_blocks)

    def test_close_idempotent(self):
        session, _ = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        session.close()
        session.close()  # Should not raise
        assert session.closed

    def test_thread_safety(self):
        """Multiple threads dispatching concurrently should not crash."""
        session, _ = self._make_session()
        session.dispatch(CardEvent(type=CardEventType.STARTED))

        errors = []

        def dispatch_many():
            try:
                for i in range(50):
                    session.dispatch(CardEvent(
                        type=CardEventType.TEXT_DELTA,
                        payload={"text": f"chunk_{i} ", "block_id": "_active_text"}
                    ))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=dispatch_many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestCardSessionFactory:
    """CardSessionFactory tests."""

    def test_factory_creates_session(self):
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        factory = CardSessionFactory(delivery)

        session = factory.create(
            chat_id="chat_1",
            metadata=CardMetadata(mode_name="Claude"),
        )
        assert isinstance(session, CardSession)
        assert not session.closed

    def test_factory_injects_delivery(self):
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        factory = CardSessionFactory(delivery)

        session = factory.create("chat_1", CardMetadata())
        session.dispatch(CardEvent(type=CardEventType.STARTED))
        assert len(client.creates) == 1

    def test_factory_custom_session_id(self):
        client = MockDeliveryClient()
        delivery = CardDelivery(client)
        factory = CardSessionFactory(delivery)

        session = factory.create(
            "chat_1",
            CardMetadata(),
            session_id="my_custom_id",
        )
        assert session.session_id == "my_custom_id"
