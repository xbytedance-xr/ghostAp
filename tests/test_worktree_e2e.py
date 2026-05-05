"""End-to-end test for the worktree card flow through the reducer pipeline.

Exercises the complete worktree lifecycle:
  TOOL_SELECT → CONFIRM → PROGRESS (partial) → PROGRESS (all done) → MERGE → CLEANUP → CANCELLED
"""

from __future__ import annotations

import pytest

from src.card.events import CardEvent, CardEventType
from src.card.state.models import CardMetadata, CardState
from src.card.state.reducer import reduce_card_state
from src.card.state.button_intent import ButtonIntent


def _reduce(state: CardState | None, event: CardEvent) -> CardState:
    """Shorthand: reduce with worktree metadata."""
    return reduce_card_state(state, event, CardMetadata(engine_type="worktree"))


class TestWorktreeE2E:
    """Full lifecycle through the worktree reducer path."""

    def _initial_state(self) -> CardState:
        return _reduce(None, CardEvent(type=CardEventType.STARTED, payload={}))

    # ------------------------------------------------------------------
    # Step 1: Tool Selection
    # ------------------------------------------------------------------

    def test_tool_select_produces_select_block(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_TOOL_SELECT,
            payload={"tools": [{"name": "coco", "id": "coco"}], "selected": ["coco"]},
        )
        state = _reduce(state, event)

        assert len(state.blocks) == 1
        assert state.blocks[0].kind == "worktree_tool_select"
        assert state.blocks[0].data["selected"] == ["coco"]

    def test_tool_select_no_selection_no_buttons(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_TOOL_SELECT,
            payload={"tools": [{"name": "coco", "id": "coco"}], "selected": []},
        )
        state = _reduce(state, event)
        # No confirm button when nothing selected
        assert len(state.buttons) == 0

    def test_tool_select_with_selection_has_confirm_button(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_TOOL_SELECT,
            payload={"tools": [{"name": "coco", "id": "coco"}], "selected": ["coco"]},
        )
        state = _reduce(state, event)
        assert len(state.buttons) == 1
        assert state.buttons[0].action_id == ButtonIntent.WORKTREE_FINISH_SELECTION

    # ------------------------------------------------------------------
    # Step 2: Confirmation
    # ------------------------------------------------------------------

    def test_confirm_produces_confirm_block(self):
        state = self._initial_state()
        state = _reduce(state, CardEvent(
            type=CardEventType.WORKTREE_TOOL_SELECT,
            payload={"tools": [{"name": "a"}], "selected": ["a"]},
        ))
        event = CardEvent(
            type=CardEventType.WORKTREE_CONFIRM,
            payload={"selected_items": [{"tool": "a", "model": "gpt-4"}], "goal": "Fix bug"},
        )
        state = _reduce(state, event)

        assert len(state.blocks) == 1
        assert state.blocks[0].kind == "worktree_confirm"
        assert state.blocks[0].data["goal"] == "Fix bug"
        # Should have start, reselect, cancel buttons
        action_ids = [b.action_id for b in state.buttons]
        assert ButtonIntent.WORKTREE_CONFIRM_START in action_ids
        assert ButtonIntent.WORKTREE_CANCEL in action_ids

    # ------------------------------------------------------------------
    # Step 3: Progress
    # ------------------------------------------------------------------

    def test_progress_partial_shows_running(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_PROGRESS,
            payload={"units": [
                {"name": "u1", "status": "running"},
                {"name": "u2", "status": "completed"},
            ]},
        )
        state = _reduce(state, event)

        assert state.blocks[0].kind == "worktree_units"
        assert state.footer.progress_pct == 50  # 1/2 completed
        assert state.footer.status == "tool_running"

    def test_progress_all_completed_shows_retry_all(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_PROGRESS,
            payload={"units": [
                {"name": "u1", "status": "completed"},
                {"name": "u2", "status": "completed"},
            ]},
        )
        state = _reduce(state, event)

        action_ids = [b.action_id for b in state.buttons]
        assert ButtonIntent.WORKTREE_RETRY_ALL in action_ids

    def test_progress_with_failures_shows_retry_failed(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_PROGRESS,
            payload={"units": [
                {"name": "u1", "status": "completed"},
                {"name": "u2", "status": "failed"},
            ]},
        )
        state = _reduce(state, event)

        action_ids = [b.action_id for b in state.buttons]
        assert ButtonIntent.WORKTREE_RETRY_FAILED in action_ids

    def test_progress_silent_mode_footer(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_PROGRESS,
            payload={"units": [{"name": "u1", "status": "running"}], "silent": True},
        )
        state = _reduce(state, event)
        assert state.footer.status_text is not None
        assert "静默" in state.footer.status_text

    # ------------------------------------------------------------------
    # Step 4: Merge
    # ------------------------------------------------------------------

    def test_merge_produces_merge_block(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_MERGE,
            payload={"merge_notes": [{"branch": "wt-1"}], "base_branch": "main"},
        )
        state = _reduce(state, event)

        assert state.blocks[0].kind == "worktree_merge"
        assert state.blocks[0].data["base_branch"] == "main"
        action_ids = [b.action_id for b in state.buttons]
        assert ButtonIntent.WORKTREE_MERGE in action_ids

    # ------------------------------------------------------------------
    # Step 5: Cleanup
    # ------------------------------------------------------------------

    def test_cleanup_summary_phase(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_CLEANUP,
            payload={
                "merge_notes": [{"branch": "wt-1"}],
                "base_branch": "main",
                "merge_results": [{"branch": "wt-1", "success": True}],
                "cleanup_phase": "summary",
            },
        )
        state = _reduce(state, event)

        assert state.blocks[0].kind == "worktree_cleanup"
        action_ids = [b.action_id for b in state.buttons]
        assert ButtonIntent.WORKTREE_MERGE in action_ids
        assert ButtonIntent.WORKTREE_CANCEL in action_ids

    def test_cleanup_with_failures_shows_retry(self):
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_CLEANUP,
            payload={
                "merge_notes": [],
                "base_branch": "dev",
                "merge_results": [{"branch": "wt-2", "success": False}],
                "cleanup_phase": "post_merge",
            },
        )
        state = _reduce(state, event)

        action_ids = [b.action_id for b in state.buttons]
        assert ButtonIntent.WORKTREE_RETRY_FAILED in action_ids
        assert ButtonIntent.WORKTREE_CLEANUP in action_ids

    # ------------------------------------------------------------------
    # Cancel terminates
    # ------------------------------------------------------------------

    def test_cancel_produces_terminal_state(self):
        state = self._initial_state()
        event = CardEvent(type=CardEventType.CANCELLED, payload={"reason": "user_cancel"})
        state = _reduce(state, event)
        assert state.terminal != "running"

    # ------------------------------------------------------------------
    # Full lifecycle: select → confirm → progress → merge → cancel
    # ------------------------------------------------------------------

    def test_full_lifecycle_version_increments(self):
        """Each event should bump state.version."""
        state = self._initial_state()
        v0 = state.version

        state = _reduce(state, CardEvent(
            type=CardEventType.WORKTREE_TOOL_SELECT,
            payload={"tools": [{"name": "x"}], "selected": ["x"]},
        ))
        assert state.version > v0

        v1 = state.version
        state = _reduce(state, CardEvent(
            type=CardEventType.WORKTREE_CONFIRM,
            payload={"selected_items": [{"tool": "x"}], "goal": "test"},
        ))
        assert state.version > v1

        v2 = state.version
        state = _reduce(state, CardEvent(
            type=CardEventType.WORKTREE_PROGRESS,
            payload={"units": [{"name": "u", "status": "completed"}]},
        ))
        assert state.version > v2

        v3 = state.version
        state = _reduce(state, CardEvent(
            type=CardEventType.WORKTREE_MERGE,
            payload={"merge_notes": [{"branch": "b"}], "base_branch": "main"},
        ))
        assert state.version > v3

    def test_completed_no_change_sets_terminal(self):
        """WORKTREE_COMPLETED_NO_CHANGE should set terminal."""
        state = self._initial_state()
        event = CardEvent(
            type=CardEventType.WORKTREE_COMPLETED_NO_CHANGE,
            payload={"units": [{"name": "u1", "status": "completed"}]},
        )
        state = _reduce(state, event)
        assert state.terminal == "completed_empty"
        action_ids = [b.action_id for b in state.buttons]
        assert ButtonIntent.WORKTREE_RETRY_ALL in action_ids
