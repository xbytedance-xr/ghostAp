"""Tests for ACPStreamBridge (src/card/stream_bridge.py)."""


from src.card.events import CardEvent, CardEventType
from src.card.protocols import StreamBridge
from src.card.stream_bridge import ACPStreamBridge


class FakeDispatchable:
    """Minimal Dispatchable for testing."""

    def __init__(self):
        self.events: list[CardEvent] = []
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def dispatch(self, event: CardEvent) -> None:
        self.events.append(event)


def _make_text_chunk():
    """Create a fake ACP TEXT_CHUNK event."""
    from unittest.mock import MagicMock
    evt = MagicMock()
    evt.event_type = "text_chunk"  # Will be compared by ACPEventType
    return evt


class TestStreamBridgeProtocol:
    """Verify ACPStreamBridge satisfies the StreamBridge protocol."""

    def test_isinstance_check(self):
        """ACPStreamBridge instances pass runtime_checkable protocol check."""
        disp = FakeDispatchable()
        bridge = ACPStreamBridge(disp)
        assert isinstance(bridge, StreamBridge)

    def test_has_required_methods(self):
        """ACPStreamBridge has all Protocol-required methods."""
        assert hasattr(ACPStreamBridge, "on_event")
        assert hasattr(ACPStreamBridge, "close_open_blocks")
        assert hasattr(ACPStreamBridge, "bind")


