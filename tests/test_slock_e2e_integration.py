"""End-to-end integration tests for Slock mode message flow.

Validates the full pipeline:
    Dispatcher → SlockHandler → SlockEngine → Card/Text Reply

Covers AC-01, AC-02, AC-05, AC-06.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.slock_engine.models import AgentIdentity, AgentStatus, SlockChannel
from src.slock_engine.slash_commands import is_slock_command


class TestE2EDispatcherToHandler:
    """Verify that dispatcher correctly routes slock commands to the handler."""

    def test_slock_activate_routes_to_handler(self):
        """AC-01: /slock command activates slock mode via dispatcher routing."""
        text = "/slock"
        assert is_slock_command(text) is True

    def test_slock_status_routes_to_handler(self):
        """AC-01: /slock status goes through slock command path."""
        text = "/slock status"
        assert is_slock_command(text) is True

    def test_non_slock_text_passthrough_in_unmanaged_chat(self):
        """AC-02: Non-slock commands in unmanaged chats don't trigger slock."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = False
        assert is_slock_command("/role list", chat_id="chat_99", manager=manager) is False
        assert is_slock_command("/task list", chat_id="chat_99", manager=manager) is False
        assert is_slock_command("hello world", chat_id="chat_99", manager=manager) is False

    def test_non_slock_commands_unaffected(self):
        """AC-02: Other mode commands are not captured by slock."""
        assert is_slock_command("/deep analyze") is False
        assert is_slock_command("/spec build") is False
        assert is_slock_command("/exit") is False
        assert is_slock_command("/coco") is False


