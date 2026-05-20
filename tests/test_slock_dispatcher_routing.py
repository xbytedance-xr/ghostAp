"""Tests for slock dispatcher routing: command scoping + message routing.

Covers:
- AC2: unmanaged chat /role /task /team passthrough; /slock captured
- AC3: managed chat normal message routes through explicit smart route execution
- AC4: @AgentName precise routing
- AC10: unmanaged chat normal message passthrough
"""

from __future__ import annotations

from concurrent.futures import Future
from unittest.mock import ANY, MagicMock

import pytest


def _sync_submit(fn, *args, **kwargs):
    """Helper that executes executor.submit synchronously for deterministic tests."""
    future = Future()
    try:
        result = fn(*args, **kwargs)
        future.set_result(result)
    except Exception as exc:
        future.set_exception(exc)
    return future

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
        """AC3: Normal text in managed chat routes to the selected agent."""
        handler = self._make_handler()
        handler.send_card_to_chat = MagicMock(return_value="placeholder-msg-001")
        handler.update_card = MagicMock(return_value=True)

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_123"
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card.return_value = {"header": {}, "elements": []}

        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-default"
        mock_agent.agent_type = "coco"
        mock_agent.model_name = ""
        engine.registry.list_agents.return_value = [mock_agent]
        engine.router.route_message.return_value = mock_agent
        engine._execute_agent.return_value = "Agent response"

        executor = MagicMock()
        executor.submit.side_effect = _sync_submit
        engine._get_executor = MagicMock(return_value=executor)

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_1", "chat_123", "hello team", None)

        engine.router.route_message.assert_called_once_with("hello team", [mock_agent])
        engine._execute_agent.assert_called_once_with(mock_agent, "hello team", ANY)

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
        handler.send_card_to_chat = MagicMock(return_value="placeholder-msg-001")
        handler.update_card = MagicMock(return_value=True)

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

        executor = MagicMock()
        executor.submit.side_effect = _sync_submit
        engine._get_executor = MagicMock(return_value=executor)

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_1", "chat_123", "@Coder please fix the bug", None)

        engine.registry.find_by_name.assert_called_with("Coder", channel_id="chat_123")
        engine._execute_agent.assert_called_once_with(target_agent, "@Coder please fix the bug", ANY)

    def test_at_mention_unknown_agent_falls_to_smart_route(self):
        """If @UnknownAgent doesn't match, fall through to explicit smart routing."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="placeholder-msg-001")
        handler.update_card = MagicMock(return_value=True)

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_123"
        engine.registry.find_by_name.return_value = None  # Not found
        engine.execute.return_value = "Handled by default agent"
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card.return_value = {"header": {}, "elements": []}

        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-default"
        mock_agent.agent_type = "coco"
        mock_agent.model_name = ""
        engine.registry.list_agents.return_value = [mock_agent]
        engine.router.route_message.return_value = mock_agent
        engine._execute_agent.return_value = "Handled by default agent"

        executor = MagicMock()
        executor.submit.side_effect = _sync_submit
        engine._get_executor = MagicMock(return_value=executor)

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_1", "chat_123", "@UnknownBot do something", None)

        engine.router.route_message.assert_called_once_with("@UnknownBot do something", [mock_agent])
        engine._execute_agent.assert_called_once_with(mock_agent, "@UnknownBot do something", ANY)


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


# ============================================================
# AC-14: Dispatcher routing chain priority
# ============================================================


class TestRoutingChainPriority:
    """AC-14: SlockModeHandler sits after SpecModeHandler and before ExitHandler."""

    def test_slock_checked_after_spec_before_exit(self):
        """AC-14: In dispatcher source, slock check follows spec and precedes exit.

        We verify by importing the dispatcher and inspecting process_with_intent
        source line order — spec check line < slock check line < exit check line.
        """
        import inspect

        from src.feishu.dispatcher import MessageDispatcher

        source = inspect.getsource(MessageDispatcher.process_with_intent)

        spec_pos = source.find("_is_spec_command")
        slock_pos = source.find("_is_slock_command")
        exit_pos = source.find("_is_exit_command")

        assert spec_pos != -1, "_is_spec_command not found in process_with_intent"
        assert slock_pos != -1, "_is_slock_command not found in process_with_intent"
        assert exit_pos != -1, "_is_exit_command not found in process_with_intent"

        assert spec_pos < slock_pos, (
            f"Spec ({spec_pos}) must appear before Slock ({slock_pos}) in routing chain"
        )
        assert slock_pos < exit_pos, (
            f"Slock ({slock_pos}) must appear before Exit ({exit_pos}) in routing chain"
        )

    def test_slock_command_does_not_fall_to_exit(self):
        """AC-14: When slock command matches, exit handler is never reached."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = True

        # /slock status is a slock command, not an exit command
        assert is_slock_command("/slock status", chat_id="ch", manager=manager) is True

    def test_spec_takes_priority_over_slock_for_spec_command(self):
        """AC-14: /spec is not captured by slock — spec handler takes precedence."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = True

        assert is_slock_command("/spec", chat_id="ch", manager=manager) is False
        assert is_slock_command("/spec start", chat_id="ch", manager=manager) is False
