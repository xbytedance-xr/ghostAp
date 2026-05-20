"""Tests for slock status panel Refresh button (AC-04).

Validates that the Refresh callback triggers update_card with
the latest agent status reflected in the card content.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.slock_engine.models import AgentIdentity, AgentStatus, SlockChannel


class TestSlockStatusRefresh:
    """Verify Refresh button callback updates the status panel card."""

    def _make_handler(self):
        """Create a SlockHandler with mocked context."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.slock_engine_manager = MagicMock()

        handler = SlockHandler(ctx)
        handler.update_card = MagicMock(return_value=True)
        handler.send_text_to_chat = MagicMock()
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine_with_agents(self, statuses: dict[str, AgentStatus]):
        """Create mock engine with agents in specified states."""
        engine = MagicMock()
        channel = SlockChannel(
            channel_id="chat-001",
            name="Test Team",
            team_name="Team Alpha",
        )
        engine.channel = channel

        agents = []
        agent_status_pairs = []
        for name, status in statuses.items():
            agent = AgentIdentity(
                agent_id=f"agent-{name}",
                name=name,
                emoji="🔧",
                agent_type="claude",
                model_name="sonnet-4",
                system_prompt="test",
                role="coder",
            )
            agents.append(agent)
            agent_status_pairs.append((agent, status))

        engine.list_agents_with_status = MagicMock(return_value=agent_status_pairs)
        engine.get_agent_status = MagicMock(side_effect=lambda aid: statuses.get(aid.replace("agent-", ""), AgentStatus.IDLE))

        # get_status_card must return a real dict (json.dumps is called on it)
        def _fake_status_card(**kwargs):
            card = {"header": {"title": {"content": "Slock Status"}}, "elements": []}
            for name, status in statuses.items():
                card["elements"].append({"tag": "div", "text": {"content": f"{name}: {status.value.title()}"}})
            return card
        engine.get_status_card = MagicMock(side_effect=_fake_status_card)

        return engine

    def test_refresh_updates_card_with_latest_status(self):
        """AC-04: Refresh button triggers update_card with current states."""
        handler = self._make_handler()
        engine = self._make_engine_with_agents({
            "Coder-A": AgentStatus.RUNNING,
            "Reviewer-B": AgentStatus.IDLE,
        })
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        # Simulate Refresh button callback
        value = {"action": "slock_refresh_status", "channel_id": "chat-001"}
        handler.handle_card_action("msg-panel-001", "chat-001", "slock_refresh_status", value)

        # update_card should have been called
        handler.update_card.assert_called_once()
        card_json = handler.update_card.call_args[0][1]
        # Card should contain agent names
        assert "Coder-A" in card_json
        assert "Reviewer-B" in card_json

    def test_refresh_reflects_state_change(self):
        """Refresh after state change shows new state."""
        handler = self._make_handler()

        # First state: both IDLE
        engine = self._make_engine_with_agents({
            "Coder-A": AgentStatus.IDLE,
            "Reviewer-B": AgentStatus.IDLE,
        })
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"action": "slock_refresh_status", "channel_id": "chat-001"}
        handler.handle_card_action("msg-001", "chat-001", "slock_refresh_status", value)

        first_card = handler.update_card.call_args[0][1]
        assert "Idle" in first_card

        # Now change state to RUNNING
        handler.update_card.reset_mock()
        engine2 = self._make_engine_with_agents({
            "Coder-A": AgentStatus.RUNNING,
            "Reviewer-B": AgentStatus.IDLE,
        })
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine2)

        handler.handle_card_action("msg-001", "chat-001", "slock_refresh_status", value)

        second_card = handler.update_card.call_args[0][1]
        assert "Running" in second_card

    def test_refresh_no_engine_no_crash(self):
        """Refresh when no engine active should not crash."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        value = {"action": "slock_refresh_status", "channel_id": "chat-001"}
        # Should not raise
        handler.handle_card_action("msg-001", "chat-001", "slock_refresh_status", value)

    def test_refresh_no_engine_sends_feedback(self):
        """Refresh when no engine → send_text_to_chat with '未激活' message."""
        handler = self._make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=None)

        value = {"action": "slock_refresh_status", "channel_id": "chat-001"}
        handler.handle_card_action("msg-001", "chat-001", "slock_refresh_status", value)

        handler.send_text_to_chat.assert_called_once()
        call_text = handler.send_text_to_chat.call_args[0][1]
        assert "未激活" in call_text

    # ------------------------------------------------------------------
    # Stop single agent via card action
    # ------------------------------------------------------------------

    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_stop_single_agent_resets_status_to_idle(self, mock_sender, mock_settings):
        """Stop button callback with agent_id resets that agent's status to IDLE."""
        mock_sender.return_value = "owner-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = self._make_engine_with_agents({
            "Coder-A": AgentStatus.RUNNING,
            "Reviewer-B": AgentStatus.IDLE,
        })
        engine.channel.owner_id = "owner-001"
        engine.stop_agent = MagicMock(return_value=True)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"action": "slock_stop_agent", "channel_id": "chat-001", "agent_id": "agent-Coder-A"}
        handler.handle_card_action("msg-001", "chat-001", "slock_stop_agent", value)

        # stop_agent must be called with the specific agent_id
        engine.stop_agent.assert_called_once_with("agent-Coder-A")
        # Confirmation message sent
        handler.send_text_to_chat.assert_called_once()
        assert "已停止" in handler.send_text_to_chat.call_args[0][1]

    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_stop_agent_card_updated_with_idle_status(self, mock_sender, mock_settings):
        """After stop, update_card should reflect IDLE for the stopped agent."""
        mock_sender.return_value = "owner-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()

        # Build engine whose state will change after stop
        statuses = {"Coder-A": AgentStatus.RUNNING, "Reviewer-B": AgentStatus.IDLE}
        engine = self._make_engine_with_agents(statuses)
        engine.channel.owner_id = "owner-001"

        def fake_stop(agent_id):
            # Simulate engine mutating internal state
            statuses["Coder-A"] = AgentStatus.IDLE
            return True

        engine.stop_agent = MagicMock(side_effect=fake_stop)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        # Trigger the stop action
        value = {"action": "slock_stop_agent", "channel_id": "chat-001", "agent_id": "agent-Coder-A"}
        handler.handle_card_action("msg-001", "chat-001", "slock_stop_agent", value)

        # Now trigger refresh to see the updated status
        handler.update_card.reset_mock()
        value_refresh = {"action": "slock_refresh_status", "channel_id": "chat-001"}
        handler.handle_card_action("msg-panel-001", "chat-001", "slock_refresh_status", value_refresh)

        handler.update_card.assert_called_once()
        card_json = handler.update_card.call_args[0][1]
        # After stop, Coder-A should now show Idle
        assert "Idle" in card_json

    # ------------------------------------------------------------------
    # Refresh reflects state change (RUNNING → IDLE after stop)
    # ------------------------------------------------------------------

    def test_refresh_reflects_running_to_idle_transition(self):
        """After agent transitions RUNNING→IDLE, Refresh shows updated state."""
        handler = self._make_handler()

        # Phase 1: Agent is RUNNING
        engine = self._make_engine_with_agents({
            "Coder-A": AgentStatus.RUNNING,
            "Reviewer-B": AgentStatus.IDLE,
        })
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"action": "slock_refresh_status", "channel_id": "chat-001"}
        handler.handle_card_action("msg-001", "chat-001", "slock_refresh_status", value)

        first_card = handler.update_card.call_args[0][1]
        assert "Running" in first_card

        # Phase 2: Agent stopped externally → now IDLE
        handler.update_card.reset_mock()
        engine2 = self._make_engine_with_agents({
            "Coder-A": AgentStatus.IDLE,
            "Reviewer-B": AgentStatus.IDLE,
        })
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine2)

        handler.handle_card_action("msg-001", "chat-001", "slock_refresh_status", value)

        second_card = handler.update_card.call_args[0][1]
        # Coder-A should no longer show Running
        assert "Running" not in second_card
        # Both should be Idle
        assert "Idle" in second_card
