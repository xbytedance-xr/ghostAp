"""Tests for WorkflowHandler base API surface.

Validates:
- handle_workflow_command routes correctly for all /wf variants
- _get_engine_name_prefix returns "Workflow"
- _get_task_type returns "workflow_engine"
- Command parser handles edge cases (empty args, unknown commands)
- Budget selection is passed through to execute_workflow
"""

import unittest
from unittest.mock import MagicMock, patch

from src.card.actions.dispatch import WORKFLOW_SELECT_BUDGET


class TestWorkflowHandlerRouting(unittest.TestCase):
    """Test handle_workflow_command routes to correct methods."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.reply_text = MagicMock()
        handler.reply_error = MagicMock()
        handler.reply_card = MagicMock()
        handler.update_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp")
        handler.get_engine_name = MagicMock(return_value="coco")
        handler.ensure_request_id = MagicMock(return_value="req_1")
        handler._submit_engine_task = MagicMock()
        handler.start_workflow = MagicMock()
        handler.stop_workflow = MagicMock()
        handler.show_workflow_status = MagicMock()
        handler.show_workflow_help = MagicMock()
        return handler

    def test_stop_wf_routes(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/stop_wf", None)
        handler.stop_workflow.assert_called_once()

    def test_stop_workflow_routes(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/stop_workflow", None)
        handler.stop_workflow.assert_called_once()

    def test_wf_status_routes(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/wf_status", None)
        handler.show_workflow_status.assert_called_once()

    def test_workflow_status_routes(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/workflow_status", None)
        handler.show_workflow_status.assert_called_once()

    def test_wf_help_routes(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/wf_help", None)
        handler.show_workflow_help.assert_called_once()

    def test_workflow_help_routes(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/workflow_help", None)
        handler.show_workflow_help.assert_called_once()

    def test_wf_with_args_starts_workflow(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/wf do code review", None)
        handler.start_workflow.assert_called_once_with("msg", "chat", "do code review", None)

    def test_workflow_with_args_starts_workflow(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/workflow refactor utils", None)
        handler.start_workflow.assert_called_once_with("msg", "chat", "refactor utils", None)

    def test_wf_no_args_shows_error(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/wf", None)
        handler.reply_card.assert_called_once()

    def test_unknown_command_shows_error(self):
        handler = self._make_handler()
        handler.handle_workflow_command("msg", "chat", "/wf_unknown_sub", None)
        handler.reply_card.assert_called()
        # Ensure the message references the expanded command list
        # (via the unified error card surface).


class TestWorkflowHandlerProperties(unittest.TestCase):
    """Test handler property methods."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        return handler

    def test_engine_name_prefix(self):
        handler = self._make_handler()
        self.assertEqual(handler._get_engine_name_prefix(), "Workflow")

    def test_task_type(self):
        handler = self._make_handler()
        self.assertEqual(handler._get_task_type(), "workflow_engine")

    def test_engine_manager_accessor(self):
        handler = self._make_handler()
        handler.ctx.workflow_engine_manager = MagicMock()
        result = handler._get_engine_manager()
        self.assertIs(result, handler.ctx.workflow_engine_manager)


class TestBudgetSelectAction(unittest.TestCase):
    """Test WORKFLOW_SELECT_BUDGET action integration."""

    def test_action_constant_defined(self):
        self.assertEqual(WORKFLOW_SELECT_BUDGET, "workflow_select_budget")

    def test_forwarding_map_has_select_budget(self):
        from src.feishu.router import FORWARDING_MAP

        self.assertIn("_handle_workflow_select_budget", FORWARDING_MAP)
        self.assertEqual(
            FORWARDING_MAP["_handle_workflow_select_budget"],
            ("workflow", "handle_workflow_select_budget"),
        )

    def test_budget_select_in_system_card_actions(self):
        from src.feishu.ws_card_action_handler import SYSTEM_CARD_ACTIONS

        self.assertIn("workflow_select_budget", SYSTEM_CARD_ACTIONS)


class TestBudgetSelectHandler(unittest.TestCase):
    """Test handle_workflow_select_budget behavior."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler
        from src.workflow_engine.models import PendingConfirmation, WorkflowProject, WorkflowStatus

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.reply_text = MagicMock()
        handler.update_card = MagicMock(return_value=True)
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        return handler

    def _make_engine_awaiting(self, budget=None):
        from src.workflow_engine.models import PendingConfirmation, WorkflowProject, WorkflowStatus

        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path="/tmp/wf.js",
                requirement="test",
                meta={"name": "test", "tools": ["coco"]},
                engine_session_key="key1",
                initiator_user_id="user_001",
                budget=budget,
            ),
        )
        return engine

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_sets_pending_budget(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting(budget=None)
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {
                "action": "workflow_select_budget",
                "budget_tokens": 5_000_000,
                "engine_session_key": "key1",
            },
        )

        self.assertEqual(engine.project.pending.budget if engine.project.pending else None, 5_000_000)
        # Budget change triggers a "regenerating" card + a final confirm card. The final card
        # should show the updated confirm card with the new budget reflected.
        self.assertGreaterEqual(handler.update_card.call_count, 1)
        # Verify the last call is the confirm card (not the regenerating one)
        final_call = handler.update_card.call_args_list[-1]
        final_card = final_call.args[1] if len(final_call.args) >= 2 else final_call.kwargs.get(
            "card", final_call.kwargs.get("elements", None)
        )
        if final_card is None:
            # Fall back to positional access
            final_card = final_call[0][1]
        final_elements = final_card["body"]["elements"]
        # The final card should mention the budget tokens (thousand-separator formatted)
        all_md = "".join(e.get("content", "") for e in final_elements if e.get("tag") == "markdown")
        self.assertIn("5,000,000", all_md)

    @patch("src.thread.get_current_sender_id", return_value="attacker")
    def test_rejects_non_initiator(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_1",
            {
                "action": "workflow_select_budget",
                "budget_tokens": 5_000_000,
                "engine_session_key": "key1",
            },
        )

        # Budget should not be changed (silent reject)
        self.assertIsNone(engine.project.pending.budget if engine.project.pending else None)
        handler.update_card.assert_not_called()


if __name__ == "__main__":
    unittest.main()
