"""Tests for /plan command handler card paths."""
from __future__ import annotations

import json
from unittest.mock import MagicMock


class TestPlanListPaths:
    """Verify /plan list returns correct card types for each state."""

    def _make_handler(self):
        """Create a minimal slock handler with mocked dependencies."""
        from src.feishu.handlers.slock import SlockHandler

        handler = SlockHandler.__new__(SlockHandler)
        handler._client = MagicMock()
        handler.reply_card = MagicMock(return_value="msg_new_123")
        handler.reply_text = MagicMock()
        handler._get_engine_manager = MagicMock()
        return handler

    def test_list_plans_no_engine_returns_error_card(self):
        """No active engine → error state card."""
        handler = self._make_handler()
        manager = handler._get_engine_manager.return_value
        manager.get_activated_engine.return_value = None

        handler.list_plans("msg_1", "chat_1")

        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        # Error card has red header
        assert card["header"]["template"] == "red"
        handler.reply_text.assert_not_called()

    def test_list_plans_empty_returns_empty_state_card(self):
        """Active engine with no plans → empty state card."""
        handler = self._make_handler()
        manager = handler._get_engine_manager.return_value
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "ch_1"
        engine.collaboration_orchestrator.list_active_plans.return_value = []
        engine.registry.list_agents.return_value = []
        manager.get_activated_engine.return_value = engine

        handler.list_plans("msg_1", "chat_1")

        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        # Empty state card has grey header
        assert card["header"]["template"] == "grey"
        handler.reply_text.assert_not_called()


class TestPlanDetailPaths:
    """Verify /plan <plan_id> returns correct card types for each state."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler

        handler = SlockHandler.__new__(SlockHandler)
        handler._client = MagicMock()
        handler.reply_card = MagicMock(return_value="msg_new_123")
        handler.reply_text = MagicMock()
        handler._get_engine_manager = MagicMock()
        return handler

    def test_show_plan_no_plan_id_returns_usage_hint(self):
        """/plan with no plan_id → usage hint card."""
        handler = self._make_handler()

        handler.show_plan_detail("msg_1", "chat_1", plan_id="")

        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        # Usage hint card has grey header with "💡 命令提示" title
        assert card["header"]["template"] == "grey"
        assert "命令提示" in card["header"]["title"]["content"]
        handler.reply_text.assert_not_called()

    def test_show_plan_not_found_returns_empty_state(self):
        """/plan <unknown_id> → empty state card."""
        handler = self._make_handler()
        manager = handler._get_engine_manager.return_value
        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "ch_1"
        engine.collaboration_orchestrator.get_plan.return_value = None
        manager.get_activated_engine.return_value = engine

        handler.show_plan_detail("msg_1", "chat_1", plan_id="plan_xyz_123456")

        handler.reply_card.assert_called_once()
        card_json = handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        # Empty state card has grey header
        assert card["header"]["template"] == "grey"
        handler.reply_text.assert_not_called()
