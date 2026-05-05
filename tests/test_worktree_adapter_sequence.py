"""Tests for Worktree engine adapter event dispatch sequence.

Validates the correct ordering of CardEvents dispatched through
WorktreeRenderer → CardSession.dispatch() → CardDelivery.deliver()
for the full worktree interaction flow.
"""

import pytest

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.events.worktree import (
    worktree_tool_select,
    worktree_confirm,
    worktree_progress,
    worktree_cleanup,
    worktree_merge,
    worktree_completed_no_change,
)
from src.card.session import CardSession
from src.card.session.config import SessionConfig
from src.card.state.models import CardMetadata

from tests.conftest import TrackingClient


class TestWorktreeAdapterSequence:
    """Verify the full worktree interaction flow dispatched through CardSession."""

    def _make_session(self):
        """Create a CardSession wired for worktree engine."""
        client = TrackingClient()
        delivery = CardDelivery(client)
        metadata = CardMetadata(
            engine_type="worktree",
            mode_name="Worktree",
        )
        config = SessionConfig(metadata=metadata)
        session = CardSession(
            chat_id="chat_wt",
            config=config,
            delivery=delivery,
            session_id="wt_seq_test",
        )
        return session, client

    def test_full_flow_tool_select_to_cleanup(self):
        """Full worktree flow: tool_select → confirm → progress → cleanup."""
        session, client = self._make_session()

        # Step 1: Tool selection
        session.dispatch(worktree_tool_select(
            tools=[
                {"id": "coco", "name": "Coco", "description": "AI coding"},
                {"id": "claude", "name": "Claude", "description": "Anthropic"},
            ],
            selected=["coco"],
            project_id="proj_1",
            message="Select tools:",
        ))

        # Should have created a card
        assert len(client.created) == 1

        # Step 2: Confirm selection
        session.dispatch(worktree_confirm(
            selected_items=[{"tool": "coco", "model": "gpt-4"}],
            goal="Implement feature X",
            project_id="proj_1",
        ))

        assert len(client.updated) >= 1

        # Step 3: Started + Progress
        session.dispatch(CardEvent.started())
        session.dispatch(worktree_progress(
            units=[
                {"name": "coco-unit", "status": "running", "summary": "Working..."},
            ],
            project_id="proj_1",
        ))

        # Update progress as unit completes
        session.dispatch(worktree_progress(
            units=[
                {"name": "coco-unit", "status": "completed", "summary": "Done"},
            ],
            project_id="proj_1",
        ))

        # Step 4: Cleanup/merge
        session.dispatch(worktree_cleanup(
            merge_notes=[
                {"branch": "wt/coco-unit", "status": "ready", "summary": "1 file changed"},
            ],
            base_branch="main",
            project_id="proj_1",
        ))

        # Session still open (waiting for user merge action)
        assert session.closed is False
        assert len(client.created) == 1
        assert len(client.updated) >= 4

    def test_tool_select_then_cancel(self):
        """User cancels after tool selection."""
        session, client = self._make_session()

        session.dispatch(worktree_tool_select(
            tools=[{"id": "coco", "name": "Coco", "description": "AI"}],
            selected=[],
            project_id="proj_2",
        ))

        # Cancel the session
        session.dispatch(CardEvent.cancelled(reason="User cancelled"))

        assert session.closed is True
        assert len(client.created) == 1

    def test_progress_with_failed_units(self):
        """Progress updates with failed units."""
        session, client = self._make_session()

        session.dispatch(worktree_tool_select(
            tools=[{"id": "t1", "name": "T1", "description": ""}],
            selected=["t1"],
            project_id="proj_3",
        ))

        session.dispatch(CardEvent.started())
        session.dispatch(worktree_progress(
            units=[
                {"name": "t1-unit", "status": "running", "summary": "Running"},
            ],
            project_id="proj_3",
        ))

        # Unit fails
        session.dispatch(worktree_progress(
            units=[
                {"name": "t1-unit", "status": "failed", "summary": "Error", "error": "timeout"},
            ],
            project_id="proj_3",
        ))

        # Show cleanup even with failures
        session.dispatch(worktree_cleanup(
            merge_notes=[],
            base_branch="main",
            project_id="proj_3",
            units=[{"name": "t1-unit", "status": "failed"}],
        ))

        assert session.closed is False

    def test_completed_no_change_flow(self):
        """Flow where execution produces no file changes."""
        session, client = self._make_session()

        session.dispatch(worktree_tool_select(
            tools=[{"id": "t1", "name": "T1", "description": ""}],
            selected=["t1"],
            project_id="proj_4",
        ))

        session.dispatch(CardEvent.started())
        session.dispatch(worktree_progress(
            units=[{"name": "t1-unit", "status": "completed", "summary": "Done"}],
            project_id="proj_4",
        ))

        session.dispatch(worktree_completed_no_change(
            units=[{"name": "t1-unit", "status": "completed", "summary": "Done"}],
            project_id="proj_4",
        ))

        session.dispatch(CardEvent.completed(summary="No changes"))
        assert session.closed is True

    def test_merge_flow(self):
        """Flow with explicit merge step before cleanup."""
        session, client = self._make_session()

        session.dispatch(worktree_tool_select(
            tools=[{"id": "t1", "name": "T1", "description": ""}],
            selected=["t1"],
            project_id="proj_5",
        ))

        session.dispatch(CardEvent.started())
        session.dispatch(worktree_progress(
            units=[{"name": "t1-unit", "status": "completed", "summary": "Done"}],
            project_id="proj_5",
        ))

        # Merge step
        session.dispatch(worktree_merge(
            merge_notes=[
                {"branch": "wt/t1-unit", "status": "ready", "summary": "+50/-10"},
            ],
            base_branch="main",
            project_id="proj_5",
        ))

        # After merge, cleanup with results
        session.dispatch(worktree_cleanup(
            merge_notes=[
                {"branch": "wt/t1-unit", "status": "merged", "summary": "+50/-10"},
            ],
            base_branch="main",
            merge_results=[{"branch": "wt/t1-unit", "success": True}],
            project_id="proj_5",
        ))

        session.dispatch(CardEvent.completed(summary="Merged"))
        assert session.closed is True
        assert len(client.created) == 1
