"""Tests for Deep engine adapter event dispatch sequence.

Validates the correct ordering of CardEvents dispatched by
DeepRenderer callbacks through the CardSession pipeline.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.session import CardSession
from src.card.session.config import SessionConfig
from src.card.state.models import CardMetadata

from tests.conftest import TrackingClient


class TestDeepAdapterSequence:
    """Verify the expected event dispatch sequence for a deep engine run."""

    def _make_session(self):
        """Create a CardSession wired for deep engine."""
        client = TrackingClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(
            engine_type="deep",
            mode_name="Deep Agent",
        )
        config = SessionConfig(metadata=metadata)
        session = CardSession(
            chat_id="chat_deep",
            config=config,
            delivery=delivery,
            session_id="deep_seq_test",
        )
        return session, client

    def test_started_then_text_then_completed(self):
        """Basic happy path: started → text → completed."""
        session, client = self._make_session()

        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.text_started("_main_text"))
        session.dispatch(CardEvent.text_delta("_main_text", "Hello world"))
        session.dispatch(CardEvent.text_done("_main_text"))
        session.dispatch(CardEvent.completed(summary="Done"))

        assert session.closed is True
        # Should have created one card and updated it multiple times
        assert len(client.created) == 1
        assert len(client.updated) >= 1

    def test_started_then_progress_then_completed(self):
        """Progress events update the footer."""
        session, client = self._make_session()

        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.progress_updated(current=1, total=5, label="Working"))
        session.dispatch(CardEvent.progress_updated(current=3, total=5, label="Working"))
        session.dispatch(CardEvent.completed(summary="All done"))

        assert session.closed is True

    def test_started_then_error_closes_session(self):
        """A failed event closes the session."""
        session, client = self._make_session()

        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.text_started("_main"))
        session.dispatch(CardEvent.text_delta("_main", "partial output"))
        session.dispatch(CardEvent.failed("something broke"))

        assert session.closed is True

    def test_warning_banner_dispatch(self):
        """Warning events are accepted and don't crash the pipeline."""
        session, client = self._make_session()

        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.warning_updated("⏳ Taking longer than expected"))
        session.dispatch(CardEvent.completed())

        assert session.closed is True

    def test_dispatch_after_close_is_noop(self):
        """Events dispatched after terminal are silently ignored."""
        session, client = self._make_session()

        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.completed())
        assert session.closed is True

        # Further dispatches should be no-ops
        update_count_before = len(client.updated)
        session.dispatch(CardEvent.text_started("late"))
        session.dispatch(CardEvent.text_delta("late", "too late"))

        # No additional updates should have been made
        assert len(client.updated) == update_count_before

    def test_multiple_tools_sequence(self):
        """Multiple tool events interleaved with text."""
        session, client = self._make_session()

        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.text_started("_main"))
        session.dispatch(CardEvent.text_delta("_main", "Starting analysis"))
        session.dispatch(CardEvent.text_done("_main"))

        # First tool
        session.dispatch(CardEvent.progress_updated(current=1, total=3, label="Tool 1"))
        session.dispatch(CardEvent.tool_started("tool_1", "read_file", {"path": "foo.py"}))
        session.dispatch(CardEvent.tool_done("tool_1", "file content here"))

        # Second tool
        session.dispatch(CardEvent.progress_updated(current=2, total=3, label="Tool 2"))
        session.dispatch(CardEvent.tool_started("tool_2", "write_file", {"path": "bar.py"}))
        session.dispatch(CardEvent.tool_done("tool_2", "written"))

        session.dispatch(CardEvent.completed(summary="3 tools executed"))

        assert session.closed is True
        # Verify card was created and updated
        assert len(client.created) == 1
        assert len(client.updated) >= 3  # Multiple state changes
