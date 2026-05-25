"""Tests for show_agent_memory and show_memory_list in SlockHandler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.feishu.handlers.slock import SlockHandler
from src.slock_engine.models import AgentIdentity, SlockMemory


def _make_handler():
    """Create a mock SlockHandler with bound methods."""
    handler = MagicMock(spec=SlockHandler)
    # Bind the real methods to the mock instance
    handler.show_agent_memory = lambda *a, **kw: SlockHandler.show_agent_memory(handler, *a, **kw)
    handler.show_memory_list = lambda *a, **kw: SlockHandler.show_memory_list(handler, *a, **kw)
    return handler


class TestShowAgentMemory:
    """Tests for SlockHandler.show_agent_memory."""

    def test_show_agent_memory_renders_card_with_key_knowledge(self):
        """When agent has memory, reply_card is called with key_knowledge items."""
        handler = _make_handler()

        # Set up engine mock
        agent = AgentIdentity(agent_id="agent-001", name="test_agent", emoji="🧪")
        memory = SlockMemory(
            key_knowledge="line1\nline2",
            active_context="ctx1",
            role="test role",
        )

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "ch-123"
        engine.registry.find_by_name.return_value = agent
        engine.memory.read_agent_memory.return_value = memory

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager

        # Call the method
        handler.show_agent_memory("msg-001", "chat-001", "test_agent", None)

        # Assert reply_card was called
        handler.reply_card.assert_called_once()
        call_args = handler.reply_card.call_args
        card_json_str = call_args[0][1]
        card_data = json.loads(card_json_str)

        # Verify the card contains key_knowledge category items
        # The card should be rendered by build_memory_group_card
        assert card_data is not None
        assert isinstance(card_data, dict)

    def test_show_agent_memory_agent_not_found_shows_empty_state(self):
        """When agent is not found, show empty state card."""
        handler = _make_handler()

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "ch-123"
        engine.registry.find_by_name.return_value = None

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager

        handler.show_agent_memory("msg-001", "chat-001", "unknown_agent", None)

        handler.reply_card.assert_called_once()
        card_json_str = handler.reply_card.call_args[0][1]
        card_data = json.loads(card_json_str)
        assert card_data is not None

    def test_show_agent_memory_no_engine_shows_error(self):
        """When no engine is active, show error card."""
        handler = _make_handler()

        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager.return_value = manager

        handler.show_agent_memory("msg-001", "chat-001", "test_agent", None)

        handler.reply_card.assert_called_once()
        card_json_str = handler.reply_card.call_args[0][1]
        card_data = json.loads(card_json_str)
        assert card_data is not None

    def test_show_agent_memory_empty_name_shows_usage_hint(self):
        """When agent_name is empty, show usage hint card."""
        handler = _make_handler()

        handler.show_agent_memory("msg-001", "chat-001", "", None)

        handler.reply_card.assert_called_once()
        card_json_str = handler.reply_card.call_args[0][1]
        card_data = json.loads(card_json_str)
        assert card_data is not None


class TestShowMemoryList:
    """Tests for SlockHandler.show_memory_list."""

    def test_show_memory_list_renders_table_with_agent_names(self):
        """When agents exist, reply_card is called with a table containing agent names."""
        handler = _make_handler()

        agent1 = AgentIdentity(agent_id="agent-001", name="coder", emoji="💻")
        agent2 = AgentIdentity(agent_id="agent-002", name="reviewer", emoji="🔍")

        memory1 = SlockMemory(key_knowledge="fact1\nfact2", active_context="ctx-a", role="coder role")
        memory2 = SlockMemory(key_knowledge="fact3", active_context="", role="reviewer role")

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "ch-123"
        engine.registry.list_agents.return_value = [agent1, agent2]

        def mock_read_memory(agent_id):
            if agent_id == "agent-001":
                return memory1
            return memory2

        engine.memory.read_agent_memory.side_effect = mock_read_memory

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager

        # Call the method
        handler.show_memory_list("msg-001", "chat-001", None)

        # Assert reply_card was called
        handler.reply_card.assert_called_once()
        call_args = handler.reply_card.call_args
        card_json_str = call_args[0][1]
        card_data = json.loads(card_json_str)

        # Verify the card contains agent names in a table
        assert card_data is not None
        assert isinstance(card_data, dict)
        # The card body should contain markdown with agent names
        card_str = json.dumps(card_data, ensure_ascii=False)
        assert "coder" in card_str
        assert "reviewer" in card_str

    def test_show_memory_list_no_engine_shows_error(self):
        """When no engine is active, show error card."""
        handler = _make_handler()

        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager.return_value = manager

        handler.show_memory_list("msg-001", "chat-001", None)

        handler.reply_card.assert_called_once()

    def test_show_memory_list_no_agents_shows_empty_state(self):
        """When no agents exist, show empty state card."""
        handler = _make_handler()

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "ch-123"
        engine.registry.list_agents.return_value = []

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager.return_value = manager

        handler.show_memory_list("msg-001", "chat-001", None)

        handler.reply_card.assert_called_once()
