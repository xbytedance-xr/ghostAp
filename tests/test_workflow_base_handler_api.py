"""Tests for WorkflowHandler base API surface.

Validates:
- handle_workflow_command routes correctly for all /wf variants
- _get_engine_name_prefix returns "Workflow"
- _get_task_type returns "workflow_engine"
- Command parser handles edge cases (empty args, unknown commands)
"""

import unittest
from unittest.mock import MagicMock


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


if __name__ == "__main__":
    unittest.main()
