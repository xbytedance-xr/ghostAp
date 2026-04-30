"""Tests for main reducer orchestration."""
from src.card.events import CardEvent, CardEventType
from src.card.state.models import CardState, CardMetadata
from src.card.state.reducer import reduce_card_state


def _meta() -> CardMetadata:
    return CardMetadata(project_name="Ghost", mode_name="Deep Agent", mode_emoji="🧠",
                        tool_name="coco", model_name="gpt-4o", engine_type="deep")


class TestMainReducer:
    def test_none_state_initializes(self):
        s = reduce_card_state(None, CardEvent.started(), metadata=_meta())
        assert s.version == 1
        assert s.terminal == "running"
        assert s.metadata.tool_name == "coco"

    def test_version_increments(self):
        s = reduce_card_state(None, CardEvent.started(), metadata=_meta())
        s = reduce_card_state(s, CardEvent.text_delta("b1", "hi"))
        s = reduce_card_state(s, CardEvent.text_delta("b1", " there"))
        assert s.version == 3

    def test_full_sequence(self):
        s = reduce_card_state(None, CardEvent.started(), metadata=_meta())
        s = reduce_card_state(s, CardEvent.text_delta("b1", "Planning..."))
        s = reduce_card_state(s, CardEvent.tool_started("t1", "bash", "ls"))
        s = reduce_card_state(s, CardEvent.tool_done("t1", tool_output="files"))
        s = reduce_card_state(s, CardEvent.text_delta("b2", "Done!"))
        s = reduce_card_state(s, CardEvent.completed())
        assert s.terminal == "completed"
        assert len(s.blocks) == 3  # text, tool, text
        assert s.blocks[1].tool_output == "files"

    def test_unknown_event_no_change(self):
        s = reduce_card_state(None, CardEvent.started(), metadata=_meta())
        v = s.version
        # Use approval events (not routed to any sub-reducer that modifies state significantly)
        s2 = reduce_card_state(s, CardEvent(type=CardEventType.APPROVAL_REQUESTED))
        # version should still increment since a new state is returned from the fallback
        # Actually the code returns state unchanged for truly unknown events
        # APPROVAL_REQUESTED is not in any event set, so goes to else branch
        # The else branch returns state (same object), so version should NOT increment
        assert s2.version == v

    def test_tool_model_changed(self):
        s = reduce_card_state(None, CardEvent.started(), metadata=_meta())
        s = reduce_card_state(s, CardEvent.tool_model_changed(model_name="claude-sonnet-4-20250514"))
        assert s.metadata.model_name == "claude-sonnet-4-20250514"
        assert s.metadata.tool_name == "coco"  # unchanged
        assert "claude-sonnet-4-20250514" in (s.header.subtitle or "")

    def test_progress_updated(self):
        s = reduce_card_state(None, CardEvent.started(), metadata=_meta())
        s = reduce_card_state(s, CardEvent.progress_updated(3, 6, "Build"))
        assert "50%" in (s.footer.progress or "")
        assert "3/6" in (s.footer.progress or "")

    def test_multiple_text_blocks(self):
        s = reduce_card_state(None, CardEvent.started(), metadata=_meta())
        s = reduce_card_state(s, CardEvent.text_started("b1"))
        s = reduce_card_state(s, CardEvent.text_delta("b1", "first"))
        s = reduce_card_state(s, CardEvent.text_done("b1"))
        s = reduce_card_state(s, CardEvent.text_started("b2"))
        s = reduce_card_state(s, CardEvent.text_delta("b2", "second"))
        assert len(s.blocks) == 2
        assert s.blocks[0].content == "first"
        assert s.blocks[1].content == "second"
