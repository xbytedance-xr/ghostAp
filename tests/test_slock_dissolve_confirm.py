"""Tests for dissolve confirmation flow cards (build_dissolve_confirm_card, build_dissolve_undo_card).

Covers:
- Confirm card structure: confirm/cancel buttons with correct actions
- Confirm card content: team_name displayed
- Confirm card routing: channel_id embedded in button values
- Undo card structure: undo button with snapshot_id
- Undo card display: TTL seconds shown in text
"""

from __future__ import annotations

from src.slock_engine.card_templates import (
    build_dissolve_confirm_card,
    build_dissolve_undo_card,
)


def _collect_buttons(node: object) -> list[dict]:
    """Recursively collect all button elements from a card structure."""
    if isinstance(node, dict):
        buttons = [node] if node.get("tag") == "button" else []
        for value in node.values():
            buttons.extend(_collect_buttons(value))
        return buttons
    if isinstance(node, list):
        buttons: list[dict] = []
        for item in node:
            buttons.extend(_collect_buttons(item))
        return buttons
    return []


def _collect_markdown_texts(card: dict) -> list[str]:
    """Collect all markdown content strings from a card body."""
    texts: list[str] = []
    for element in card.get("body", {}).get("elements", []):
        if isinstance(element, dict):
            if element.get("tag") == "markdown":
                texts.append(element.get("content", ""))
            # Also look inside nested structures (responsive layout columns, etc.)
            for value in element.values():
                if isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and item.get("tag") == "markdown":
                            texts.append(item.get("content", ""))
    return texts


# ---------------------------------------------------------------------------
# build_dissolve_confirm_card tests
# ---------------------------------------------------------------------------


class TestDissolveConfirmCardStructure:
    """test_dissolve_confirm_card_structure — verify confirm/cancel buttons with correct actions."""

    def test_has_confirm_and_cancel_buttons(self):
        card = build_dissolve_confirm_card("MyTeam", channel_id="ch_123")
        buttons = _collect_buttons(card)
        actions = [b.get("value", {}).get("action") for b in buttons]
        # Must have a confirm action and a cancel action
        confirm_actions = [a for a in actions if a and "confirm" in a and "dissolve" in a]
        cancel_actions = [a for a in actions if a and "cancel" in a and "dissolve" in a]
        assert len(confirm_actions) >= 1, f"Expected confirm dissolve action, got: {actions}"
        assert len(cancel_actions) >= 1, f"Expected cancel dissolve action, got: {actions}"

    def test_confirm_button_is_danger_type(self):
        card = build_dissolve_confirm_card("MyTeam", channel_id="ch_123")
        buttons = _collect_buttons(card)
        confirm_buttons = [
            b for b in buttons
            if "confirm" in b.get("value", {}).get("action", "")
            and "dissolve" in b.get("value", {}).get("action", "")
        ]
        assert len(confirm_buttons) >= 1
        assert confirm_buttons[0]["type"] == "danger"

    def test_card_has_valid_schema(self):
        card = build_dissolve_confirm_card("TestTeam")
        assert card["schema"] == "2.0"
        assert card["config"]["wide_screen_mode"] is True
        assert "header" in card
        assert "body" in card


class TestDissolveConfirmCardTeamName:
    """test_dissolve_confirm_card_team_name_shown — verify team_name appears in card content."""

    def test_team_name_in_body_content(self):
        card = build_dissolve_confirm_card("AlphaSquad", channel_id="ch_abc")
        texts = _collect_markdown_texts(card)
        combined = " ".join(texts)
        assert "AlphaSquad" in combined

    def test_team_name_with_special_chars(self):
        card = build_dissolve_confirm_card("Team & <Test>", channel_id="ch_xyz")
        texts = _collect_markdown_texts(card)
        combined = " ".join(texts)
        assert "Team & <Test>" in combined

    def test_team_name_in_button_extra_value(self):
        card = build_dissolve_confirm_card("BetaTeam", channel_id="ch_456")
        buttons = _collect_buttons(card)
        confirm_buttons = [
            b for b in buttons
            if "confirm" in b.get("value", {}).get("action", "")
            and "dissolve" in b.get("value", {}).get("action", "")
        ]
        assert len(confirm_buttons) >= 1
        assert confirm_buttons[0]["value"]["team_name"] == "BetaTeam"


