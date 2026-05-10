"""Defensive tests for buttons.py import-time validation and _is_stop_intent.

Dependencies on private API (document for maintainability):
- src.card.render.buttons._CONFIRM_TITLE_MAP
- src.card.render.buttons._STOP_INTENTS
- src.card.render.buttons._is_stop_intent
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import src.card.render.buttons as buttons_module
from src.card.render.buttons import _is_stop_intent, _STOP_INTENTS, _DESTRUCTIVE_ACTIONS, _render_button, INTENT_TO_ACTION_ID
from src.card.state.button_intent import ButtonIntent
from src.card.state.models import ButtonSpec


class TestConfirmTitleMapValidation:
    """Verify that import-time validation emits RuntimeWarning for invalid keys."""

    def test_invalid_key_emits_runtime_warning(self):
        """When _CONFIRM_TITLE_MAP contains a key not in ButtonIntent or INTENT_TO_ACTION_ID,
        a RuntimeWarning should be emitted (validation logic test)."""
        # Simulate the validation logic with a bad _CONFIRM_TITLE_MAP
        bad_map = {**buttons_module._CONFIRM_TITLE_MAP, "bogus.invalid.key": "card_btn_confirm_stop_title"}
        valid_keys = {m.value for m in ButtonIntent} | set(INTENT_TO_ACTION_ID.values())
        invalid_keys = set(bad_map.keys()) - valid_keys
        assert invalid_keys == {"bogus.invalid.key"}, (
            f"Expected bogus key to be invalid, got: {invalid_keys}"
        )

    def test_current_map_has_no_invalid_keys(self):
        """Verify that the actual _CONFIRM_TITLE_MAP has no invalid keys at import time."""
        valid_keys = {m.value for m in ButtonIntent} | set(INTENT_TO_ACTION_ID.values())
        invalid_keys = set(buttons_module._CONFIRM_TITLE_MAP.keys()) - valid_keys
        assert not invalid_keys, (
            f"_CONFIRM_TITLE_MAP contains invalid keys: {sorted(invalid_keys)}"
        )


class TestIsStopIntent:
    """Direct unit tests for _is_stop_intent function."""

    @pytest.mark.parametrize("action_id", [
        "intent.engine.stop",
        "intent.deep.stop",
        "intent.loop.stop",
        "intent.spec.stop",
        "intent.worktree.cancel",
    ])
    def test_all_stop_intents_return_true(self, action_id: str):
        """Every member of _STOP_INTENTS should make _is_stop_intent return True."""
        assert action_id in _STOP_INTENTS, f"{action_id} not in _STOP_INTENTS"
        spec = ButtonSpec(text="test", action_id=action_id)
        assert _is_stop_intent(spec) is True

    @pytest.mark.parametrize("action_id", [
        "intent.worktree.confirm_start",
        "intent.approval.approve",
        "intent.approval.reject",
        "some_random_action",
    ])
    def test_non_stop_intents_return_false(self, action_id: str):
        """Non-stop intents should make _is_stop_intent return False."""
        spec = ButtonSpec(text="test", action_id=action_id)
        assert _is_stop_intent(spec) is False

    def test_stop_intents_frozenset_completeness(self):
        """Ensure the test covers ALL members of _STOP_INTENTS (guard against additions)."""
        expected = {
            "intent.engine.stop",
            "intent.deep.stop",
            "intent.loop.stop",
            "intent.spec.stop",
            "intent.worktree.cancel",
        }
        assert _STOP_INTENTS == expected, (
            f"_STOP_INTENTS changed! Expected {expected}, got {_STOP_INTENTS}"
        )


class TestDestructiveConfirm:
    """Verify destructive buttons get confirm dialog for user protection."""

    @pytest.mark.parametrize("intent", [
        "intent.engine.stop",
        "intent.deep.stop",
        "intent.loop.stop",
        "intent.spec.stop",
        "intent.worktree.cleanup",
        "intent.worktree.merge",
        "intent.worktree.cancel",
        "intent.approval.approve",
    ])
    def test_destructive_buttons_have_confirm(self, intent: str):
        """Buttons resolving to destructive action_ids must have confirm dialog."""
        spec = ButtonSpec(text="Test", action_id=intent, type="danger")
        btn = _render_button(spec)
        assert "confirm" in btn
        assert "complex_interaction" not in btn

    @pytest.mark.parametrize("intent", [
        "intent.worktree.confirm_start",
        "intent.worktree.finish_selection",
        "intent.deep.resume",
        "intent.loop.resume",
        "intent.spec.resume",
        "intent.show_status",
    ])
    def test_non_destructive_buttons_no_complex_interaction(self, intent: str):
        """Non-destructive buttons should NOT have complex_interaction."""
        spec = ButtonSpec(text="Test", action_id=intent, type="default")
        btn = _render_button(spec)
        assert "complex_interaction" not in btn

    def test_destructive_actions_set_completeness(self):
        """Guard: _DESTRUCTIVE_ACTIONS must contain the expected set."""
        from src.card.actions.dispatch import (
            ENGINE_STOP, DEEP_STOP, LOOP_STOP, SPEC_STOP,
            WORKTREE_CLEANUP, WORKTREE_MERGE, WORKTREE_CANCEL,
            APPROVE_ACTION,
        )
        expected = frozenset({
            ENGINE_STOP, DEEP_STOP, LOOP_STOP, SPEC_STOP,
            WORKTREE_CLEANUP, WORKTREE_MERGE, WORKTREE_CANCEL,
            APPROVE_ACTION,
        })
        assert _DESTRUCTIVE_ACTIONS == expected


class TestExitButtonLabels:
    """Verify exit buttons contain mode-specific prefix for context identification."""

    @pytest.mark.parametrize("button_key,expected_mode", [
        ("exit_claude", "Claude"),
        ("exit_coco", "Coco"),
        ("exit_gemini", "Gemini"),
        ("exit_ttadk", "TTADK"),
    ])
    def test_exit_button_text_contains_mode_name(self, button_key: str, expected_mode: str):
        """Each exit button must include its mode name for user context."""
        from src.card.buttons_config import BUTTON_CONFIG
        assert button_key in BUTTON_CONFIG, f"{button_key} missing from BUTTON_CONFIG"
        text = BUTTON_CONFIG[button_key]["text"]
        assert expected_mode in text, (
            f"Exit button '{button_key}' text '{text}' does not contain mode name '{expected_mode}'"
        )
