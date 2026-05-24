"""Tests for dispatcher NEEDS_ACTIVATION routing (AC-14)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.feishu.dispatcher import MessageDispatcher


class TestSlockActivationRouting:
    """Verify dispatcher routes NEEDS_ACTIVATION to activation hint reply."""

    def setup_method(self):
        """Set up a mock dispatcher/client."""
        self.client = MagicMock()
        # Default stubs so dispatcher does not trip on unrelated branches
        self.client._is_deep_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._get_effective_mode.return_value = ("SMART", False)
        self.client._is_slock_active.return_value = False
        self.client._is_exit_command.return_value = False

        self.dispatcher = MessageDispatcher(self.client)

    def test_needs_activation_returns_hint(self):
        """When _is_slock_command returns 'NEEDS_ACTIVATION', _reply_text is called with activation hint."""
        self.client._is_slock_command.return_value = "NEEDS_ACTIVATION"

        self.dispatcher.process_with_intent(
            message_id="msg_001",
            chat_id="chat_001",
            text="/slock status",
            project=None,
        )

        # _reply_text must be called with text containing the activation keyword
        self.client._reply_text.assert_called_once()
        reply_args = self.client._reply_text.call_args
        reply_text = reply_args[0][1] if reply_args[0] else reply_args[1].get("text", "")
        assert "激活" in reply_text

        # _handle_slock_command must NOT be called
        self.client._handle_slock_command.assert_not_called()

    def test_active_slock_routes_to_handler(self):
        """When _is_slock_command returns True, _handle_slock_command is called."""
        self.client._is_slock_command.return_value = True

        self.dispatcher.process_with_intent(
            message_id="msg_002",
            chat_id="chat_002",
            text="/slock status",
            project=None,
        )

        # _handle_slock_command must be invoked
        self.client._handle_slock_command.assert_called_once_with(
            "msg_002", "chat_002", "/slock status", None
        )

        # _reply_text (activation hint) must NOT be called
        self.client._reply_text.assert_not_called()


class TestSlockManagedChatRouting:
    """Verify non-command messages require BOTH _is_slock_active AND _is_slock_managed_chat.

    The dispatcher (line ~121) checks:
        if self.client._is_slock_active(chat_id) and self.client._is_slock_managed_chat(chat_id):
            ...route to slock engine...

    This ensures stale or unregistered chats do not accidentally route to slock.
    """

    def setup_method(self):
        """Set up a mock dispatcher/client with all unrelated branches disabled."""
        self.client = MagicMock()
        # Bypass command-level branches
        self.client._is_deep_command.return_value = False
        self.client._is_spec_command.return_value = False
        self.client._is_slock_command.return_value = False
        # Return a mock enum-like object with .value for current_mode
        mock_mode = MagicMock()
        mock_mode.value = "SMART"
        self.client._get_effective_mode.return_value = (mock_mode, False)
        self.client._is_exit_command.return_value = False
        self.client._is_interceptable_command_match.return_value = False

        # Default: neither active nor managed
        self.client._is_slock_active.return_value = False
        self.client._is_slock_managed_chat.return_value = False

        self.dispatcher = MessageDispatcher(self.client)

    # ------------------------------------------------------------------
    # Scenario 1: active=True, managed=False -> NOT routed to slock engine
    # ------------------------------------------------------------------
    def test_active_but_not_managed_does_not_route_to_slock(self):
        """When slock is active but the chat is NOT a managed chat, message must NOT route to slock engine."""
        self.client._is_slock_active.return_value = True
        self.client._is_slock_managed_chat.return_value = False

        self.dispatcher.process_with_intent(
            message_id="msg_100",
            chat_id="chat_unregistered",
            text="hello team, any updates?",
            project=None,
        )

        # _handle_slock_message must NOT be called
        self.client._handle_slock_message.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 2: active=True, managed=True -> routed to slock engine
    # ------------------------------------------------------------------
    def test_active_and_managed_routes_to_slock(self):
        """When slock is active AND the chat is managed, message is routed to slock engine."""
        self.client._is_slock_active.return_value = True
        self.client._is_slock_managed_chat.return_value = True

        self.dispatcher.process_with_intent(
            message_id="msg_101",
            chat_id="chat_managed",
            text="please review the latest PR",
            project=None,
        )

        # _handle_slock_message must be called with correct args
        self.client._handle_slock_message.assert_called_once_with(
            "msg_101", "chat_managed", "please review the latest PR", None
        )
        # Processing reaction should be added
        self.client._add_reaction.assert_called()

    # ------------------------------------------------------------------
    # Scenario 3: active=False, managed=True -> NOT routed
    # ------------------------------------------------------------------
    def test_not_active_but_managed_does_not_route_to_slock(self):
        """When slock is NOT active but the chat is managed, message must NOT route to slock engine."""
        self.client._is_slock_active.return_value = False
        self.client._is_slock_managed_chat.return_value = True

        self.dispatcher.process_with_intent(
            message_id="msg_102",
            chat_id="chat_managed_inactive",
            text="some message",
            project=None,
        )

        # _handle_slock_message must NOT be called
        self.client._handle_slock_message.assert_not_called()

    # ------------------------------------------------------------------
    # Scenario 4: both False -> NOT routed
    # ------------------------------------------------------------------
    def test_neither_active_nor_managed_does_not_route_to_slock(self):
        """When slock is NOT active and chat is NOT managed, message must NOT route to slock engine."""
        self.client._is_slock_active.return_value = False
        self.client._is_slock_managed_chat.return_value = False

        self.dispatcher.process_with_intent(
            message_id="msg_103",
            chat_id="chat_random",
            text="random message",
            project=None,
        )

        # _handle_slock_message must NOT be called
        self.client._handle_slock_message.assert_not_called()

    # ------------------------------------------------------------------
    # Additional: verify short-circuit - _is_slock_managed_chat not called
    # when _is_slock_active is False (Python `and` short-circuits)
    # ------------------------------------------------------------------
    def test_short_circuit_skips_managed_check_when_not_active(self):
        """When _is_slock_active returns False, _is_slock_managed_chat should not be called (short-circuit)."""
        self.client._is_slock_active.return_value = False
        self.client._is_slock_managed_chat.return_value = True

        self.dispatcher.process_with_intent(
            message_id="msg_104",
            chat_id="chat_shortcircuit",
            text="test short circuit",
            project=None,
        )

        # Due to Python's `and` short-circuit, if _is_slock_active is False,
        # _is_slock_managed_chat should never be evaluated.
        self.client._is_slock_managed_chat.assert_not_called()
