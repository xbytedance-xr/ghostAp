"""Unit tests for build_error_suggestion_card in slock_engine/card_templates.py."""

from __future__ import annotations

import json

from src.slock_engine.card_templates import build_error_suggestion_card


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


class TestErrorSuggestionCardBasicStructure:
    """Verify schema 2.0, header with wathet template, and body elements."""

    def test_schema_is_2_0(self):
        card = build_error_suggestion_card("something failed", [])
        assert card["schema"] == "2.0"

    def test_header_template_is_wathet(self):
        card = build_error_suggestion_card("something failed", [])
        assert card["header"]["template"] == "wathet"

    def test_header_title_content(self):
        card = build_error_suggestion_card("something failed", [])
        title = card["header"]["title"]
        assert title["tag"] == "plain_text"
        assert "无法识别" in title["content"]

    def test_body_contains_user_input(self):
        card = build_error_suggestion_card("something failed", [])
        elements = card["body"]["elements"]
        error_elements = [
            e for e in elements
            if e.get("tag") == "markdown" and "something failed" in e.get("content", "")
        ]
        assert len(error_elements) == 1

    def test_wide_screen_mode_enabled(self):
        card = build_error_suggestion_card("err", [])
        assert card["config"]["wide_screen_mode"] is True

    def test_footer_note_present(self):
        card = build_error_suggestion_card("err", [])
        elements = card["body"]["elements"]
        # New implementation uses markdown with font color for footer hint
        footer_elements = [
            e for e in elements
            if e.get("tag") == "markdown" and "/help" in e.get("content", "")
        ]
        assert len(footer_elements) == 1


class TestErrorSuggestionCardWithSuggestions:
    """Verify suggestion buttons are rendered with correct action values."""

    def _make_suggestions(self) -> list[str]:
        return ["/role list", "/help"]

    def test_buttons_rendered_for_each_suggestion(self):
        suggestions = self._make_suggestions()
        card = build_error_suggestion_card("bad command", suggestions)
        buttons = _collect_buttons(card)
        assert len(buttons) >= len(suggestions)

    def test_button_text_contains_suggestion(self):
        suggestions = self._make_suggestions()
        card = build_error_suggestion_card("bad command", suggestions)
        buttons = _collect_buttons(card)
        button_texts = [b["text"]["content"] for b in buttons]
        assert any("/role list" in t for t in button_texts)
        assert any("/help" in t for t in button_texts)

    def test_button_action_is_slock_cmd_fix(self):
        suggestions = ["/fix"]
        card = build_error_suggestion_card("err", suggestions)
        buttons = _collect_buttons(card)
        for btn in buttons:
            value = btn.get("value", {})
            assert value.get("action") == "slock_cmd_fix"

    def test_button_value_contains_fix_command(self):
        suggestions = ["/fix"]
        card = build_error_suggestion_card("err", suggestions)
        buttons = _collect_buttons(card)
        fix_commands = [btn.get("value", {}).get("fix_command") for btn in buttons]
        assert "/fix" in fix_commands

    def test_suggestion_header_markdown_present(self):
        suggestions = ["/x"]
        card = build_error_suggestion_card("err", suggestions)
        elements = card["body"]["elements"]
        suggestion_headers = [
            e for e in elements
            if e.get("tag") == "markdown" and "您是否想要" in e.get("content", "")
        ]
        assert len(suggestion_headers) == 1

    def test_max_five_suggestions_rendered(self):
        suggestions = [f"/cmd{i}" for i in range(10)]
        card = build_error_suggestion_card("err", suggestions)
        buttons = _collect_buttons(card)
        assert len(buttons) == 5


class TestErrorSuggestionCardEmptySuggestions:
    """Verify card still renders properly with no suggestions."""

    def test_no_buttons_when_empty(self):
        card = build_error_suggestion_card("err", [])
        buttons = _collect_buttons(card)
        assert buttons == []

    def test_still_has_error_message(self):
        card = build_error_suggestion_card("something broke", [])
        elements = card["body"]["elements"]
        error_elements = [
            e for e in elements
            if e.get("tag") == "markdown" and "something broke" in e.get("content", "")
        ]
        assert len(error_elements) == 1

    def test_hr_always_present_for_footer(self):
        card = build_error_suggestion_card("err", [])
        elements = card["body"]["elements"]
        hr_elements = [e for e in elements if e.get("tag") == "hr"]
        assert len(hr_elements) == 1

    def test_card_is_valid_json_serializable(self):
        card = build_error_suggestion_card("err", [])
        # Should not raise
        serialized = json.dumps(card, ensure_ascii=False)
        assert isinstance(serialized, str)


class TestErrorSuggestionCardChannelIdPropagated:
    """Verify channel_id appears in button actions."""

    def test_channel_id_in_button_value(self):
        suggestions = ["/do"]
        card = build_error_suggestion_card("err", suggestions, channel_id="ch_123")
        buttons = _collect_buttons(card)
        for btn in buttons:
            value = btn.get("value", {})
            assert value.get("channel_id") == "ch_123"

    def test_channel_id_in_behaviors_callback(self):
        suggestions = ["/do"]
        card = build_error_suggestion_card("err", suggestions, channel_id="ch_abc")
        buttons = _collect_buttons(card)
        for btn in buttons:
            behaviors = btn.get("behaviors", [])
            assert len(behaviors) >= 1
            assert behaviors[0]["value"]["channel_id"] == "ch_abc"

    def test_empty_channel_id_default(self):
        suggestions = ["/x"]
        card = build_error_suggestion_card("err", suggestions)
        buttons = _collect_buttons(card)
        for btn in buttons:
            value = btn.get("value", {})
            assert value.get("channel_id") == ""


class TestErrorSuggestionCardLongErrorTruncated:
    """Verify long error messages are handled gracefully."""

    def test_long_error_does_not_crash(self):
        long_msg = "x" * 5000
        card = build_error_suggestion_card(long_msg, [])
        assert card["schema"] == "2.0"
        assert card["body"]["elements"]

    def test_long_error_is_truncated_in_display(self):
        long_msg = "A" * 3000
        card = build_error_suggestion_card(long_msg, [])
        elements = card["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        combined_content = " ".join(e.get("content", "") for e in md_elements)
        # Input exceeds 50 chars so it should be truncated with "..."
        assert "..." in combined_content
        # The full 3000-char string should NOT appear verbatim
        assert long_msg not in combined_content

    def test_short_error_not_truncated(self):
        short_msg = "bad cmd"
        card = build_error_suggestion_card(short_msg, [])
        elements = card["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        combined_content = " ".join(e.get("content", "") for e in md_elements)
        assert "bad cmd" in combined_content

    def test_card_serializable_with_long_error(self):
        long_msg = "Z" * 10000
        card = build_error_suggestion_card(long_msg, ["/f"])
        serialized = json.dumps(card, ensure_ascii=False)
        assert len(serialized) > 0
