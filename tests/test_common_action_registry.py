"""Tests for build_common_action_registry().

Covers:
1. Returns dict containing MODE_FULL, MODE_COMPACT, ENGINE_STOP action_ids
2. Each action handler returns correct CardEvent type
"""

from __future__ import annotations

from src.card.actions.dispatch import (
    ENGINE_STOP,
    MODE_COMPACT,
    MODE_FULL,
    build_common_action_registry,
)
from src.card.events import CardEvent, CardEventType


class TestBuildCommonActionRegistry:
    """Test build_common_action_registry returns expected action handlers."""

    def test_contains_expected_action_ids(self):
        """Registry must contain MODE_FULL, MODE_COMPACT, ENGINE_STOP keys."""
        registry = build_common_action_registry()
        assert MODE_FULL in registry
        assert MODE_COMPACT in registry
        assert ENGINE_STOP in registry

    def test_mode_full_returns_mode_toggled_compact_false(self):
        """MODE_FULL action handler returns MODE_TOGGLED event with compact=False."""
        registry = build_common_action_registry()
        event = registry[MODE_FULL]({})
        assert isinstance(event, CardEvent)
        assert event.type == CardEventType.MODE_TOGGLED
        assert event.payload.get("compact") is False

    def test_mode_compact_returns_mode_toggled_compact_true(self):
        """MODE_COMPACT action handler returns MODE_TOGGLED event with compact=True."""
        registry = build_common_action_registry()
        event = registry[MODE_COMPACT]({})
        assert isinstance(event, CardEvent)
        assert event.type == CardEventType.MODE_TOGGLED
        assert event.payload.get("compact") is True

    def test_engine_stop_returns_stopping_event(self):
        """ENGINE_STOP action handler returns STOPPING event."""
        registry = build_common_action_registry()
        event = registry[ENGINE_STOP]({})
        assert isinstance(event, CardEvent)
        assert event.type == CardEventType.STOPPING

    def test_all_handlers_callable(self):
        """All handlers in registry are callable and accept dict param."""
        registry = build_common_action_registry()
        for action_id, handler in registry.items():
            assert callable(handler), f"Handler for {action_id} is not callable"
            # Should not raise when called with empty dict
            result = handler({})
            assert isinstance(result, CardEvent), f"Handler for {action_id} did not return CardEvent"
