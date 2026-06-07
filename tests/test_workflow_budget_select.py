"""Tests for Workflow budget selection.

Validates:
- Budget selection sets pending_budget and re-renders confirm card
- Session key mismatch → silent reject
- Non-initiator → silent reject
- Wrong status (not AWAITING_CONFIRM) → silent reject
- Invalid budget values (non-int, zero, negative) → silent reject
- WORKFLOW_SELECT_BUDGET action constant is defined
- FORWARDING_MAP entry exists
"""

import unittest
from unittest.mock import MagicMock, patch

from src.card.actions.dispatch import WORKFLOW_SELECT_BUDGET
from src.workflow_engine.constants import BUDGET_OPTIONS, DEFAULT_BUDGET_TOKENS
from src.workflow_engine.models import PendingConfirmation, WorkflowProject, WorkflowStatus


class TestWorkflowBudgetSelectAction(unittest.TestCase):
    """Test WORKFLOW_SELECT_BUDGET action constant."""

    def test_action_constant_defined(self):
        self.assertEqual(WORKFLOW_SELECT_BUDGET, "workflow_select_budget")

    def test_forwarding_map_has_select_budget(self):
        from src.feishu.router import FORWARDING_MAP

        self.assertIn("_handle_workflow_select_budget", FORWARDING_MAP)
        self.assertEqual(
            FORWARDING_MAP["_handle_workflow_select_budget"],
            ("workflow", "handle_workflow_select_budget"),
        )


