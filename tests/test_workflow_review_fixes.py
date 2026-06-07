"""Tests for review-requested fixes to Workflow mode.

Covers:
- MAX_NESTING_DEPTH increased to 3 (constants)
- BUDGET_OPTIONS includes 200万 tier (constants)
- TOOL_DESCRIPTIONS dict defined (constants)
- initiator_user_id validation in stop_workflow()
- project_id routing in cancel/select_tool/select_budget handlers
- TOOL_DESCRIPTIONS shown in confirm card
- Path traversal protection in bridge.py
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from src.workflow_engine.constants import (
    BUDGET_OPTIONS,
    MAX_NESTING_DEPTH,
    TOOL_DESCRIPTIONS,
)
from src.workflow_engine.models import WorkflowProject, WorkflowStatus


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstantsReviewFixes(unittest.TestCase):
    """Validate constants changes from review feedback."""

    def test_max_nesting_depth_is_3(self):
        self.assertEqual(MAX_NESTING_DEPTH, 3)

    def test_budget_options_has_200wan(self):
        labels = [label for label, _ in BUDGET_OPTIONS]
        self.assertIn("200万", labels)

    def test_budget_options_200wan_value(self):
        for label, value in BUDGET_OPTIONS:
            if label == "200万":
                self.assertEqual(value, 2_000_000)
                return
        self.fail("200万 not found in BUDGET_OPTIONS")

    def test_tool_descriptions_keys(self):
        # TOOL_DESCRIPTIONS (deprecated) now includes all known tools including gemini/ttadk
        expected_tools = {"coco", "aiden", "codex", "claude", "traex", "gemini", "ttadk"}
        self.assertEqual(set(TOOL_DESCRIPTIONS.keys()), expected_tools)

    def test_tool_descriptions_non_empty(self):
        for tool, desc in TOOL_DESCRIPTIONS.items():
            self.assertTrue(len(desc) > 0, f"{tool} has empty description")


# ---------------------------------------------------------------------------
# stop_workflow auth tests
# ---------------------------------------------------------------------------


class TestStopWorkflowAuth(unittest.TestCase):
    """Test initiator_user_id validation in stop_workflow."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.ctx.settings = MagicMock()
        handler.ctx.settings.admin_user_ids = ["admin_001"]
        handler.reply_text = MagicMock()
        handler._reply_workflow_error = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        return handler

    def _make_running_engine(self, initiator="user_123"):
        engine = MagicMock()
        engine.is_running = True
        engine.project = MagicMock()
        engine.project.initiator_user_id = initiator
        engine.stop = MagicMock()
        return engine

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_initiator_can_stop(self, _):
        handler = self._make_handler()
        engine = self._make_running_engine(initiator="user_123")
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.stop_workflow("msg_1", "chat_1")

        engine.stop.assert_called_once()
        handler.reply_text.assert_called_once()
        self.assertIn("已停止", handler.reply_text.call_args[0][1])

    @patch("src.thread.get_current_sender_id", return_value="admin_001")
    def test_admin_can_stop(self, _):
        handler = self._make_handler()
        engine = self._make_running_engine(initiator="user_123")
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.stop_workflow("msg_1", "chat_1")

        engine.stop.assert_called_once()

    @patch("src.thread.get_current_sender_id", return_value="attacker_456")
    def test_non_initiator_non_admin_blocked(self, _):
        handler = self._make_handler()
        engine = self._make_running_engine(initiator="user_123")
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.stop_workflow("msg_1", "chat_1")

        engine.stop.assert_not_called()
        handler._reply_workflow_error.assert_called_once()

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_no_running_engine_replies_not_found(self, _):
        handler = self._make_handler()
        engine = MagicMock()
        engine.is_running = False
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.stop_workflow("msg_1", "chat_1")

        handler._reply_workflow_error.assert_called_once()


# ---------------------------------------------------------------------------
# project_id routing tests
# ---------------------------------------------------------------------------


