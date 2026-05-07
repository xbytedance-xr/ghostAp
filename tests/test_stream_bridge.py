"""Tests for ACPStreamBridge (src/card/stream_bridge.py)."""

import pytest

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