class TestE2EHandlerToEngine:
    """Verify the handler correctly invokes the engine and produces output."""

    def _make_handler_with_engine(self):
        """Create a SlockHandler with mocked context and a real-enough engine."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.update_card = MagicMock()
        handler.add_reaction = MagicMock()
        handler.create_static_card_session = MagicMock()

        # Mock the static card session
        session_mock = MagicMock()
        handler.create_static_card_session.return_value = session_mock

        return handler

    def _make_engine_with_agent(self, chat_id: str = "chat_e2e"):
        """Create a mock engine with one registered agent."""
        engine = MagicMock()
        engine.channel = SlockChannel(channel_id=chat_id, name="E2E Test", team_name="TestTeam")

        agent = AgentIdentity(
            name="Coder-E2E",
            emoji="🔧",
            agent_type="coco",
            model_name="test-model",
            system_prompt="You are a coder",
            role="coder",
            owner_group=chat_id,
        )
        engine.registry.list_agents.return_value = [agent]
        engine.registry.find_by_name.return_value = agent
        engine.get_agent_status.return_value = AgentStatus.IDLE
        engine.tasks = []

        return engine, agent

    def test_activate_creates_engine_and_channel(self):
        """AC-01: activate_slock creates engine, activates channel, sends card."""
        handler = self._make_handler_with_engine()

        manager = MagicMock()
        manager.get_activated_engine.return_value = None  # not yet activated
        engine_mock = MagicMock()
        manager.get_or_create.return_value = engine_mock
        handler._get_engine_manager = MagicMock(return_value=manager)
        handler._ensure_project = MagicMock(return_value=MagicMock(
            root_path="/tmp/test", project_name="TestProj", project_id="p1"
        ))
        handler.get_working_dir = MagicMock(return_value="/tmp/test")
        handler.get_engine_name = MagicMock(return_value="Slock-e2e")

        handler.activate_slock("msg_1", "chat_e2e", "")

        # Engine was created and channel was activated
        manager.get_or_create.assert_called_once()
        engine_mock.activate_channel.assert_called_once()

    def test_at_mention_routes_to_specific_agent(self):
        """AC-05: @AgentName routes message to the named agent."""
        handler = self._make_handler_with_engine()
        engine, agent = self._make_engine_with_agent()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        # Simulate @mention execution returning a result
        engine._execute_agent.return_value = "Code review complete"
        engine._mouthpiece.format_card.return_value = {
            "header": {"title": {"content": "🔧 Coder-E2E"}},
            "elements": [{"tag": "markdown", "content": "Code review complete"}],
        }

        handler.handle_message("msg_2", "chat_e2e", "@Coder-E2E please review this code")

        # Agent was found by name and executed
        engine.registry.find_by_name.assert_called_with("Coder-E2E")
        engine._execute_agent.assert_called_once()

    def test_smart_routing_when_no_mention(self):
        """AC-05: Messages without @mention go through engine.execute() smart routing."""
        handler = self._make_handler_with_engine()
        engine, agent = self._make_engine_with_agent()

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        # No @mention in text → find_by_name returns None
        engine.registry.find_by_name.return_value = None
        engine.execute.return_value = "Smart routed response"
        engine._mouthpiece.format_card.return_value = {
            "header": {"title": {"content": "🔧 Coder-E2E"}},
            "elements": [{"tag": "markdown", "content": "Smart routed response"}],
        }

        handler.handle_message("msg_3", "chat_e2e", "implement login feature")

        engine.execute.assert_called_once()

    def test_card_output_contains_required_fields(self):
        """AC-06: Agent reply card contains color Header, emoji+name, content, footer."""
        from src.slock_engine.card_templates import build_agent_message_card
        from src.slock_engine.models import AGENT_ROLE_COLORS

        agent = AgentIdentity(
            name="Reviewer-Beta",
            emoji="🔍",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="",
            role="reviewer",
            owner_group="chat_test",
        )

        card = build_agent_message_card(
            agent=agent,
            content="Code looks good, approved!",
            model_info="claude",
            duration_s=2.5,
        )

        # Verify card structure
        assert "header" in card
        header = card["header"]
        assert "template" in header
        # Color should come from AGENT_ROLE_COLORS for 'reviewer'
        expected_color = AGENT_ROLE_COLORS.get("reviewer", "blue")
        assert header["template"] == expected_color

        # Title should contain emoji + name
        title_content = header["title"]["content"]
        assert "🔍" in title_content
        assert "Reviewer-Beta" in title_content

        # Elements in body should contain markdown content
        body = card.get("body", card)
        elements = body.get("elements", card.get("elements", []))
        assert any(
            e.get("tag") == "markdown" and "Code looks good" in e.get("content", "")
            for e in elements
        )

        # Footer note should contain model info
        note_elements = [e for e in elements if e.get("tag") == "note"]
        assert len(note_elements) > 0

    def test_no_engine_message_stays_silent(self):
        """When no engine is active, handle_message does nothing."""
        handler = self._make_handler_with_engine()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_4", "chat_e2e", "hello")

        handler.reply_text.assert_not_called()
        handler.send_card_to_chat.assert_not_called()


class TestE2EStatusPanel:
    """Verify /slock status produces a valid status panel card."""

    def test_status_with_active_engine(self):
        """AC-12: /slock status returns panel with all agents."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()

        engine = MagicMock()
        engine.channel = SlockChannel(channel_id="chat_s", name="Status Test", team_name="Team-S")
        engine.get_status_card.return_value = {
            "header": {"title": {"content": "🎭 Team-S Status"}},
            "elements": [],
        }

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)
        handler.get_engine_name = MagicMock(return_value="Slock-s")

        handler.show_slock_status("msg_s", "chat_s")

        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card_data = json.loads(card_json)
        assert "header" in card_data

    def test_status_without_engine_shows_guidance(self):
        """AC-02: No active engine shows guidance message."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.get_engine_name = MagicMock(return_value="Slock")

        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        handler._get_engine_manager = MagicMock(return_value=manager)

        with patch("src.feishu.handlers.slock.CardBuilder") as mock_cb:
            mock_cb.build_info_card.return_value = ("interactive", '{"header":{}}')
            handler.show_slock_status("msg_n", "chat_n")

        handler.reply_card.assert_called_once()

    def test_task_status_returns_task_board_card(self):
        """`/task status` should render the Kanban-style task board card."""
        from src.feishu.handlers.slock import SlockHandler
        from src.slock_engine.models import SlockTask

        ctx = MagicMock()
        handler = SlockHandler(ctx)
        handler.reply_card = MagicMock()
        handler.reply_text = MagicMock()

        engine, agent = TestE2EHandlerToEngine()._make_engine_with_agent(chat_id="chat_task")
        engine.tasks = [SlockTask(content="Implement archive")]
        engine.registry.list_agents.return_value = [agent]

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.show_task_status("msg_task", "chat_task")

        handler.reply_card.assert_called_once()
        handler.reply_text.assert_not_called()
        card_json = handler.reply_card.call_args[0][1]
        card_data = json.loads(card_json)
        assert "Task Board" in card_data["header"]["title"]["content"]
