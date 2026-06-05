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

        # Mock script generation to avoid actual AI calls
        with patch.object(handler, '_generate_script_via_ai') as mock_gen:
            mock_gen.return_value = ("/tmp/new_script.js", {"tools": ["coco"], "budget_tokens": 5_000_000}, False)

            handler.handle_workflow_select_budget(
                "msg_1", "chat_1", "proj_1",
                {"action": "workflow_select_budget", "budget_tokens": 5_000_000, "engine_session_key": "key1"},
            )

        self.assertEqual(engine.project.pending.budget if engine.project.pending else None, 5_000_000)
        # update_card called twice: once for "regenerating" card, once for final confirm card
        self.assertEqual(handler.update_card.call_count, 2)
        # Verify script generation was called with the new budget
        mock_gen.assert_called_once()
        call_kwargs = mock_gen.call_args.kwargs
        self.assertEqual(call_kwargs.get("override_budget_tokens"), 5_000_000)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_minimum_budget_option(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        min_budget = BUDGET_OPTIONS[0][1]  # 500_000

        # Mock script generation to avoid actual AI calls
        with patch.object(handler, '_generate_script_via_ai') as mock_gen:
            mock_gen.return_value = ("/tmp/new_script.js", {"tools": ["coco"], "budget_tokens": min_budget}, False)

            handler.handle_workflow_select_budget(
                "msg_1", "chat_1", "proj_1",
                {"action": "workflow_select_budget", "budget_tokens": min_budget, "engine_session_key": "key1"},
            )

        self.assertEqual(engine.project.pending.budget if engine.project.pending else None, min_budget)
        # update_card called twice: once for "regenerating" card, once for final confirm card
        self.assertEqual(handler.update_card.call_count, 2)
        # Verify script generation was called with the new budget
        mock_gen.assert_called_once()
        call_kwargs = mock_gen.call_args.kwargs
        self.assertEqual(call_kwargs.get("override_budget_tokens"), min_budget)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_maximum_budget_option(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        max_budget = BUDGET_OPTIONS[-1][1]  # 5_000_000

        # Mock script generation to avoid actual AI calls
        with patch.object(handler, '_generate_script_via_ai') as mock_gen:
            mock_gen.return_value = ("/tmp/new_script.js", {"tools": ["coco"], "budget_tokens": max_budget}, False)

            handler.handle_workflow_select_budget(
                "msg_1", "chat_1", "proj_1",
                {"action": "workflow_select_budget", "budget_tokens": max_budget, "engine_session_key": "key1"},
            )

        self.assertEqual(engine.project.pending.budget if engine.project.pending else None, max_budget)
        # update_card called twice: once for "regenerating" card, once for final confirm card
        self.assertEqual(handler.update_card.call_count, 2)
        # Verify script generation was called with the new budget
        mock_gen.assert_called_once()
        call_kwargs = mock_gen.call_args.kwargs
        self.assertEqual(call_kwargs.get("override_budget_tokens"), max_budget)

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


if __name__ == "__main__":
    unittest.main()
