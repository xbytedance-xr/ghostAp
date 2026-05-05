"""Tests for Spec engine adapter event dispatch sequence.

Validates the correct ordering of CardEvents dispatched by
SpecRenderer callbacks through the SessionRotator → CardSession pipeline,
including cycle rotation scenarios.
"""

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.session import CardSession
from src.card.session.config import SessionConfig
from src.card.session.rotator import SessionRotator
from src.card.state.models import CardMetadata

from tests.conftest import TrackingClient


class TestSpecAdapterSequence:
    """Verify the expected event dispatch sequence for a spec engine run."""

    def _make_rotator(self):
        """Create a SessionRotator wired for spec engine."""
        client = TrackingClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(
            engine_type="spec",
            mode_name="Spec Agent",
        )

        def _create_session():
            config = SessionConfig(metadata=metadata)
            return CardSession(
                chat_id="chat_spec",
                config=config,
                delivery=delivery,
            )

        session = _create_session()
        rotator = SessionRotator(session)
        return rotator, client, delivery, metadata, _create_session

    def test_happy_path_spec_phases(self):
        """Happy path: started → phase_started → text → phase_done → completed."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 1))

        # Phase: Spec
        rotator.dispatch(CardEvent.phase_started(1, "spec"))
        rotator.dispatch(CardEvent.text_delta("_spec", "Writing spec..."))
        rotator.dispatch(CardEvent.text_done("_spec"))
        rotator.dispatch(CardEvent.phase_done(1, "spec"))

        # Phase: Plan
        rotator.dispatch(CardEvent.phase_started(1, "plan"))
        rotator.dispatch(CardEvent.text_delta("_plan", "Planning steps..."))
        rotator.dispatch(CardEvent.text_done("_plan"))
        rotator.dispatch(CardEvent.phase_done(1, "plan"))

        # Progress update
        rotator.dispatch(CardEvent.progress_updated(current=2, total=5, label="Phase 2/5"))

        rotator.dispatch(CardEvent.cycle_done(1))
        rotator.dispatch(CardEvent.completed(summary="Spec complete"))

        assert rotator._session.closed is True
        assert len(client.created) == 1
        assert len(client.updated) >= 3

    def test_cycle_rotation_archives_old_session(self):
        """Cycle rotation: old session is archived, new session gets new events."""
        rotator, client, delivery, metadata, _create_session = self._make_rotator()

        # Cycle 1
        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 3))
        rotator.dispatch(CardEvent.phase_started(1, "spec"))
        rotator.dispatch(CardEvent.text_delta("_spec", "Cycle 1 spec"))
        rotator.dispatch(CardEvent.phase_done(1, "spec"))
        rotator.dispatch(CardEvent.cycle_done(1))

        # Track which session is the old one
        old_session = rotator._session
        assert old_session.closed is False

        # Rotate for cycle 2
        new_session = _create_session()
        rotator.rotate(lambda: new_session)

        # Old session should be archived (closed via archived event)
        assert old_session.closed is True

        # Cycle 2 dispatches go to the new session
        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(2, 3))
        rotator.dispatch(CardEvent.phase_started(2, "plan"))
        rotator.dispatch(CardEvent.text_delta("_plan", "Cycle 2 plan"))
        rotator.dispatch(CardEvent.phase_done(2, "plan"))
        rotator.dispatch(CardEvent.cycle_done(2))
        rotator.dispatch(CardEvent.completed(summary="Done in cycle 2"))

        # New session closed via completed
        assert rotator._session.closed is True
        # Two cards created (one per session lifecycle)
        assert len(client.created) == 2

    def test_criteria_and_review_retry(self):
        """Criteria updates and review retry events."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 5))

        rotator.dispatch(CardEvent.criteria_updated(
            content="- [x] Criteria A\n- [ ] Criteria B",
            satisfied_count=1,
            total_count=2,
        ))

        rotator.dispatch(CardEvent.review_retry(
            cycle_num=1, attempt=1, max_attempts=3, status="executing"
        ))

        rotator.dispatch(CardEvent.cycle_done(1))
        rotator.dispatch(CardEvent.completed())

        assert rotator._session.closed is True
        assert len(client.created) == 1

    def test_failed_closes_session(self):
        """A failed event closes the session."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 3))
        rotator.dispatch(CardEvent.phase_started(1, "build"))
        rotator.dispatch(CardEvent.text_delta("_build", "Building..."))
        rotator.dispatch(CardEvent.failed("Build failed"))

        assert rotator._session.closed is True

    def test_tool_events_within_phase(self):
        """Tool calls within a spec phase are properly dispatched."""
        rotator, client, *_ = self._make_rotator()

        rotator.dispatch(CardEvent.started())
        rotator.dispatch(CardEvent.cycle_started(1, 1))
        rotator.dispatch(CardEvent.phase_started(1, "build"))

        # Tool call
        rotator.dispatch(CardEvent.tool_started("t1", "write_file", {"path": "main.py"}))
        rotator.dispatch(CardEvent.tool_done("t1", "written"))

        rotator.dispatch(CardEvent.tool_started("t2", "run_tests", {}))
        rotator.dispatch(CardEvent.tool_done("t2", "all passed"))

        rotator.dispatch(CardEvent.phase_done(1, "build"))
        rotator.dispatch(CardEvent.cycle_done(1))
        rotator.dispatch(CardEvent.completed())

        assert rotator._session.closed is True
        assert len(client.created) == 1
        assert len(client.updated) >= 4