class TestDissolveConfirmChannelId:
    """test_dissolve_confirm_channel_id — verify channel_id is embedded in button action values."""

    def test_channel_id_in_confirm_button(self):
        card = build_dissolve_confirm_card("MyTeam", channel_id="ch_routing_001")
        buttons = _collect_buttons(card)
        confirm_buttons = [
            b for b in buttons
            if "confirm" in b.get("value", {}).get("action", "")
            and "dissolve" in b.get("value", {}).get("action", "")
        ]
        assert len(confirm_buttons) >= 1
        assert confirm_buttons[0]["value"]["channel_id"] == "ch_routing_001"

    def test_channel_id_in_cancel_button(self):
        card = build_dissolve_confirm_card("MyTeam", channel_id="ch_routing_002")
        buttons = _collect_buttons(card)
        cancel_buttons = [
            b for b in buttons
            if "cancel" in b.get("value", {}).get("action", "")
            and "dissolve" in b.get("value", {}).get("action", "")
        ]
        assert len(cancel_buttons) >= 1
        assert cancel_buttons[0]["value"]["channel_id"] == "ch_routing_002"

    def test_all_buttons_carry_channel_id(self):
        card = build_dissolve_confirm_card("MyTeam", channel_id="ch_all_buttons")
        buttons = _collect_buttons(card)
        assert len(buttons) >= 2, "Expected at least 2 buttons (confirm + cancel)"
        for btn in buttons:
            assert btn["value"]["channel_id"] == "ch_all_buttons", (
                f"Button with action={btn['value'].get('action')} missing channel_id"
            )


# ---------------------------------------------------------------------------
# build_dissolve_undo_card tests
# ---------------------------------------------------------------------------


class TestDissolveUndoCardStructure:
    """test_dissolve_undo_card_structure — verify undo button with snapshot_id in action value."""

    def test_has_undo_button(self):
        card = build_dissolve_undo_card("snap_abc123", channel_id="ch_undo")
        buttons = _collect_buttons(card)
        assert len(buttons) >= 1
        undo_buttons = [
            b for b in buttons
            if "undo" in b.get("value", {}).get("action", "")
            and "dissolve" in b.get("value", {}).get("action", "")
        ]
        assert len(undo_buttons) >= 1, f"Expected undo dissolve button, got actions: {[b['value'].get('action') for b in buttons]}"

    def test_snapshot_id_in_button_value(self):
        card = build_dissolve_undo_card("snap_xyz789", channel_id="ch_undo")
        buttons = _collect_buttons(card)
        undo_buttons = [
            b for b in buttons
            if "undo" in b.get("value", {}).get("action", "")
        ]
        assert len(undo_buttons) >= 1
        assert undo_buttons[0]["value"]["snapshot_id"] == "snap_xyz789"

    def test_undo_button_is_primary(self):
        card = build_dissolve_undo_card("snap_001", channel_id="ch_test")
        buttons = _collect_buttons(card)
        undo_buttons = [
            b for b in buttons
            if "undo" in b.get("value", {}).get("action", "")
        ]
        assert len(undo_buttons) >= 1
        assert undo_buttons[0]["type"] == "primary"

    def test_card_has_valid_schema(self):
        card = build_dissolve_undo_card("snap_002")
        assert card["schema"] == "2.0"
        assert card["config"]["wide_screen_mode"] is True
        assert "header" in card
        assert "body" in card


class TestDissolveUndoCardTTL:
    """test_dissolve_undo_card_ttl_displayed — verify TTL seconds are shown in the card text."""

    def test_default_ttl_30_shown(self):
        card = build_dissolve_undo_card("snap_ttl", channel_id="ch_ttl")
        texts = _collect_markdown_texts(card)
        combined = " ".join(texts)
        assert "30" in combined, f"Expected '30' in card text, got: {combined}"

    def test_custom_ttl_shown(self):
        card = build_dissolve_undo_card("snap_ttl_60", channel_id="ch_ttl", ttl=60)
        texts = _collect_markdown_texts(card)
        combined = " ".join(texts)
        assert "60" in combined, f"Expected '60' in card text, got: {combined}"

    def test_ttl_in_seconds_context(self):
        card = build_dissolve_undo_card("snap_ttl_ctx", channel_id="ch_ttl", ttl=45)
        texts = _collect_markdown_texts(card)
        combined = " ".join(texts)
        # The card should mention the TTL with a time unit indicator (秒 = seconds)
        assert "45" in combined
        assert "秒" in combined, f"Expected time unit '秒' in card text, got: {combined}"

    def test_channel_id_in_undo_button(self):
        card = build_dissolve_undo_card("snap_ch", channel_id="ch_undo_route")
        buttons = _collect_buttons(card)
        undo_buttons = [
            b for b in buttons
            if "undo" in b.get("value", {}).get("action", "")
        ]
        assert len(undo_buttons) >= 1
        assert undo_buttons[0]["value"]["channel_id"] == "ch_undo_route"
