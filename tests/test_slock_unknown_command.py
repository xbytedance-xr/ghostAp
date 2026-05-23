"""Tests for unknown command feedback — AC-UX4.

Verifies that unrecognized commands produce explicit error feedback
with Levenshtein-based command suggestions in a card.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.feishu.handlers.slock import SlockHandler


class TestUnknownCommandFeedback:
    """AC-UX4: Unknown commands get error card with Levenshtein suggestions."""

    @patch("src.feishu.handlers.slock.is_slock_command", return_value=True)
    def test_unknown_command_gives_feedback_card(self, _mock_is_slock):
        """User entering an unrecognized slash command gets error card with suggestions."""
        handler = MagicMock(spec=SlockHandler)
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None

        # /xyz is not a recognized slock command → parsed as UNKNOWN
        SlockHandler.handle_slock_command(handler, "msg-1", "chat-1", "/xyz foobar")

        # reply_card should be called (not reply_text)
        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)

        # Card should contain the unrecognized input
        card_text = json.dumps(card, ensure_ascii=False)
        assert "foobar" in card_text or "/xyz" in card_text
        assert "无法识别" in card_text or "未识别" in card_text

        # Card should have schema 2.0
        assert card.get("schema") == "2.0"

    @patch("src.feishu.handlers.slock.is_slock_command", return_value=True)
    def test_unknown_command_preserves_original_input(self, _mock_is_slock):
        """The error card includes the original command input."""
        handler = MagicMock(spec=SlockHandler)
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None

        SlockHandler.handle_slock_command(handler, "msg-2", "chat-2", "/weird-cmd something")

        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)

        # The card should contain the original input
        card_text = json.dumps(card, ensure_ascii=False)
        assert "weird-cmd" in card_text

    @patch("src.feishu.handlers.slock.is_slock_command", return_value=True)
    def test_unknown_command_has_suggestions(self, _mock_is_slock):
        """Unknown command card includes Levenshtein-based suggestions."""
        handler = MagicMock(spec=SlockHandler)
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None

        # Use a typo of a real command to get meaningful suggestions
        SlockHandler.handle_slock_command(handler, "msg-3", "chat-3", "/rol list")

        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)

        # Card should have buttons with suggestions
        card_text = json.dumps(card, ensure_ascii=False)
        # Should suggest /role list or similar
        assert "role" in card_text.lower() or "task" in card_text.lower()

    @patch("src.feishu.handlers.slock.is_slock_command", return_value=True)
    def test_chitchat_also_gives_feedback_card(self, _mock_is_slock):
        """Empty/chitchat input that produces no dispatch match gives feedback card."""
        handler = MagicMock(spec=SlockHandler)
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None

        # Empty string produces UNKNOWN action
        SlockHandler.handle_slock_command(handler, "msg-4", "chat-4", "")

        # Should use reply_card, not show_slock_help
        handler.reply_card.assert_called_once()
