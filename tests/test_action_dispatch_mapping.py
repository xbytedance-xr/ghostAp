"""Tests for action_dispatch registries — ensure every worktree action_id constant
has a matching factory in build_worktree_action_registry() and each factory returns a CardEvent.
"""

from __future__ import annotations

import pytest

from src.card.action_dispatch import build_worktree_action_registry
from src.card import action_ids
from src.card.events import CardEvent


# All WORKTREE_* constants from action_ids that should be in the registry.
_WORKTREE_ACTION_IDS = [
    action_ids.WORKTREE_FINISH_SELECTION,
    action_ids.WORKTREE_CONFIRM_START,
    action_ids.WORKTREE_MERGE,
    action_ids.WORKTREE_CLEANUP,
    action_ids.WORKTREE_RETRY_FAILED,
    action_ids.WORKTREE_RETRY_ALL,
    action_ids.WORKTREE_CANCEL,
    action_ids.SHOW_WORKTREE_MENU,
    # Common actions (inherited from build_common_action_registry)
    action_ids.MODE_FULL,
    action_ids.MODE_COMPACT,
    action_ids.ENGINE_STOP,
]


class TestBuildWorktreeActionRegistry:
    """Validate build_worktree_action_registry() coverage and correctness."""

    def test_all_worktree_ids_present(self):
        """Every expected worktree action_id has an entry in the registry."""
        registry = build_worktree_action_registry()
        for aid in _WORKTREE_ACTION_IDS:
            assert aid in registry, f"action_id {aid!r} missing from worktree registry"

    def test_no_extra_keys(self):
        """Registry contains only known worktree action_ids (no stale entries)."""
        registry = build_worktree_action_registry()
        expected = set(_WORKTREE_ACTION_IDS)
        extra = set(registry.keys()) - expected
        assert not extra, f"Unexpected keys in worktree registry: {extra}"

    @pytest.mark.parametrize("action_id", _WORKTREE_ACTION_IDS)
    def test_factory_returns_card_event(self, action_id: str):
        """Each factory in the registry returns a CardEvent when called with a dict payload."""
        registry = build_worktree_action_registry()
        factory = registry[action_id]
        event = factory({"test": True})
        assert isinstance(event, CardEvent), (
            f"factory for {action_id!r} returned {type(event).__name__}, expected CardEvent"
        )

    @pytest.mark.parametrize("action_id", _WORKTREE_ACTION_IDS)
    def test_factory_returns_card_event_with_empty_payload(self, action_id: str):
        """Factories handle empty payload without error."""
        registry = build_worktree_action_registry()
        factory = registry[action_id]
        event = factory({})
        assert isinstance(event, CardEvent)

    def test_registry_values_are_callable(self):
        """All values in the registry are callable."""
        registry = build_worktree_action_registry()
        for aid, factory in registry.items():
            assert callable(factory), f"Registry value for {aid!r} is not callable"

    def test_cancel_factory_ignores_payload(self):
        """WORKTREE_CANCEL factory produces fixed payload regardless of input."""
        registry = build_worktree_action_registry()
        event = registry[action_ids.WORKTREE_CANCEL]({"arbitrary": "data"})
        assert event.payload == {"reason": "user_cancel"}
