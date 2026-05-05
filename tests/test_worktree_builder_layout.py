"""Tests for WorktreeBuilder button layout structure.

Verifies that build_responsive_layout is used for all button sections
and produces correct element structures (column_set based, not bare action tags).
"""

import json

import pytest

from src.card.builders.worktree import WorktreeBuilder
from src.card.shared import build_responsive_layout


class TestBuildResponsiveLayoutStructure:
    """Verify build_responsive_layout returns proper card elements."""

    def test_empty_buttons_returns_empty(self):
        result = build_responsive_layout([])
        assert result == []

    def test_single_button_returns_list_of_column_sets(self):
        btn = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "Test"},
            "type": "primary",
            "value": {"action": "test"},
        }
        result = build_responsive_layout([btn])
        assert isinstance(result, list)
        assert len(result) > 0
        # Schema 2.0: all layouts use column_set, not action tag
        for el in result:
            assert el.get("tag") == "column_set"

    def test_multiple_buttons_produce_column_set(self):
        """Multiple buttons are wrapped in column_set grid elements."""
        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"Btn {i}"},
                "type": "default",
                "value": {"action": f"act_{i}"},
            }
            for i in range(4)
        ]
        result = build_responsive_layout(buttons)
        assert len(result) > 0
        # All top-level elements should be column_set
        for el in result:
            assert el.get("tag") == "column_set"

    def test_no_bare_action_tags(self):
        """build_responsive_layout never produces bare 'action' tags (Schema 2.0)."""
        buttons = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "X"},
                "type": "default",
                "value": {"action": "x"},
            }
        ]
        result = build_responsive_layout(buttons)
        for el in result:
            assert el.get("tag") != "action"


class TestWorktreeBuilderLayoutIntegration:
    """Verify WorktreeBuilder card output uses column_set button layouts."""

    def _extract_buttons_from_card(self, card_json: dict) -> list[dict]:
        """Recursively find all button elements in a card structure."""
        buttons = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("tag") == "button":
                    buttons.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(card_json)
        return buttons

    def test_tool_select_card_has_buttons(self):
        """Worktree tool selection card renders buttons via build_responsive_layout."""
        tools = [
            {"tool_name": "grep", "display_name": "Grep"},
            {"tool_name": "find", "display_name": "Find"},
            {"tool_name": "sed", "display_name": "Sed"},
        ]

        _type, card_str = WorktreeBuilder._build_worktree_tool_select_card(
            tools=tools,
            selected_items=[],
            project_id="proj_1",
            title_key="worktree_select_tool_title",
            prompt_key="worktree_select_tool_prompt",
        )
        card = json.loads(card_str)

        # Should have buttons
        buttons = self._extract_buttons_from_card(card)
        assert len(buttons) >= 3

    def test_tool_select_with_selection_has_finish_button(self):
        """When items are selected, a finish button is rendered."""
        tools = [{"tool_name": "grep", "display_name": "Grep"}]
        selected = [{"tool_name": "grep", "display_name": "Grep"}]

        _type, card_str = WorktreeBuilder._build_worktree_tool_select_card(
            tools=tools,
            selected_items=selected,
            project_id="proj_1",
            title_key="worktree_select_tool_title",
            prompt_key="worktree_select_tool_prompt",
        )
        card = json.loads(card_str)

        buttons = self._extract_buttons_from_card(card)
        finish_buttons = [b for b in buttons if b.get("value", {}).get("action") == "worktree_finish_selection"]
        assert len(finish_buttons) == 1
        assert finish_buttons[0]["type"] == "primary"
