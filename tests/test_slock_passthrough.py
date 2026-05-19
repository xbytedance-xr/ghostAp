"""Tests for SlockHandler passthrough behavior (AC-11).

Validates that when slock mode is not activated for a chat,
handle_message returns None without consuming the message.
"""

from __future__ import annotations

from concurrent.futures import Future
from unittest.mock import MagicMock

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


class TestSlockPassthrough:
    """Verify SlockHandler passes through messages in non-slock chats."""

    def _make_handler(self):
        """Create a SlockHandler with mocked context."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.slock_engine_manager = MagicMock()

        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.add_reaction = MagicMock()
        return handler

    def test_non_activated_chat_returns_none(self):
        """AC-11: handle_message returns None for non-activated chat."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        result = handler.handle_message("msg-001", "chat-no-slock", "hello world")

        assert result is None
        handler.send_text_to_chat.assert_not_called()
        handler.reply_text.assert_not_called()
        handler.reply_card.assert_not_called()

    def test_non_activated_chat_no_side_effects(self):
        """Non-activated chat should not trigger any engine operations."""
        handler = self._make_handler()
        manager = handler.ctx.slock_engine_manager
        manager.get_activated_engine = MagicMock(return_value=None)

        handler.handle_message("msg-002", "chat-normal", "some code review request")

        # No engine interaction beyond the activation check
        manager.get_or_create.assert_not_called()

    def test_activated_chat_does_not_passthrough(self):
        """Activated chat should NOT passthrough — engine processes the message."""
        handler = self._make_handler()
        handler.update_card = MagicMock(return_value=True)

        engine = MagicMock()
        engine.is_active = True
        engine.channel = MagicMock()
        engine.registry = MagicMock()
        engine.registry.find_by_name = MagicMock(return_value=None)
        # engine.execute returns None (no output) — avoids json.dumps on MagicMock
        engine.execute = MagicMock(return_value=None)

        # Mock async executor to run synchronously
        executor = MagicMock()
        executor.submit.side_effect = _sync_submit
        engine._get_executor = MagicMock(return_value=executor)

        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        # For activated chats, the engine processes the message (not passthrough)
        handler.handle_message("msg-003", "chat-active-slock", "build the feature")

        # The engine's execute method should have been called (message consumed)
        engine.execute.assert_called_once()

    def test_passthrough_with_task_prefix_redirects(self):
        """Even /task commands passthrough when no engine is active."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)
        handler.handle_slock_command = MagicMock()

        # /task prefix redirects to handle_slock_command before engine check
        handler.handle_message("msg-004", "chat-no-slock", "/task list")

        # When text starts with /task, it redirects to handle_slock_command
        handler.handle_slock_command.assert_called_once()


class TestSlockPassthroughNoSideEffects:
    """Extended passthrough tests: verify zero side effects when chat is not slock-activated."""

    def _make_handler(self):
        """Create a SlockHandler with mocked context and all output methods tracked."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.slock_engine_manager = MagicMock()

        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.update_card = MagicMock()
        handler.add_reaction = MagicMock()
        return handler

    def test_get_activated_engine_called_with_chat_id(self):
        """Passthrough path must query the engine manager with the correct chat_id."""
        handler = self._make_handler()
        manager = handler.ctx.slock_engine_manager
        manager.get_activated_engine = MagicMock(return_value=None)

        handler.handle_message("msg-010", "chat-xyz-123", "some message")

        manager.get_activated_engine.assert_called_once_with("chat-xyz-123")

    def test_no_send_card_to_chat_on_passthrough(self):
        """No card should be sent when the chat has no activated engine."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        handler.handle_message("msg-011", "chat-inactive", "please do something")

        handler.send_card_to_chat.assert_not_called()

    def test_no_update_card_on_passthrough(self):
        """No card update should occur when the chat has no activated engine."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        handler.handle_message("msg-012", "chat-inactive", "update something")

        handler.update_card.assert_not_called()

    def test_no_add_reaction_on_passthrough(self):
        """No emoji reaction should be added when passing through."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        handler.handle_message("msg-013", "chat-inactive", "trigger reaction?")

        handler.add_reaction.assert_not_called()

    def test_no_engine_execute_on_passthrough(self):
        """engine.execute must never be called when get_activated_engine returns None."""
        handler = self._make_handler()
        manager = handler.ctx.slock_engine_manager
        manager.get_activated_engine = MagicMock(return_value=None)

        handler.handle_message("msg-014", "chat-passive", "run this code")

        # Since engine is None, no execute-related calls should happen
        # Verify the manager was not asked to create or fetch engines beyond the check
        manager.get_or_create.assert_not_called()
        manager.register_managed_chat.assert_not_called()
        manager.unregister_managed_chat.assert_not_called()

    def test_passthrough_returns_none_not_false(self):
        """Return value must be exactly None (not False, 0, or empty string)."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        result = handler.handle_message("msg-015", "chat-no-engine", "anything")

        assert result is None

    def test_passthrough_with_empty_text(self):
        """Empty text in a non-activated chat still passes through without side effects."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        result = handler.handle_message("msg-016", "chat-empty", "")

        assert result is None
        handler.send_card_to_chat.assert_not_called()
        handler.reply_text.assert_not_called()
        handler.reply_card.assert_not_called()
        handler.update_card.assert_not_called()

    def test_passthrough_with_none_text(self):
        """None text in a non-activated chat still passes through without side effects."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        result = handler.handle_message("msg-017", "chat-none-text", None)

        assert result is None
        handler.send_card_to_chat.assert_not_called()
        handler.reply_text.assert_not_called()
        handler.reply_card.assert_not_called()
        handler.update_card.assert_not_called()

    def test_passthrough_with_at_mention_syntax(self):
        """@AgentName syntax in a non-activated chat passes through silently."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        result = handler.handle_message("msg-018", "chat-no-slock", "@Coder please fix this")

        assert result is None
        handler.send_card_to_chat.assert_not_called()
        handler.reply_text.assert_not_called()
        handler.update_card.assert_not_called()

    def test_passthrough_multiple_messages_same_chat(self):
        """Multiple messages to the same non-activated chat all pass through independently."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        result1 = handler.handle_message("msg-019a", "chat-multi", "first message")
        result2 = handler.handle_message("msg-019b", "chat-multi", "second message")
        result3 = handler.handle_message("msg-019c", "chat-multi", "third message")

        assert result1 is None
        assert result2 is None
        assert result3 is None

        # get_activated_engine called once per message
        assert handler.ctx.slock_engine_manager.get_activated_engine.call_count == 3
        # Still no side effects after multiple calls
        handler.send_card_to_chat.assert_not_called()
        handler.reply_text.assert_not_called()
        handler.reply_card.assert_not_called()
        handler.update_card.assert_not_called()

    def test_passthrough_different_chats_all_inactive(self):
        """Messages to different non-activated chats all pass through."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        results = []
        for i, chat_id in enumerate(["chat-A", "chat-B", "chat-C"]):
            result = handler.handle_message(f"msg-020-{i}", chat_id, f"message for {chat_id}")
            results.append(result)

        assert all(r is None for r in results)
        handler.send_card_to_chat.assert_not_called()
        handler.update_card.assert_not_called()

    def test_passthrough_does_not_call_execute_async(self):
        """The internal _execute_async helper must not be invoked on passthrough."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)
        handler._execute_async = MagicMock()

        result = handler.handle_message("msg-021", "chat-no-engine", "run something")

        assert result is None
        handler._execute_async.assert_not_called()

    def test_passthrough_does_not_invoke_create_callbacks(self):
        """_create_callbacks must not be invoked when engine is None."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)
        handler._create_callbacks = MagicMock()

        result = handler.handle_message("msg-022", "chat-no-engine", "do work")

        assert result is None
        handler._create_callbacks.assert_not_called()
