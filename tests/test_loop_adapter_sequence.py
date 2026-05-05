"""Tests for Loop engine adapter event dispatch sequence.

Validates the correct ordering of CardEvents dispatched by
LoopRenderer callbacks through the SessionRotator → CardSession pipeline.
"""

from unittest.mock import MagicMock

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.session import CardSession
from src.card.session.config import SessionConfig
from src.card.session.rotator import SessionRotator
from src.card.state.models import CardMetadata

from tests.conftest import TrackingClient


class TestLoopAdapterSequence:
    """Verify the expected event dispatch sequence for a loop engine run."""

    def _make_rotator(self):
        """Create a SessionRotator wired for loop engine."""
        client = TrackingClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(
            engine_type="loop",
            mode_name="Loop Agent",
        )

        def _create_session():
            config = SessionConfig(metadata=metadata)
            return CardSession(
                chat_id="chat_loop",
                config=config,
                delivery=delivery,
            )

        session = _create_session()
        rotator = SessionRotator(session)
        return rotator, client, delivery, metadata, _create_session

    def test_started_then_text_then_completed(self):
        """Basic happy path: started → text → completed."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.text_delta("_main", "Hello loop"))
        rotator.dispatch(CardEvent.completed())

        assert rotator._session.closed is True
        assert len(client.created) == 1
        assert len(client.updated) >= 1

    def test_cycle_started_then_criteria_then_cycle_done(self):
        """Loop iteration cycle with criteria updates."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 3))
        rotator.dispatch(CardEvent.criteria_updated(
            content="- [x] Step 1\n- [ ] Step 2",
            satisfied_count=1,
            total_count=2,
        ))
        rotator.dispatch(CardEvent.cycle_done(1))
        rotator.dispatch(CardEvent.completed())

        assert rotator._session.closed is True

    def test_session_rotation_on_iteration_boundary(self):
        """Rotating the session creates a new card."""
        rotator, client, delivery, metadata, _create_session = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 3))
        rotator.dispatch(CardEvent.text_delta("_main", "iteration 1"))
        rotator.dispatch(CardEvent.cycle_done(1))

        # Rotate to simulate new iteration
        new_session = _create_session()
        rotator.rotate(lambda: new_session)

        # New dispatches go to the new session
        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(2, 3))
        rotator.dispatch(CardEvent.text_delta("_main", "iteration 2"))
        rotator.dispatch(CardEvent.completed())

        # Two cards should have been created (one per session)
        assert len(client.created) == 2
        assert rotator._session.closed is True

    def test_error_path_closes_session(self):
        """A failed event closes the session."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 5))
        rotator.dispatch(CardEvent.text_delta("_main", "working..."))
        rotator.dispatch(CardEvent.failed("loop diverged"))

        assert rotator._session.closed is True

    def test_warning_and_review_retry(self):
        """Warning and review_retry events don't crash the pipeline."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 3))
        rotator.dispatch(CardEvent.warning_updated("⚠️ Criteria may be too strict"))
        rotator.dispatch(CardEvent.review_retry(
            cycle_num=1, attempt=1, max_attempts=3, status="executing"
        ))
        rotator.dispatch(CardEvent.completed())

        assert rotator._session.closed is True

    def test_dispatch_after_close_is_noop(self):
        """Events dispatched after terminal are silently ignored."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.completed())
        assert rotator._session.closed is True

        update_count_before = len(client.updated)
        rotator.dispatch(CardEvent.text_delta("late", "too late"))

        assert len(client.updated) == update_count_before