class TestACPStreamBridgeBehavior:
    """Test on_event, close_open_blocks, and bind behavior."""

    def test_text_chunk_starts_text_block(self):
        """TEXT_CHUNK event opens a text block and dispatches events."""
        from unittest.mock import MagicMock

        from src.acp import ACPEventType

        disp = FakeDispatchable()
        bridge = ACPStreamBridge(disp)

        evt = MagicMock()
        evt.event_type = ACPEventType.TEXT_CHUNK
        evt.text = "hello"
        evt.tool = None
        evt.plan = None

        bridge.on_event(evt)

        # Should have dispatched text_started + the card_event_from_acp result
        assert len(disp.events) >= 2
        assert disp.events[0].type == CardEventType.TEXT_STARTED

    def test_close_open_blocks_closes_text(self):
        """close_open_blocks emits text_done if text was active."""
        from unittest.mock import MagicMock

        from src.acp import ACPEventType

        disp = FakeDispatchable()
        bridge = ACPStreamBridge(disp)

        evt = MagicMock()
        evt.event_type = ACPEventType.TEXT_CHUNK
        evt.text = "hello"
        evt.tool = None
        evt.plan = None

        bridge.on_event(evt)
        disp.events.clear()

        bridge.close_open_blocks()
        assert any(e.type == CardEventType.TEXT_DONE for e in disp.events)

    def test_bind_rebinds_and_closes_blocks(self):
        """bind() closes open blocks on old dispatchable and rebinds."""
        from unittest.mock import MagicMock

        from src.acp import ACPEventType

        old_disp = FakeDispatchable()
        new_disp = FakeDispatchable()
        bridge = ACPStreamBridge(old_disp)

        # Open text block
        evt = MagicMock()
        evt.event_type = ACPEventType.TEXT_CHUNK
        evt.text = "hello"
        evt.tool = None
        evt.plan = None
        bridge.on_event(evt)

        # Rebind
        bridge.bind(new_disp)

        # Old dispatchable should have text_done
        assert any(e.type == CardEventType.TEXT_DONE for e in old_disp.events)

        # New text chunk goes to new dispatchable
        bridge.on_event(evt)
        assert any(e.type == CardEventType.TEXT_STARTED for e in new_disp.events)

    def test_close_open_blocks_noop_when_no_active(self):
        """close_open_blocks does nothing if no active blocks."""
        disp = FakeDispatchable()
        bridge = ACPStreamBridge(disp)
        bridge.close_open_blocks()
        assert len(disp.events) == 0

    def test_thought_chunk_opens_reasoning(self):
        """THOUGHT_CHUNK opens a reasoning block."""
        from unittest.mock import MagicMock

        from src.acp import ACPEventType

        disp = FakeDispatchable()
        bridge = ACPStreamBridge(disp)

        evt = MagicMock()
        evt.event_type = ACPEventType.THOUGHT_CHUNK
        evt.text = "thinking..."
        evt.tool = None
        evt.plan = None

        bridge.on_event(evt)
        assert disp.events[0].type == CardEventType.REASONING_STARTED

    def test_soft_text_reasoning_alternation_reuses_logical_blocks(self):
        """Interleaved text/reasoning chunks should not become tiny card blocks."""
        from unittest.mock import MagicMock

        from src.acp import ACPEventType

        disp = FakeDispatchable()
        bridge = ACPStreamBridge(disp)

        def _event(event_type, text):
            evt = MagicMock()
            evt.event_type = event_type
            evt.text = text
            evt.tool = None
            evt.plan = None
            return evt

        bridge.on_event(_event(ACPEventType.TEXT_CHUNK, "正文第一段"))
        bridge.on_event(_event(ACPEventType.THOUGHT_CHUNK, "引用第一段"))
        bridge.on_event(_event(ACPEventType.TEXT_CHUNK, "正文第二段"))
        bridge.on_event(_event(ACPEventType.THOUGHT_CHUNK, "引用第二段"))

        text_started = [e for e in disp.events if e.type == CardEventType.TEXT_STARTED]
        reasoning_started = [e for e in disp.events if e.type == CardEventType.REASONING_STARTED]
        text_done = [e for e in disp.events if e.type == CardEventType.TEXT_DONE]
        reasoning_done = [e for e in disp.events if e.type == CardEventType.REASONING_DONE]
        text_deltas = [e for e in disp.events if e.type == CardEventType.TEXT_DELTA]
        reasoning_deltas = [e for e in disp.events if e.type == CardEventType.REASONING_DELTA]

        assert len(text_started) == 1
        assert len(reasoning_started) == 1
        assert text_done == []
        assert reasoning_done == []
        assert {e.payload["block_id"] for e in text_deltas} == {"_active_text"}
        assert {e.payload["block_id"] for e in reasoning_deltas} == {"_active_reasoning"}

        bridge.close_open_blocks()

        assert sum(e.type == CardEventType.TEXT_DONE for e in disp.events) == 1
        assert sum(e.type == CardEventType.REASONING_DONE for e in disp.events) == 1

    def test_tool_call_keeps_reasoning_summary_but_splits_streamed_text(self):
        """Tools split answer text without fragmenting one source's process summary."""
        from unittest.mock import MagicMock

        from src.acp import ACPEventType

        disp = FakeDispatchable()
        bridge = ACPStreamBridge(disp)

        def _event(event_type, text=""):
            evt = MagicMock()
            evt.event_type = event_type
            evt.text = text
            evt.tool = "Read" if event_type == ACPEventType.TOOL_CALL_START else None
            evt.tool_call = None
            evt.plan = None
            evt.args = {"file_path": "src/app.py"} if event_type == ACPEventType.TOOL_CALL_START else None
            return evt

        bridge.on_event(_event(ACPEventType.TEXT_CHUNK, "正文"))
        bridge.on_event(_event(ACPEventType.THOUGHT_CHUNK, "引用"))
        bridge.on_event(_event(ACPEventType.TOOL_CALL_START))
        bridge.on_event(_event(ACPEventType.TEXT_CHUNK, "工具后的正文"))
        bridge.on_event(_event(ACPEventType.THOUGHT_CHUNK, "继续分析"))

        assert sum(e.type == CardEventType.TEXT_DONE for e in disp.events) == 1
        assert sum(e.type == CardEventType.REASONING_DONE for e in disp.events) == 0
        text_started = [e for e in disp.events if e.type == CardEventType.TEXT_STARTED]
        assert [e.payload["block_id"] for e in text_started] == ["_active_text", "_turn_2_text"]
        reasoning_started = [e for e in disp.events if e.type == CardEventType.REASONING_STARTED]
        reasoning_deltas = [e for e in disp.events if e.type == CardEventType.REASONING_DELTA]
        assert [e.payload["block_id"] for e in reasoning_started] == ["_active_reasoning"]
        assert {e.payload["block_id"] for e in reasoning_deltas} == {"_active_reasoning"}

    def test_interleaved_text_chunks_from_different_sources_use_separate_blocks(self):
        """Concurrent agent text streams must not append into the same text block."""
        from src.acp.models import ACPEvent, ACPEventType

        disp = FakeDispatchable()
        bridge = ACPStreamBridge(disp)

        bridge.on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="Alpha ", source_id="agent-a"))
        bridge.on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="甲", source_id="agent-b"))
        bridge.on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="Beta", source_id="agent-a"))
        bridge.on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="乙", source_id="agent-b"))

        text_started = [e for e in disp.events if e.type == CardEventType.TEXT_STARTED]
        text_deltas = [e for e in disp.events if e.type == CardEventType.TEXT_DELTA]

        assert len(text_started) == 2
        assert len({e.payload["block_id"] for e in text_started}) == 2

        chunks_by_block: dict[str, list[str]] = {}
        for event in text_deltas:
            chunks_by_block.setdefault(event.payload["block_id"], []).append(event.payload["text"])

        assert sorted("".join(chunks) for chunks in chunks_by_block.values()) == ["Alpha Beta", "甲乙"]

        bridge.close_open_blocks()

        assert sum(e.type == CardEventType.TEXT_DONE for e in disp.events) == 2
