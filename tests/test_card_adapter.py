"""Tests for card adapter: engine callbacks → CardSession dispatch."""

import pytest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

from src.card.adapter import create_deep_card_callbacks, create_loop_card_callbacks
from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.session import CardSession
from src.card.state.models import CardMetadata


class MockClient:
    """Minimal mock for CardAPIClient."""
    def __init__(self):
        self._counter = 0

    def create_card(self, chat_id, card_json, *, reply_to=None):
        self._counter += 1
        return (f"msg_{self._counter}", f"card_{self._counter}")

    def update_card(self, card_id, card_json, *, sequence=0):
        pass

    def update_element(self, card_id, element_id, content, *, sequence=0):
        pass


def _make_session():
    client = MockClient()
    delivery = CardDelivery(client)
    metadata = CardMetadata(mode_name="Deep", tool_name="coco", model_name="gpt-4o")
    session = CardSession(
        chat_id="chat_test",
        metadata=metadata,
        delivery=delivery,
        session_id="adapter_test",
    )
    return session


class TestDeepCardCallbacks:
    """Deep engine callbacks adapter."""

    def test_on_analyzing_start_dispatches_started(self):
        session = _make_session()
        callbacks = create_deep_card_callbacks(session)

        callbacks.on_analyzing_start("fix the bug")
        assert session.state is not None
        assert session.state.terminal == "running"

    def test_on_event_dispatches_acp_event(self):
        session = _make_session()
        callbacks = create_deep_card_callbacks(session)
        callbacks.on_analyzing_start("task")

        # Simulate an ACPEvent (mock)
        from src.acp.models import ACPEvent, ACPEventType
        acp_event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="Hello")
        callbacks.on_event(acp_event)

        state = session.state
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert len(text_blocks) >= 1

    def test_on_project_done_dispatches_completed(self):
        session = _make_session()
        callbacks = create_deep_card_callbacks(session)
        callbacks.on_analyzing_start("task")

        # Create a mock DeepProject
        mock_project = MagicMock()
        callbacks.on_project_done(mock_project)

        assert session.closed
        assert session.state.terminal == "completed"

    def test_on_error_dispatches_failed(self):
        session = _make_session()
        callbacks = create_deep_card_callbacks(session)
        callbacks.on_analyzing_start("task")

        callbacks.on_error("Something went wrong")
        assert session.closed
        assert session.state.terminal == "failed"

    def test_on_text_dispatches_text_delta(self):
        session = _make_session()
        callbacks = create_deep_card_callbacks(session)
        callbacks.on_analyzing_start("task")

        callbacks.on_text("Hello world")
        state = session.state
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert any("Hello world" in b.content for b in text_blocks)

    def test_metadata_preserved(self):
        session = _make_session()
        callbacks = create_deep_card_callbacks(session)
        callbacks.on_analyzing_start("task")

        state = session.state
        assert state.metadata.mode_name == "Deep"
        assert state.metadata.tool_name == "coco"


class TestLoopCardCallbacks:
    """Loop engine callbacks adapter."""

    def test_on_analyzing_start_dispatches_started(self):
        session = _make_session()
        callbacks = create_loop_card_callbacks(session)
        callbacks.on_analyzing_start("iterative task")

        assert session.state is not None
        assert session.state.terminal == "running"

    def test_on_iteration_start_dispatches_progress(self):
        session = _make_session()
        callbacks = create_loop_card_callbacks(session)
        callbacks.on_analyzing_start("task")
        callbacks.on_iteration_start(1, 5)

        state = session.state
        # Should have progress in footer
        assert state.footer.progress is not None
        assert "1" in state.footer.progress

    def test_on_iteration_event_dispatches_acp(self):
        session = _make_session()
        callbacks = create_loop_card_callbacks(session)
        callbacks.on_analyzing_start("task")
        callbacks.on_iteration_start(1, 3)

        from src.acp.models import ACPEvent, ACPEventType
        acp_event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="iteration output")
        callbacks.on_iteration_event(1, acp_event)

        state = session.state
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert any("iteration output" in b.content for b in text_blocks)

    def test_on_project_done_dispatches_completed(self):
        session = _make_session()
        callbacks = create_loop_card_callbacks(session)
        callbacks.on_analyzing_start("task")

        mock_project = MagicMock()
        callbacks.on_project_done(mock_project)

        assert session.closed
        assert session.state.terminal == "completed"

    def test_on_error_dispatches_failed(self):
        session = _make_session()
        callbacks = create_loop_card_callbacks(session)
        callbacks.on_analyzing_start("task")

        callbacks.on_error("iteration failed")
        assert session.closed
        assert session.state.terminal == "failed"

    def test_multi_iteration_flow(self):
        session = _make_session()
        callbacks = create_loop_card_callbacks(session)
        callbacks.on_analyzing_start("task")

        from src.acp.models import ACPEvent, ACPEventType

        for i in range(1, 4):
            callbacks.on_iteration_start(i, 3)
            acp_event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=f"iter {i} output")
            callbacks.on_iteration_event(i, acp_event)

        state = session.state
        # Should have multiple text blocks
        text_blocks = [b for b in state.blocks if b.kind == "text"]
        assert len(text_blocks) >= 3