class TestProjectIdRouting(unittest.TestCase):
    """Test that cancel/select_tool/select_budget use project_id for routing."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.reply_text = MagicMock()
        handler.update_card = MagicMock(return_value=True)
        handler.get_working_dir = MagicMock(return_value="/tmp/default")
        return handler

    def _make_engine_awaiting(self):
        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending_script_path="/tmp/wf.js",
            pending_requirement="test req",
            pending_meta={"name": "test", "tools": ["coco"]},
            pending_is_fallback=False,
            pending_engine_session_key="key1",
            pending_initiator_user_id="user_001",
        )
        return engine

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_cancel_uses_project_id_routing(self, _):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        # Mock _resolve_project_from_id
        mock_project = MagicMock()
        mock_project.root_path = "/home/user/proj_abc"
        handler._resolve_project_from_id = MagicMock(return_value=mock_project)
        handler._get_root_path = MagicMock(return_value="/home/user/proj_abc")

        handler.handle_workflow_cancel(
            "msg_1", "chat_1", "proj_abc",
            {"engine_session_key": "key1"},
        )

        handler._resolve_project_from_id.assert_called_once_with("proj_abc", "chat_1")
        handler._get_root_path.assert_called_once_with("chat_1", mock_project)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_select_tool_uses_project_id_routing(self, _):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        mock_project = MagicMock()
        mock_project.root_path = "/home/user/proj_xyz"
        handler._resolve_project_from_id = MagicMock(return_value=mock_project)
        handler._get_root_path = MagicMock(return_value="/home/user/proj_xyz")
        handler._read_pending_script = MagicMock(return_value="")
        handler._build_confirm_card = MagicMock(return_value={})

        handler.handle_workflow_select_tool(
            "msg_1", "chat_1", "proj_xyz",
            {"engine_session_key": "key1", "tool_name": "claude"},
        )

        handler._resolve_project_from_id.assert_called_once_with("proj_xyz", "chat_1")

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_select_budget_uses_project_id_routing(self, _):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        handler.ctx.workflow_engine_manager.get.return_value = engine

        mock_project = MagicMock()
        mock_project.root_path = "/home/user/proj_xyz"
        handler._resolve_project_from_id = MagicMock(return_value=mock_project)
        handler._get_root_path = MagicMock(return_value="/home/user/proj_xyz")
        handler._read_pending_script = MagicMock(return_value="")
        handler._build_confirm_card = MagicMock(return_value={})

        handler.handle_workflow_select_budget(
            "msg_1", "chat_1", "proj_xyz",
            {"engine_session_key": "key1", "budget_tokens": 2_000_000},
        )

        handler._resolve_project_from_id.assert_called_once_with("proj_xyz", "chat_1")


# ---------------------------------------------------------------------------
# Confirm card TOOL_DESCRIPTIONS tests
# ---------------------------------------------------------------------------


class TestConfirmCardToolDescriptions(unittest.TestCase):
    """Test that _build_confirm_card includes TOOL_DESCRIPTIONS."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        return handler

    def test_confirm_card_contains_tool_descriptions(self):
        handler = self._make_handler()

        # Mock the tool registry to return known descriptions
        mock_tools = {"coco": "全栈编程·支持 subagent", "claude": "Anthropic 深度推理"}
        with patch("src.workflow_engine.tool_registry.get_available_tools", return_value=mock_tools):
            card = handler._build_confirm_card(
                meta={"name": "test-wf", "description": "Test", "phases": [], "tools": ["coco", "claude"]},
                requirement="Test requirement",
                engine_session_key="key1",
                chat_id="chat_1",
                project_id="proj_1",
            )

        # Serialize card to find tool selection in UI
        import json
        card_str = json.dumps(card, ensure_ascii=False)

        # Check that tool names appear in the selection UI
        self.assertIn("coco", card_str)
        self.assertIn("claude", card_str)
        # With tool-selection-first flow, confirm card shows "允许执行的工具"
        # (not "推荐工具" which is now in the tool selection card)
        self.assertIn("允许执行的工具", card_str)
        # Also verify script planned tools section is present
        self.assertIn("脚本计划使用", card_str)

    def test_confirm_card_has_budget_200wan_option(self):
        handler = self._make_handler()
        card = handler._build_confirm_card(
            meta={"name": "test-wf", "description": "Test", "phases": [], "tools": ["coco"]},
            requirement="Test requirement",
            engine_session_key="key1",
            chat_id="chat_1",
            project_id="proj_1",
        )

        import json
        card_str = json.dumps(card, ensure_ascii=False)

        # Check that 200万 budget option exists
        self.assertIn("200万", card_str)
        self.assertIn("2000000", card_str)


# ---------------------------------------------------------------------------
# Path traversal protection tests
# ---------------------------------------------------------------------------


