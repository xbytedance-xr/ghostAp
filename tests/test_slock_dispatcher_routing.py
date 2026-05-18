"""Tests for slock dispatcher routing: command scoping + message routing.

Covers:
- AC2: unmanaged chat /role /task /team passthrough; /slock captured
- AC3: managed chat normal message routes to engine.execute()
- AC4: @AgentName precise routing
- AC10: unmanaged chat normal message passthrough
"""

from __future__ import annotations

import unittest.mock
from unittest.mock import ANY, MagicMock, patch

import pytest

from src.slock_engine.slash_commands import is_slock_command


# ============================================================
# AC2: Command Scoping
# ============================================================


class TestCommandScopingUnmanagedChat:
    """In unmanaged chats, only /slock and /new-team are captured."""

    def _make_manager(self, is_managed: bool):
        manager = MagicMock()
        manager.is_managed_chat.return_value = is_managed
        return manager

    @pytest.mark.parametrize("text", ["/slock", "/slock status", "/new-team MyTeam"])
    def test_global_commands_always_captured(self, text):
        """AC2: /slock and /new-team are captured even in unmanaged chats."""
        manager = self._make_manager(is_managed=False)
        assert is_slock_command(text, chat_id="unmanaged_chat", manager=manager) is True

    @pytest.mark.parametrize("text", [
        "/role list",
        "/role remove Coder",
        "/task list",
        "/task assign fix-bug Coder",
        "/team list",
        "/team status Alpha",
        "/new-role Writer",
    ])
    def test_team_commands_passthrough_in_unmanaged(self, text):
        """AC2: Team commands are NOT captured in unmanaged chats."""
        manager = self._make_manager(is_managed=False)
        assert is_slock_command(text, chat_id="unmanaged_chat", manager=manager) is False

    @pytest.mark.parametrize("text", [
        "/role list",
        "/task assign fix-bug Coder",
        "/team list",
        "/new-role Writer",
    ])
    def test_team_commands_captured_in_managed(self, text):
        """AC2: Team commands ARE captured in managed chats."""
        manager = self._make_manager(is_managed=True)
        assert is_slock_command(text, chat_id="managed_chat", manager=manager) is True


# ============================================================
# AC3: Managed chat message routing to engine
# ============================================================


class TestManagedChatMessageRouting:
    """In slock-active chats, non-command messages route to engine."""

    def _make_handler(self):
        """Build a SlockHandler with mocked dependencies."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def test_normal_message_calls_engine_execute(self):
        """AC3: Normal text in managed chat routes to engine.execute()."""
        handler = self._make_handler()
        engine = MagicMock()
        engine.execute.return_value = "Agent response"
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_123"
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card.return_value = {"header": {}, "elements": []}

        mock_agent = MagicMock()
        mock_agent.agent_type = "coco"
        engine.registry.list_agents.return_value = [mock_agent]

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_1", "chat_123", "hello team", None)

        engine.execute.assert_called_once()

    def test_no_engine_silently_returns(self):
        """If no engine active, handle_message does nothing."""
        handler = self._make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_1", "chat_123", "hello", None)
        # No crash, no reply


# ============================================================
# AC4: @AgentName precise routing
# ============================================================


class TestAtMentionRouting:
    """@AgentName routes precisely to that agent."""

    def test_at_mention_routes_to_named_agent(self):
        """AC4: @Coder routes to the Coder agent specifically."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.send_card_to_chat = MagicMock()

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_123"

        target_agent = MagicMock()
        target_agent.name = "Coder"
        target_agent.agent_type = "codex"
        engine.registry.find_by_name.return_value = target_agent
        engine._execute_agent.return_value = "Code fix applied"
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card.return_value = {"header": {}, "elements": []}

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_1", "chat_123", "@Coder please fix the bug", None)

        engine.registry.find_by_name.assert_called_with("Coder")
        engine._execute_agent.assert_called_once_with(target_agent, "@Coder please fix the bug", ANY)

    def test_at_mention_unknown_agent_falls_to_smart_route(self):
        """If @UnknownAgent doesn't match, fall through to engine.execute()."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.send_card_to_chat = MagicMock()

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_123"
        engine.registry.find_by_name.return_value = None  # Not found
        engine.execute.return_value = "Handled by default agent"
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card.return_value = {"header": {}, "elements": []}

        mock_agent = MagicMock()
        mock_agent.agent_type = "coco"
        engine.registry.list_agents.return_value = [mock_agent]

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_1", "chat_123", "@UnknownBot do something", None)

        engine.execute.assert_called_once()


# ============================================================
# AC10: Unmanaged chat passthrough
# ============================================================


class TestUnmanagedChatPassthrough:
    """Normal messages in non-slock chats must not be intercepted."""

    def test_is_slock_command_false_for_normal_text(self):
        """AC10: Normal text is never a slock command."""
        assert is_slock_command("hello world") is False
        assert is_slock_command("let's fix this bug") is False
        assert is_slock_command("") is False

    def test_no_manager_means_no_capture(self):
        """Without manager context, team commands are not captured."""
        assert is_slock_command("/role list") is False
        assert is_slock_command("/task list") is False
        assert is_slock_command("/team status") is False