class TestWorkflowBudgetSelectHandler(unittest.TestCase):
    """Test handle_workflow_select_budget behavior."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.update_card = MagicMock(return_value=True)
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        return handler

    def _make_engine_awaiting(self, budget=None, meta_budget=None):
        engine = MagicMock()
        meta = {"name": "test", "tools": ["coco", "claude"]}
        if meta_budget is not None:
            meta["budget_tokens"] = meta_budget
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path="/tmp/wf.js",
                requirement="test task",
                meta=meta,
                is_fallback=False,
                engine_session_key="key1",
                initiator_user_id="user_001",
                selected_tools=["coco", "claude"],
                budget=budget,
            ),
        )
        return engine

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_valid_budget_selection(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        # Budget selection is selection-only: updates pending.budget and re-renders
        # the confirm card, but does NOT re-invoke the AI. Regeneration is gated
        # on the explicit "apply budget and regenerate" button.
        with patch.object(handler, '_generate_script_via_ai') as mock_gen:
            handler.handle_workflow_select_budget(
                "msg_1", "chat_1", "proj_1",
                {"action": "workflow_select_budget", "budget_tokens": 5_000_000, "engine_session_key": "key1"},
            )

        self.assertEqual(engine.project.pending.budget if engine.project.pending else None, 5_000_000)
        # Selection-only flow: one card re-render to reflect the new budget choice
        self.assertEqual(handler.update_card.call_count, 1)
        # Script generation must NOT be called on plain selection
        mock_gen.assert_not_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_minimum_budget_option(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        min_budget = BUDGET_OPTIONS[0][1]  # 500_000

        # Selection-only: no AI call
        with patch.object(handler, '_generate_script_via_ai') as mock_gen:
            handler.handle_workflow_select_budget(
                "msg_1", "chat_1", "proj_1",
                {"action": "workflow_select_budget", "budget_tokens": min_budget, "engine_session_key": "key1"},
            )

        self.assertEqual(engine.project.pending.budget if engine.project.pending else None, min_budget)
        self.assertEqual(handler.update_card.call_count, 1)
        mock_gen.assert_not_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_maximum_budget_option(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        max_budget = BUDGET_OPTIONS[-1][1]  # 5_000_000

        # Selection-only: no AI call
        with patch.object(handler, '_generate_script_via_ai') as mock_gen:
            handler.handle_workflow_select_budget(
                "msg_1", "chat_1", "proj_1",
                {"action": "workflow_select_budget", "budget_tokens": max_budget, "engine_session_key": "key1"},
            )

        self.assertEqual(engine.project.pending.budget if engine.project.pending else None, max_budget)
        self.assertEqual(handler.update_card.call_count, 1)
        mock_gen.assert_not_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_wrong_session_key_silent_reject(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 1_500_000, "engine_session_key": "wrong_key"},
        )

        self.assertIsNone(engine.project.pending.budget if engine.project.pending else None)
        handler.update_card.assert_not_called()

    @patch("src.thread.get_current_sender_id", return_value="other_user")
    def test_non_initiator_silent_reject(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 1_500_000, "engine_session_key": "key1"},
        )

        self.assertIsNone(engine.project.pending.budget if engine.project.pending else None)
        handler.update_card.assert_not_called()

    def test_wrong_status_silent_reject(self):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        engine.project.status = WorkflowStatus.RUNNING
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 1_500_000, "engine_session_key": "key1"},
        )

        handler.update_card.assert_not_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_invalid_budget_non_int(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": "1500000", "engine_session_key": "key1"},
        )

        self.assertIsNone(engine.project.pending.budget if engine.project.pending else None)
        handler.update_card.assert_not_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_invalid_budget_zero(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 0, "engine_session_key": "key1"},
        )

        self.assertIsNone(engine.project.pending.budget if engine.project.pending else None)
        handler.update_card.assert_not_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_invalid_budget_negative(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": -100, "engine_session_key": "key1"},
        )

        self.assertIsNone(engine.project.pending.budget if engine.project.pending else None)
        handler.update_card.assert_not_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_invalid_budget_none(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": None, "engine_session_key": "key1"},
        )

        self.assertIsNone(engine.project.pending.budget if engine.project.pending else None)
        handler.update_card.assert_not_called()

    def test_no_engine_silent_reject(self):
        handler = self._make_handler()
        handler.ctx.workflow_engine_manager.get.return_value = None

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 2_000_000, "engine_session_key": "key1"},
        )

        handler.update_card.assert_not_called()

    # --- Tiered budget validation tests (post-Task 5 tightening) ---

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_valid_tier_500k_accepted(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 500_000, "engine_session_key": "key1"},
        )

        self.assertEqual(engine.project.pending.selected_budget, 500_000)
        self.assertEqual(engine.project.pending.budget, 500_000)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_valid_tier_1_5m_accepted(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 1_500_000, "engine_session_key": "key1"},
        )

        self.assertEqual(engine.project.pending.selected_budget, 1_500_000)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_valid_tier_2m_accepted(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 2_000_000, "engine_session_key": "key1"},
        )

        self.assertEqual(engine.project.pending.selected_budget, 2_000_000)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_valid_tier_5m_accepted(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 5_000_000, "engine_session_key": "key1"},
        )

        self.assertEqual(engine.project.pending.selected_budget, 5_000_000)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_invalid_arbitrary_value_rejected(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 1_234_567, "engine_session_key": "key1"},
        )

        # Must not write the invalid value to pending state
        self.assertIsNone(engine.project.pending.selected_budget)
        self.assertIsNone(engine.project.pending.budget)
        handler.update_card.assert_not_called()
        # Must send an error card via reply_card
        handler.reply_card.assert_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_invalid_out_of_range_rejected(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 99_999_999, "engine_session_key": "key1"},
        )

        self.assertIsNone(engine.project.pending.selected_budget)
        self.assertIsNone(engine.project.pending.budget)
        handler.reply_card.assert_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_invalid_string_rejected_not_cast(self, mock_sender):
        """A string that looks numeric must NOT be silently cast."""
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": "2000000", "engine_session_key": "key1"},
        )

        self.assertIsNone(engine.project.pending.selected_budget)
        self.assertIsNone(engine.project.pending.budget)
        handler.update_card.assert_not_called()
        handler.reply_card.assert_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_invalid_float_rejected(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_budget", "budget_tokens": 2_000_000.5, "engine_session_key": "key1"},
        )

        self.assertIsNone(engine.project.pending.selected_budget)
        handler.reply_card.assert_called()


if __name__ == "__main__":
    unittest.main()
