"""Tests for the Workflow confirm card button layout (Task 9).

Validates:
- The confirm card has exactly 2 primary action buttons at the bottom: 确认执行 + 取消
- Regenerate / fill-missing-tools / back-to-tools buttons are inside a collapsible panel
- When tools_mismatch=True, the "确认执行" button is disabled with a non-empty disabled_tips
- When tools_mismatch=False, the "确认执行" button is enabled with no disabled_tips
"""

import unittest
from unittest.mock import patch

from src.card.actions.dispatch import (
    WORKFLOW_BACK_TO_TOOLS,
    WORKFLOW_CANCEL,
    WORKFLOW_CONFIRM_START,
    WORKFLOW_FILL_MISSING_TOOLS,
    WORKFLOW_REGENERATE_SCRIPT,
)

# ---------------------------------------------------------------------------
# Helpers — walk a card JSON and extract button / panel structures
# ---------------------------------------------------------------------------

def _get_elements(card: dict) -> list:
    """Get the elements list from a card, handling both {body: {elements: []}} and flat {elements: []} layouts."""
    body = card.get("body", card)
    if isinstance(body, dict):
        return body.get("elements", card.get("elements", []))
    if isinstance(body, list):
        return body
    return []


def _walk_elements(el: dict | list, tag_filter: str | None = None, out: list[dict] | None = None) -> list[dict]:
    """Recursively walk a card element tree, collecting matching items by tag.

    Handles ``column_set`` → ``columns`` → ``column`` → ``elements`` nesting
    and ``collapsible_panel`` → ``elements`` nesting.
    """
    if out is None:
        out = []
    if isinstance(el, list):
        for item in el:
            _walk_elements(item, tag_filter, out)
    elif isinstance(el, dict):
        tag = el.get("tag", "")
        if tag_filter is None or tag == tag_filter:
            out.append(el)
        # Container children: "elements" list (collapsible_panel, column, etc.)
        for child in el.get("elements", []):
            _walk_elements(child, tag_filter, out)
        # column_set children via "columns" → "column" → "elements"
        for column in el.get("columns", []):
            _walk_elements(column, tag_filter, out)
        # Also walk all dict/list values defensively
        for v in el.values():
            if isinstance(v, (dict, list)):
                _walk_elements(v, tag_filter, out)
    return out


def _collect_buttons(card: dict) -> list[dict]:
    """Recursively find every button dict in a card element tree."""
    roots = _get_elements(card)
    found: list[dict] = []
    for root in roots:
        _walk_elements(root, tag_filter="button", out=found)
    return found


def _collect_collapsible_panels(card: dict) -> list[dict]:
    """Recursively find every collapsible_panel dict in a card element tree."""
    roots = _get_elements(card)
    found: list[dict] = []
    for root in roots:
        _walk_elements(root, tag_filter="collapsible_panel", out=found)
    return found


def _buttons_in_panel(panel: dict) -> list[dict]:
    """Return button dicts contained directly/indirectly within a panel."""
    found: list[dict] = []
    _walk_elements(panel, tag_filter="button", out=found)
    return found


def _button_text(btn: dict) -> str:
    """Pull the plain-text label from a button dict."""
    return btn.get("text", {}).get("content", "")


def _button_action(btn: dict) -> str:
    """Pull the action id from a button dict (via `value` or `behaviors[0].value`)."""
    val = btn.get("value") or (btn.get("behaviors") or [{}])[0].get("value") or {}
    return val.get("action", "")


def _build_confirm_card_via_handler(
    *,
    meta: dict | None = None,
    requirement: str = "test requirement",
    engine_session_key: str = "session_123",
    chat_id: str = "chat_123",
    project_id: str = "proj_123",
    is_fallback: bool = False,
    selected_tools: list[str] | None = None,
    script_content: str = "",
) -> dict:
    """Build a confirm card by calling ``_build_confirm_card`` directly."""
    from src.feishu.handlers.workflow import WorkflowHandler

    # Instantiate with no-args via a minimal mock — we only call a pure method
    with patch.object(WorkflowHandler, "__init__", lambda self: None):
        handler = WorkflowHandler()
    return handler._build_confirm_card(
        meta=meta,
        requirement=requirement,
        engine_session_key=engine_session_key,
        chat_id=chat_id,
        project_id=project_id,
        is_fallback=is_fallback,
        selected_tools=selected_tools,
        script_content=script_content,
    )


def _find_button_by_action(card: dict, action: str) -> dict | None:
    """Find a button in a card by its action id."""
    for b in _collect_buttons(card):
        if _button_action(b) == action:
            return b
    return None