class TestBridgePathTraversal(unittest.TestCase):
    """Test path traversal protection in RuntimeBridge._handle_workflow_call."""

    def _make_bridge(self, cwd="/tmp/project"):
        from src.workflow_engine.bridge import RuntimeBridge

        bridge = RuntimeBridge.__new__(RuntimeBridge)
        bridge._cwd = cwd
        bridge._nesting_depth = 0
        bridge._max_concurrent = 4
        bridge._budget_total = 500000
        bridge._on_agent_call = MagicMock()
        bridge._on_phase = MagicMock()
        bridge._on_log = MagicMock()
        bridge._cancel_event = MagicMock()
        bridge._cancel_event.is_set.return_value = False
        bridge._allowed_tools = None
        bridge._workflow_executor = MagicMock()
        bridge._futures_lock = MagicMock()
        bridge._active_futures = set()
        bridge._pending_submit_count = 0
        bridge._send_error_response = MagicMock()
        bridge._send_response = MagicMock()
        return bridge

    def test_path_traversal_blocked(self):
        """Script path escaping project dir should be rejected."""
        bridge = self._make_bridge(cwd="/tmp/project")

        bridge._handle_workflow_call(
            {"script_path": "../../etc/passwd"},
            request_id="req_1",
        )

        bridge._send_error_response.assert_called_once()
        call_kwargs = bridge._send_error_response.call_args[1]
        self.assertIn("forbidden", call_kwargs.get("message", ""))

    def test_valid_path_within_project_allowed(self):
        """Raw script_path is always rejected (even inside project); named templates are required."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            bridge = self._make_bridge(cwd=tmpdir)
            script_path = os.path.join(tmpdir, "sub", "workflow.js")
            os.makedirs(os.path.join(tmpdir, "sub"), exist_ok=True)
            with open(script_path, "w") as f:
                f.write("// test")

            bridge._handle_workflow_call(
                {"script_path": "sub/workflow.js"},
                request_id="req_2",
            )

            # script_path is universally rejected now, so an error response is always sent.
            bridge._send_error_response.assert_called_once()
            call_kwargs = bridge._send_error_response.call_args[1]
            self.assertIn("forbidden", call_kwargs.get("message", ""))


# ---------------------------------------------------------------------------
# Nesting depth validation
# ---------------------------------------------------------------------------


class TestNestingDepthLimit(unittest.TestCase):
    """Test that nesting at MAX_NESTING_DEPTH is rejected."""

    def _make_bridge(self, nesting_depth=0):
        from src.workflow_engine.bridge import RuntimeBridge

        bridge = RuntimeBridge.__new__(RuntimeBridge)
        bridge._cwd = "/tmp/project"
        bridge._nesting_depth = nesting_depth
        bridge._send_error_response = MagicMock()
        return bridge

    def test_at_max_depth_rejected(self):
        bridge = self._make_bridge(nesting_depth=MAX_NESTING_DEPTH)

        bridge._handle_workflow_call(
            {"script_path": "test.js"},
            request_id="req_1",
        )

        bridge._send_error_response.assert_called_once()
        msg = bridge._send_error_response.call_args[1].get("message", "")
        self.assertIn("nesting depth exceeded", msg)

    def test_below_max_depth_proceeds(self):
        """Depth < MAX should not be immediately rejected for nesting."""
        bridge = self._make_bridge(nesting_depth=MAX_NESTING_DEPTH - 1)
        bridge._workflow_executor = MagicMock()
        bridge._futures_lock = MagicMock()
        bridge._active_futures = set()
        bridge._max_concurrent = 4
        bridge._budget_total = 500000
        bridge._on_agent_call = MagicMock()
        bridge._on_phase = MagicMock()
        bridge._on_log = MagicMock()
        bridge._cancel_event = MagicMock()
        bridge._cancel_event.is_set.return_value = False
        bridge._allowed_tools = None
        bridge._send_response = MagicMock()

        bridge._handle_workflow_call(
            {"script_path": "/tmp/project/test.js"},
            request_id="req_2",
        )

        # Should not be rejected for nesting depth
        for call in bridge._send_error_response.call_args_list:
            msg = call[1].get("message", "") if call[1] else ""
            self.assertNotIn("nesting depth", msg)


# ---------------------------------------------------------------------------
# Empty /wf args shows help hint
# ---------------------------------------------------------------------------


class TestWfEmptyArgsShowsHelpHint(unittest.TestCase):
    """Test that /wf with no arguments replies with an entry card containing help."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.reply_error = MagicMock()
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        return handler

    def test_wf_empty_args_shows_help_hint(self):
        handler = self._make_handler()

        handler.handle_workflow_command("msg_1", "chat_1", "/wf")

        handler.reply_card.assert_called_once()
        # The entry card should contain help references including /wf_help
        call_args = handler.reply_card.call_args[0]
        card_content = str(call_args[1]) if len(call_args) > 1 else str(call_args[0])
        self.assertIn("/wf_help", card_content)


if __name__ == "__main__":
    unittest.main()
