"""Tests for MODE_TOGGLED event in lifecycle reducer.

Covers:
1. Running state: MODE_TOGGLED flips compact flag and updates buttons
2. Non-running state: MODE_TOGGLED is a no-op (buttons unchanged)
"""

from __future__ import annotations

from src.card.events import CardEvent, CardEventType
from src.card.state.models import CardMetadata, CardState, FooterState
from src.card.state.reducers.lifecycle import reduce_lifecycle


class TestModeToggledReducer:
    """Test MODE_TOGGLED event handling in lifecycle reducer."""

    def _make_running_state(self, compact: bool = False) -> CardState:
        """Create a running state with engine_type='deep'."""
        return CardState(
            terminal="running",
            metadata=CardMetadata(engine_type="deep", compact=compact),
            footer=FooterState(status="thinking", status_text="思考中"),
        )

    def test_running_state_toggle_to_compact(self):
        """In running state, MODE_TOGGLED with compact=True → compact mode."""
        state = self._make_running_state(compact=False)
        event = CardEvent(type=CardEventType.MODE_TOGGLED, payload={"compact": True})
        new_state = reduce_lifecycle(state, event)

        assert new_state.metadata.compact is True
        # Should have updated buttons (mode toggle + stop)
        assert len(new_state.buttons) == 2
        # First button should be "switch to full mode" since we're now compact
        assert "完整" in new_state.buttons[0].text

    def test_running_state_toggle_to_full(self):
        """In running state, MODE_TOGGLED with compact=False → full mode."""
        state = self._make_running_state(compact=True)
        event = CardEvent(type=CardEventType.MODE_TOGGLED, payload={"compact": False})
        new_state = reduce_lifecycle(state, event)

        assert new_state.metadata.compact is False
        assert len(new_state.buttons) == 2
        # First button should be "switch to compact mode" since we're now full
        assert "精简" in new_state.buttons[0].text

    def test_running_state_toggle_without_explicit_compact(self):
        """MODE_TOGGLED without explicit compact field → invert current."""
        state = self._make_running_state(compact=False)
        event = CardEvent(type=CardEventType.MODE_TOGGLED, payload={})
        new_state = reduce_lifecycle(state, event)

        # Should have flipped to compact=True
        assert new_state.metadata.compact is True

    def test_completed_state_mode_toggled_noop(self):
        """In completed state, MODE_TOGGLED does not change buttons."""
        state = CardState(
            terminal="completed",
            metadata=CardMetadata(engine_type="deep", compact=False),
            buttons=(),  # terminal has no buttons
        )
        event = CardEvent(type=CardEventType.MODE_TOGGLED, payload={"compact": True})
        new_state = reduce_lifecycle(state, event)

        # Metadata compact should still update
        assert new_state.metadata.compact is True
        # But buttons should remain unchanged (empty)
        assert new_state.buttons == ()

    def test_failed_state_mode_toggled_preserves_buttons(self):
        """In failed state, MODE_TOGGLED preserves existing retry/show_details buttons."""
        from src.card.state.models import ButtonSpec
        retry_btn = ButtonSpec(text="🔁 重试", action_id="intent.deep.resume", type="primary")
        state = CardState(
            terminal="failed",
            metadata=CardMetadata(engine_type="deep", compact=False),
            buttons=(retry_btn,),
        )
        event = CardEvent(type=CardEventType.MODE_TOGGLED, payload={"compact": True})
        new_state = reduce_lifecycle(state, event)

        # Buttons preserved (not running, so buttons stay as-is)
        assert new_state.buttons == (retry_btn,)
