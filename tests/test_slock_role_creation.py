"""Tests for slock role creation with parameter parsing.

Covers:
- AC6: /new-role Coder --tool codex --model o3-pro --emoji 🔧 creates correct AgentIdentity
- AC7: /new-role SimpleAgent (no params) uses defaults
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestCreateRoleWithParams:
    """AC6: Parameterized role creation."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine(self):
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_test"
        # Capture the registered agent
        engine.registry.register = MagicMock()
        return engine

    def test_create_role_with_all_params(self):
        """AC6: --tool codex --model o3-pro --emoji 🔧 sets fields correctly."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", 'Coder --tool codex --model o3-pro --emoji 🔧')

        # Verify the registered agent has correct fields
        call_args = engine.registry.register.call_args
        agent = call_args[0][0]  # First positional arg

        assert agent.name == "Coder"
        assert agent.agent_type == "codex"
        assert agent.model_name == "o3-pro"
        assert agent.emoji == "🔧"

    def test_create_role_with_prompt(self):
        """--prompt sets system_prompt field."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Writer --tool claude --prompt 'You are a writer'")

        call_args = engine.registry.register.call_args
        agent = call_args[0][0]

        assert agent.name == "Writer"
        assert agent.agent_type == "claude"
        assert agent.system_prompt == "You are a writer"

    def test_create_role_partial_params(self):
        """Only some params provided — others use defaults."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "Reviewer --tool gemini")

        call_args = engine.registry.register.call_args
        agent = call_args[0][0]

        assert agent.name == "Reviewer"
        assert agent.agent_type == "gemini"
        assert agent.model_name == ""  # default
        assert agent.emoji == "🤖"  # default


class TestCreateRoleDefaults:
    """AC7: Default role creation without parameters."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine(self):
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_test"
        engine.registry.register = MagicMock()
        return engine

    def test_create_role_no_params_uses_defaults(self):
        """AC7: /new-role SimpleAgent uses coco/🤖/empty defaults."""
        handler = self._make_handler()
        engine = self._make_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "SimpleAgent")

        call_args = engine.registry.register.call_args
        agent = call_args[0][0]

        assert agent.name == "SimpleAgent"
        assert agent.agent_type == "coco"
        assert agent.model_name == ""
        assert agent.emoji == "🤖"
        assert agent.system_prompt == ""
        assert agent.owner_group == "chat_test"

    def test_create_role_empty_name_shows_usage(self):
        """Empty name shows usage message."""
        handler = self._make_handler()
        handler.create_role("msg_1", "chat_test", "")
        handler.reply_text.assert_called_once()
        assert "用法" in handler.reply_text.call_args[0][1]

    def test_create_role_no_engine_shows_error(self):
        """No active engine shows activation prompt."""
        handler = self._make_handler()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.create_role("msg_1", "chat_test", "TestAgent")
        handler.reply_text.assert_called_once()
        assert "激活" in handler.reply_text.call_args[0][1]