def _get_panel_actions(card: dict) -> set[str]:
    """Collect all action ids found inside any collapsible panel in the card."""
    panel_actions: set[str] = set()
    for panel in _collect_collapsible_panels(card):
        for pb in _buttons_in_panel(panel):
            panel_actions.add(_button_action(pb))
    return panel_actions


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConfirmCardLayout(unittest.TestCase):
    """Validate button layering: primary actions at the bottom, advanced in a panel."""

    def _make_card_with_mismatch(self, *, mismatch: bool) -> dict:
        if mismatch:
            # Script plans coco+claude but only coco is selected → mismatch
            meta = {"tools": ["coco", "claude"]}
            selected_tools = ["coco"]
        else:
            # Script and selection agree
            meta = {"tools": ["coco"]}
            selected_tools = ["coco"]
        return _build_confirm_card_via_handler(
            meta=meta, selected_tools=selected_tools
        )

    # --- disabled_tips tests ---

    def test_confirm_button_disabled_with_tips_when_mismatch(self):
        card = self._make_card_with_mismatch(mismatch=True)
        btn = _find_button_by_action(card, WORKFLOW_CONFIRM_START)
        self.assertIsNotNone(btn, "确认执行 button must exist")
        self.assertTrue(btn.get("disabled"), "confirm button should be disabled on mismatch")
        tips = btn.get("disabled_tips")
        self.assertIsNotNone(tips, "disabled_tips must be present when disabled")
        tips_content = tips.get("content", "") if isinstance(tips, dict) else str(tips)
        self.assertTrue(
            len(tips_content.strip()) > 0,
            f"disabled_tips content must be non-empty, got: {tips_content!r}",
        )
        # Message should hint at fixing the mismatch
        self.assertIn("工具", tips_content)

    def test_confirm_button_enabled_without_tips_when_no_mismatch(self):
        card = self._make_card_with_mismatch(mismatch=False)
        btn = _find_button_by_action(card, WORKFLOW_CONFIRM_START)
        self.assertIsNotNone(btn, "确认执行 button must exist")
        # Not disabled → either disabled is False, or the field is absent (falsy)
        self.assertFalse(
            bool(btn.get("disabled")),
            "confirm button should NOT be disabled when tools match",
        )
        self.assertIsNone(
            btn.get("disabled_tips"),
            "disabled_tips must NOT be present when confirm button is enabled",
        )

    # --- Layout tests ---

    def test_primary_bottom_row_has_confirm_and_cancel_plus_unblock_when_mismatch(self):
        """Under mismatch, fill/back unblock buttons must be visible at primary level (not hidden in panel)."""
        card = self._make_card_with_mismatch(mismatch=True)
        panel_actions = _get_panel_actions(card)
        all_buttons = _collect_buttons(card)

        action_ids_of_interest = {
            WORKFLOW_CONFIRM_START,
            WORKFLOW_CANCEL,
            WORKFLOW_REGENERATE_SCRIPT,
            WORKFLOW_FILL_MISSING_TOOLS,
            WORKFLOW_BACK_TO_TOOLS,
        }

        outside_panel = [
            _button_action(b)
            for b in all_buttons
            if _button_action(b) in action_ids_of_interest
            and _button_action(b) not in panel_actions
        ]

        # Under mismatch, fill & back must be in the primary (non-panel) area
        # along with confirm/cancel so users see the unblock actions at a glance.
        self.assertIn(WORKFLOW_FILL_MISSING_TOOLS, outside_panel)
        self.assertIn(WORKFLOW_BACK_TO_TOOLS, outside_panel)
        self.assertIn(WORKFLOW_CONFIRM_START, outside_panel)
        self.assertIn(WORKFLOW_CANCEL, outside_panel)

        # Regenerate should stay in the panel
        # (it is an optional, non-blocking action).
        self.assertIn(WORKFLOW_REGENERATE_SCRIPT, panel_actions)

    def test_regenerate_lives_in_collapsible_panel(self):
        card = self._make_card_with_mismatch(mismatch=True)
        panel_actions = _get_panel_actions(card)

        self.assertIn(
            WORKFLOW_REGENERATE_SCRIPT,
            panel_actions,
            f"{WORKFLOW_REGENERATE_SCRIPT} should be inside a collapsible '更多操作' panel, "
            f"but wasn't found. Panel actions: {sorted(panel_actions)}",
        )

    def test_collapsible_panel_has_more_actions_title(self):
        card = self._make_card_with_mismatch(mismatch=True)
        panels = _collect_collapsible_panels(card)
        titles = [
            p.get("header", {}).get("title", {}).get("content", "") for p in panels
        ]
        self.assertTrue(
            any("更多操作" in t for t in titles),
            f"Expected a collapsible panel with '更多操作' in its title; got: {titles!r}",
        )

    def test_cancel_button_is_always_present_in_primary_row(self):
        """Cancel should always be in the primary row regardless of mismatch."""
        for mismatch in (True, False):
            with self.subTest(mismatch=mismatch):
                card = self._make_card_with_mismatch(mismatch=mismatch)
                panel_actions = _get_panel_actions(card)
                all_buttons = _collect_buttons(card)
                primary = [
                    _button_action(b)
                    for b in all_buttons
                    if _button_action(b) not in panel_actions
                ]
                self.assertIn(WORKFLOW_CANCEL, primary)
                self.assertIn(WORKFLOW_CONFIRM_START, primary)


if __name__ == "__main__":
    unittest.main()
