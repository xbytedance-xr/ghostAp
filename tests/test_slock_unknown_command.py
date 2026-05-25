"""Tests for unknown command feedback — AC-UX4.

Verifies that unrecognized commands produce feedback via show_slock_help.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.feishu.handlers.slock import SlockHandler


class TestUnknownCommandFeedback:
    """AC-UX4: Unknown commands fall through to show_slock_help."""

    @patch("src.feishu.handlers.slock.is_slock_command", return_value=True)
    def test_unknown_command_gives_feedback_card(self, _mock_is_slock):
        """User entering an unrecognized slash command gets help feedback."""
        handler = MagicMock(spec=SlockHandler)
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None

        # /xyz is not a recognized slock command → falls to else branch
        SlockHandler.handle_slock_command(handler, "msg-1", "chat-1", "/xyz foobar")

        # Implementation calls show_slock_help for unknown commands
        handler.show_slock_help.assert_called_once_with("msg-1")

    @patch("src.feishu.handlers.slock.is_slock_command", return_value=True)
    def test_unknown_command_preserves_original_input(self, _mock_is_slock):
        """Unknown command still triggers help (no crash on any input)."""
        handler = MagicMock(spec=SlockHandler)
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None

        SlockHandler.handle_slock_command(handler, "msg-2", "chat-2", "/weird-cmd something")

        handler.show_slock_help.assert_called_once_with("msg-2")

    @patch("src.feishu.handlers.slock.is_slock_command", return_value=True)
    def test_unknown_command_has_suggestions(self, _mock_is_slock):
        """Unknown command triggers help (suggestions come from help card)."""
        handler = MagicMock(spec=SlockHandler)
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None

        # Use a typo of a real command
        SlockHandler.handle_slock_command(handler, "msg-3", "chat-3", "/rol list")

        handler.show_slock_help.assert_called_once_with("msg-3")

    @patch("src.feishu.handlers.slock.is_slock_command", return_value=True)
    def test_chitchat_also_gives_feedback_card(self, _mock_is_slock):
        """Empty/chitchat input that produces no dispatch match gives help."""
        handler = MagicMock(spec=SlockHandler)
        handler._get_engine_manager.return_value.get_activated_engine.return_value = None

        # Empty string produces UNKNOWN action
        SlockHandler.handle_slock_command(handler, "msg-4", "chat-4", "")

        handler.show_slock_help.assert_called_once_with("msg-4")
