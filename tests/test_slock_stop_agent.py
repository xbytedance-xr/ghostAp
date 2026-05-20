"""Tests for per-agent Stop button functionality (AC-05).

Validates that stop_agent() cancels only the target agent's session
and resets its status to IDLE without affecting other agents.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.slock_engine.models import AgentStatus


class TestSlockStopAgent:
    """Test stop_agent engine method and handler dispatch."""

    def _make_engine(self):
        """Create a SlockEngine-like mock with real stop_agent logic."""
        from src.slock_engine.engine import SlockEngine

        engine = MagicMock(spec=SlockEngine)
        # We'll test the actual stop_agent method
        engine._agent_statuses = {
            "agent-coder": AgentStatus.RUNNING,
            "agent-reviewer": AgentStatus.THINKING,
            "agent-writer": AgentStatus.IDLE,
        }
        engine._agent_sessions = {}
        engine._lock = __import__("threading").Lock()
        engine.chat_id = "chat-001"

        # Bind real stop_agent method
        engine.stop_agent = SlockEngine.stop_agent.__get__(engine, SlockEngine)
        return engine

    def test_stop_running_agent_resets_to_idle(self):
        """AC-05: Stop a running agent → status becomes IDLE."""
        engine = self._make_engine()
        mock_session = MagicMock()
        engine._agent_sessions["agent-coder"] = mock_session

        result = engine.stop_agent("agent-coder")

        assert result is True
        assert engine._agent_statuses["agent-coder"] == AgentStatus.IDLE
        mock_session.cancel.assert_called_once()

    def test_stop_does_not_affect_other_agents(self):
        """AC-05: Stopping one agent leaves others unchanged."""
        engine = self._make_engine()
        engine._agent_sessions["agent-coder"] = MagicMock()

        engine.stop_agent("agent-coder")

        # Other agents untouched
        assert engine._agent_statuses["agent-reviewer"] == AgentStatus.THINKING
        assert engine._agent_statuses["agent-writer"] == AgentStatus.IDLE

    def test_stop_nonexistent_agent_returns_false(self):
        """Stopping an agent that doesn't exist returns False."""
        engine = self._make_engine()

        result = engine.stop_agent("agent-nonexistent")

        assert result is False

    def test_stop_agent_without_session(self):
        """Stop agent that has status but no active session → still resets to IDLE."""
        engine = self._make_engine()
        # agent-reviewer has THINKING status but no session in _agent_sessions

        result = engine.stop_agent("agent-reviewer")

        assert result is True
        assert engine._agent_statuses["agent-reviewer"] == AgentStatus.IDLE

    def test_stop_agent_session_cancel_exception_handled(self):
        """If session.cancel() raises, it's caught gracefully."""
        engine = self._make_engine()
        mock_session = MagicMock()
        mock_session.cancel.side_effect = RuntimeError("Already cancelled")
        engine._agent_sessions["agent-coder"] = mock_session

        result = engine.stop_agent("agent-coder")

        assert result is True
        assert engine._agent_statuses["agent-coder"] == AgentStatus.IDLE


class TestSlockStopAgentHandler:
    """Test the handler-level dispatch for slock_stop_agent action."""

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
        handler.send_card_to_chat = MagicMock()
        return handler

    def test_stop_agent_action_dispatches_correctly(self):
        """handle_card_action with slock_stop_agent calls stop_agent on engine."""
        handler = self._make_handler()
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.owner_id = "owner-001"
        engine.stop_agent = MagicMock(return_value=True)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        with patch("src.thread.manager.get_current_sender_id", return_value="owner-001"), \
             patch("src.config.get_settings", return_value=MagicMock(admin_user_ids=frozenset({"admin-001"}))):
            value = {"action": "slock_stop_agent", "channel_id": "chat-001", "agent_id": "agent-coder"}
            handler.handle_card_action("msg-001", "chat-001", "slock_stop_agent", value)

        engine.stop_agent.assert_called_once_with("agent-coder")
        handler.send_text_to_chat.assert_called_once()
        assert "已停止" in handler.send_text_to_chat.call_args[0][1]

    def test_stop_agent_no_agent_id_fallback_to_full_stop(self):
        """Missing agent_id in value → falls back to full engine stop."""
        handler = self._make_handler()
        engine = MagicMock()
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)
        handler.stop_slock_engine = MagicMock()

        value = {"action": "slock_stop_agent", "channel_id": "chat-001"}
        handler.handle_card_action("msg-001", "chat-001", "slock_stop_agent", value)

        handler.stop_slock_engine.assert_called_once()

    def test_stop_agent_not_found_feedback(self):
        """Agent not found → user gets warning message."""
        handler = self._make_handler()
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.owner_id = "owner-001"
        engine.stop_agent = MagicMock(return_value=False)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        with patch("src.thread.manager.get_current_sender_id", return_value="owner-001"), \
             patch("src.config.get_settings", return_value=MagicMock(admin_user_ids=frozenset({"admin-001"}))):
            value = {"action": "slock_stop_agent", "channel_id": "chat-001", "agent_id": "agent-unknown"}
            handler.handle_card_action("msg-001", "chat-001", "slock_stop_agent", value)

        handler.send_text_to_chat.assert_called_once()
        assert "未找到" in handler.send_text_to_chat.call_args[0][1]

    # ------------------------------------------------------------------
    # Permission checks for _stop_single_agent
    # ------------------------------------------------------------------

    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_unauthorized_user_stop_rejected(self, mock_sender, mock_settings):
        """Non-admin, non-owner user clicking Stop → permission denied."""
        mock_sender.return_value = "random-user-999"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.owner_id = "owner-001"
        engine.stop_agent = MagicMock(return_value=True)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"action": "slock_stop_agent", "channel_id": "chat-001", "agent_id": "agent-coder"}
        handler._stop_single_agent("msg-001", "chat-001", value)

        # Engine stop_agent should NOT be called
        engine.stop_agent.assert_not_called()
        # Permission denied feedback
        handler.reply_text.assert_called_once()
        assert "权限不足" in handler.reply_text.call_args[0][1]

    @patch("src.config.get_settings")
    @patch("src.thread.manager.get_current_sender_id")
    def test_admin_user_stop_succeeds(self, mock_sender, mock_settings):
        """Admin user clicking Stop → agent stopped successfully."""
        mock_sender.return_value = "admin-001"
        mock_settings.return_value = MagicMock(admin_user_ids=frozenset({"admin-001"}))

        handler = self._make_handler()
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.owner_id = "owner-001"
        engine.stop_agent = MagicMock(return_value=True)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        value = {"action": "slock_stop_agent", "channel_id": "chat-001", "agent_id": "agent-coder"}
        handler._stop_single_agent("msg-001", "chat-001", value)

        engine.stop_agent.assert_called_once_with("agent-coder")
        handler.send_text_to_chat.assert_called_once()
        assert "已停止" in handler.send_text_to_chat.call_args[0][1]
