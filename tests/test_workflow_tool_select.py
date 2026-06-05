"""Tests for Workflow tool selection (Task 8/15).

Validates:
- Tool toggle adds/removes from pending.selected_tools
- At least one tool must remain selected
- Card is re-rendered with updated tool state
- WORKFLOW_SELECT_TOOL action constant is defined
"""

import unittest
from unittest.mock import MagicMock, patch

from src.card.actions.dispatch import WORKFLOW_SELECT_TOOL
from src.workflow_engine.models import WorkflowProject, WorkflowStatus


class TestWorkflowToolSelectAction(unittest.TestCase):
    """Test WORKFLOW_SELECT_TOOL action constant."""

    def test_action_constant_defined(self):
        self.assertEqual(WORKFLOW_SELECT_TOOL, "workflow_select_tool")

    def test_forwarding_map_has_select_tool(self):
        from src.feishu.router import FORWARDING_MAP

        self.assertIn("_handle_workflow_select_tool", FORWARDING_MAP)
        self.assertEqual(
            FORWARDING_MAP["_handle_workflow_select_tool"],
            ("workflow", "handle_workflow_select_tool"),
        )


class TestWorkflowToolSelectHandler(unittest.TestCase):
    """Test handle_workflow_select_tool behavior."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.reply_text = MagicMock()
        handler.update_card = MagicMock(return_value=True)
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        return handler

    def _make_engine_awaiting(self, tools=None):
        engine = MagicMock()
        engine.project = WorkflowProject.from_dict({
            "status": WorkflowStatus.AWAITING_CONFIRM,
            "pending_script_path": "/tmp/wf.js",
            "pending_requirement": "test",
            "pending_meta": {"name": "test", "tools": ["coco", "claude"]},
            "pending_is_fallback": False,
            "pending_engine_session_key": "key1",
            "pending_initiator_user_id": "user_001",
            "pending_selected_tools": tools,
        })
        return engine

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_toggle_removes_tool(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting(tools=["coco", "claude"])
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_tool(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_tool", "tool_name": "claude", "engine_session_key": "key1"},
        )

        self.assertNotIn("claude", engine.project.pending.selected_tools)
        self.assertIn("coco", engine.project.pending.selected_tools)
        handler.update_card.assert_called_once()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_toggle_adds_tool(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting(tools=["coco"])
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_tool(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_tool", "tool_name": "claude", "engine_session_key": "key1"},
        )

        self.assertIn("claude", engine.project.pending.selected_tools)
        self.assertIn("coco", engine.project.pending.selected_tools)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_cannot_deselect_last_tool(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting(tools=["coco"])
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_tool(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_tool", "tool_name": "coco", "engine_session_key": "key1"},
        )

        # Should still have coco (re-added as minimum)
        self.assertIn("coco", engine.project.pending.selected_tools)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_initializes_from_meta_if_none(self, mock_sender):
        handler = self._make_handler()
        engine = self._make_engine_awaiting(tools=None)  # Not yet initialized
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_tool(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_tool", "tool_name": "claude", "engine_session_key": "key1"},
        )

        # Should have initialized from meta then toggled claude off
        self.assertIn("coco", engine.project.pending.selected_tools)
        self.assertNotIn("claude", engine.project.pending.selected_tools)

    def test_wrong_status_is_noop(self):
        handler = self._make_handler()
        engine = self._make_engine_awaiting()
        engine.project.status = WorkflowStatus.RUNNING
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_tool(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_tool", "tool_name": "claude"},
        )

        # Should not update card
        handler.update_card.assert_not_called()


class TestWorkflowAwaitingToolSelectState(unittest.TestCase):
    """Tests for AWAITING_TOOL_SELECT state handling."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.reply_text = MagicMock()
        handler.update_card = MagicMock(return_value=True)
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler.send_card_to_chat = MagicMock()
        return handler

    def _make_engine_awaiting_tool_select(self, tools=None):
        engine = MagicMock()
        engine.project = WorkflowProject.from_dict({
            "status": WorkflowStatus.AWAITING_TOOL_SELECT,
            "pending_script_path": None,
            "pending_requirement": "test requirement",
            "pending_meta": None,
            "pending_is_fallback": False,
            "pending_engine_session_key": "key1",
            "pending_initiator_user_id": "user_001",
            "pending_selected_tools": tools,
        })
        return engine

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_select_tool_in_awaiting_tool_select_state(self, mock_sender):
        """Verify handle_workflow_select_tool works in AWAITING_TOOL_SELECT state."""
        handler = self._make_handler()
        engine = self._make_engine_awaiting_tool_select(tools=["coco", "claude"])
        handler.ctx.workflow_engine_manager.get.return_value = engine

        handler.handle_workflow_select_tool(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_tool", "tool_name": "claude", "engine_session_key": "key1"},
        )

        # Should have toggled claude off
        self.assertNotIn("claude", engine.project.pending.selected_tools)
        self.assertIn("coco", engine.project.pending.selected_tools)
        # Should have re-rendered the tool selection card via in-place update
        handler.update_card.assert_called()

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    def test_select_tool_wrong_state_noop_in_tool_select(self, mock_sender):
        """Verify tool selection in RUNNING or IDLE state is a no-op."""
        handler = self._make_handler()

        # Test RUNNING state
        engine_running = self._make_engine_awaiting_tool_select(tools=["coco"])
        engine_running.project.status = WorkflowStatus.RUNNING
        handler.ctx.workflow_engine_manager.get.return_value = engine_running

        handler.handle_workflow_select_tool(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_select_tool", "tool_name": "claude", "engine_session_key": "key1"},
        )

        # Should not update card or send new card
        handler.update_card.assert_not_called()
        handler.send_card_to_chat.assert_not_called()

        # Test IDLE state
        engine_idle = self._make_engine_awaiting_tool_select(tools=["coco"])
        engine_idle.project.status = WorkflowStatus.IDLE
        handler.ctx.workflow_engine_manager.get.return_value = engine_idle

        handler.handle_workflow_select_tool(
            "msg_2", "chat_1", "proj_1",
            {"action": "workflow_select_tool", "tool_name": "claude", "engine_session_key": "key1"},
        )

        handler.update_card.assert_not_called()
        handler.send_card_to_chat.assert_not_called()


class TestWorkflowActionRegistrations(unittest.TestCase):
    """Tests for action ID definitions and registrations."""

    def test_workflow_confirm_tools_action_registered(self):
        """Verify WORKFLOW_CONFIRM_TOOLS is defined in dispatch.py and registered in action_registry.py."""
        from src.card.actions import dispatch as action_ids

        # Check it's defined in dispatch.py
        self.assertEqual(action_ids.WORKFLOW_CONFIRM_TOOLS, "workflow_confirm_tools")

        # Check it's registered in action_registry.py by reading the source file
        with open("/home/jiataorui/work/ghostAp/src/feishu/action_registry.py", "r", encoding="utf-8") as f:
            source = f.read()
        self.assertIn("WORKFLOW_CONFIRM_TOOLS", source)
        self.assertIn("_handle_workflow_confirm_tools", source)

    def test_workflow_regenerate_script_action_registered(self):
        """Verify WORKFLOW_REGENERATE_SCRIPT is defined and registered."""
        from src.card.actions import dispatch as action_ids

        # Check it's defined in dispatch.py
        self.assertEqual(action_ids.WORKFLOW_REGENERATE_SCRIPT, "workflow_regenerate_script")

        # Check it's registered in action_registry.py by reading the source file
        with open("/home/jiataorui/work/ghostAp/src/feishu/action_registry.py", "r", encoding="utf-8") as f:
            source = f.read()
        self.assertIn("WORKFLOW_REGENERATE_SCRIPT", source)
        self.assertIn("_handle_workflow_regenerate_script", source)

    def test_forwarding_map_has_confirm_tools(self):
        """Verify _handle_workflow_confirm_tools is in FORWARDING_MAP."""
        from src.feishu.router import FORWARDING_MAP

        self.assertIn("_handle_workflow_confirm_tools", FORWARDING_MAP)
        self.assertEqual(
            FORWARDING_MAP["_handle_workflow_confirm_tools"],
            ("workflow", "handle_workflow_confirm_tools"),
        )

    def test_forwarding_map_has_regenerate_script(self):
        """Verify _handle_workflow_regenerate_script is in FORWARDING_MAP."""
        from src.feishu.router import FORWARDING_MAP

        self.assertIn("_handle_workflow_regenerate_script", FORWARDING_MAP)
        self.assertEqual(
            FORWARDING_MAP["_handle_workflow_regenerate_script"],
            ("workflow", "handle_workflow_regenerate_script"),
        )


class TestWorkflowToolSelectionCardUI(unittest.TestCase):
    """Tests for the tool selection card UI elements."""

    @staticmethod
    def _find_all_buttons(elements: list) -> list[dict]:
        """Recursively find all button elements in a card's elements tree."""
        buttons = []
        for elem in elements:
            if not isinstance(elem, dict):
                continue
            if elem.get("tag") == "button":
                buttons.append(elem)
            # Recurse into nested structures
            for key in ("elements", "columns", "children"):
                nested = elem.get(key, [])
                if isinstance(nested, list):
                    buttons.extend(TestWorkflowToolSelectionCardUI._find_all_buttons(nested))
            # Handle collapsible_panel elements
            if elem.get("tag") == "collapsible_panel":
                nested = elem.get("elements", [])
                if isinstance(nested, list):
                    buttons.extend(TestWorkflowToolSelectionCardUI._find_all_buttons(nested))
        return buttons

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = MagicMock()
        handler.reply_text = MagicMock()
        handler.update_card = MagicMock(return_value=True)
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler.send_card_to_chat = MagicMock()
        handler.get_engine_name = MagicMock(return_value="test-engine")
        return handler

    def _make_engine(self):
        engine = MagicMock()
        engine.project = WorkflowProject.from_dict({
            "status": WorkflowStatus.AWAITING_TOOL_SELECT,
            "pending_script_path": None,
            "pending_requirement": "test requirement",
            "pending_meta": None,
            "pending_is_fallback": False,
            "pending_engine_session_key": "key1",
            "pending_initiator_user_id": "user_001",
            "pending_selected_tools": None,
        })
        return engine

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    @patch("src.workflow_engine.tool_registry.get_available_tools")
    def test_tool_selection_card_shows_recommended_tools(self, mock_tools, mock_sender):
        """Verify _show_tool_selection_card includes recommended tools (coco, claude, codex) in the card."""
        from src.card.actions.dispatch import WORKFLOW_SELECT_TOOL

        mock_tools.return_value = {
            "coco": "全栈编程", "claude": "Claude AI", "codex": "Codex",
            "aiden": "Aiden", "gemini": "Gemini",
        }
        handler = self._make_handler()
        engine = self._make_engine()
        handler.ctx.workflow_engine_manager.get_or_create.return_value = engine

        handler._show_tool_selection_card(
            message_id="msg_1",
            chat_id="chat_1",
            requirement="build a web app",
            project=MagicMock(project_id="proj_1"),
            root_path="/tmp/project",
        )

        # Capture the card that was sent
        handler.send_card_to_chat.assert_called_once()
        call_args = handler.send_card_to_chat.call_args
        card = call_args[0][1]  # Second positional argument is the card

        # Check recommended tools section exists in the card elements
        elements = card.get("body", {}).get("elements", [])
        elements_text = str(elements)

        # Recommended tools should be present
        self.assertIn("coco", elements_text)
        self.assertIn("claude", elements_text)
        self.assertIn("codex", elements_text)
        self.assertIn("推荐工具", elements_text)

        # Check buttons for recommended tools exist (recursively find all buttons)
        all_buttons = self._find_all_buttons(elements)
        button_tools = [b.get("text", {}).get("content", "") for b in all_buttons]
        self.assertTrue(any("coco" in b for b in button_tools))
        self.assertTrue(any("claude" in b for b in button_tools))
        self.assertTrue(any("codex" in b for b in button_tools))

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    @patch("src.workflow_engine.tool_registry.get_available_tools")
    def test_tool_selection_card_has_confirm_button(self, mock_tools, mock_sender):
        """Verify the tool selection card has a confirm button with WORKFLOW_CONFIRM_TOOLS action."""
        from src.card.actions.dispatch import WORKFLOW_CONFIRM_TOOLS

        mock_tools.return_value = {"coco": "全栈编程", "claude": "Claude AI"}
        handler = self._make_handler()
        engine = self._make_engine()
        handler.ctx.workflow_engine_manager.get_or_create.return_value = engine

        handler._show_tool_selection_card(
            message_id="msg_1",
            chat_id="chat_1",
            requirement="test",
            project=MagicMock(project_id="proj_1"),
            root_path="/tmp/project",
        )

        call_args = handler.send_card_to_chat.call_args
        card = call_args[0][1]
        elements = card.get("body", {}).get("elements", [])

        # Find confirm button (recursively search all buttons)
        all_buttons = self._find_all_buttons(elements)
        confirm_btn = None
        for btn in all_buttons:
            btn_text = btn.get("text", {}).get("content", "")
            if "确认工具" in btn_text or ("确认" in btn_text and "生成脚本" in btn_text):
                confirm_btn = btn
                break

        self.assertIsNotNone(confirm_btn, "Confirm button not found in card")
        self.assertEqual(confirm_btn.get("value", {}).get("action"), WORKFLOW_CONFIRM_TOOLS)
        self.assertIn("确认工具", confirm_btn.get("text", {}).get("content", ""))

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    @patch("src.workflow_engine.tool_registry.get_available_tools")
    def test_tool_selection_card_has_cancel_button(self, mock_tools, mock_sender):
        """Verify the tool selection card has a cancel button with WORKFLOW_CANCEL action."""
        from src.card.actions.dispatch import WORKFLOW_CANCEL

        mock_tools.return_value = {"coco": "全栈编程", "claude": "Claude AI"}
        handler = self._make_handler()
        engine = self._make_engine()
        handler.ctx.workflow_engine_manager.get_or_create.return_value = engine

        handler._show_tool_selection_card(
            message_id="msg_1",
            chat_id="chat_1",
            requirement="test",
            project=MagicMock(project_id="proj_1"),
            root_path="/tmp/project",
        )

        call_args = handler.send_card_to_chat.call_args
        card = call_args[0][1]
        elements = card.get("body", {}).get("elements", [])

        # Find cancel button (recursively search all buttons)
        all_buttons = self._find_all_buttons(elements)
        cancel_btn = None
        for btn in all_buttons:
            btn_text = btn.get("text", {}).get("content", "")
            if btn_text == "取消":
                cancel_btn = btn
                break

        self.assertIsNotNone(cancel_btn, "Cancel button not found in card")
        self.assertEqual(cancel_btn.get("value", {}).get("action"), WORKFLOW_CANCEL)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    @patch("src.workflow_engine.tool_registry.get_available_tools")
    def test_tool_selection_card_uses_stage_colors(self, mock_tools, mock_sender):
        """Verify the tool selection card uses the tool_select color from workflow_header_colors."""
        from src.card.ui_text import UI_TEXT

        mock_tools.return_value = {"coco": "全栈编程"}
        handler = self._make_handler()
        engine = self._make_engine()
        handler.ctx.workflow_engine_manager.get_or_create.return_value = engine

        handler._show_tool_selection_card(
            message_id="msg_1",
            chat_id="chat_1",
            requirement="test",
            project=MagicMock(project_id="proj_1"),
            root_path="/tmp/project",
        )

        call_args = handler.send_card_to_chat.call_args
        card = call_args[0][1]
        header = card.get("header", {})
        template = header.get("template", "")

        expected_color = UI_TEXT["workflow_header_colors"].get("tool_select", "turquoise")
        self.assertEqual(template, expected_color)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    @patch("src.workflow_engine.tool_registry.get_available_tools")
    def test_tool_selection_card_shows_requirement(self, mock_tools, mock_sender):
        """Verify the requirement text is displayed in the tool selection card."""
        mock_tools.return_value = {"coco": "全栈编程"}
        handler = self._make_handler()
        engine = self._make_engine()
        handler.ctx.workflow_engine_manager.get_or_create.return_value = engine

        test_requirement = "Build a REST API with authentication and database"
        handler._show_tool_selection_card(
            message_id="msg_1",
            chat_id="chat_1",
            requirement=test_requirement,
            project=MagicMock(project_id="proj_1"),
            root_path="/tmp/project",
        )

        call_args = handler.send_card_to_chat.call_args
        card = call_args[0][1]
        elements = card.get("body", {}).get("elements", [])
        elements_text = str(elements)

        # Requirement should be displayed
        self.assertIn("需求", elements_text)
        self.assertIn(test_requirement[:100], elements_text)

    @patch("src.thread.get_current_sender_id", return_value="user_001")
    @patch("src.workflow_engine.tool_registry.get_available_tools")
    def test_tool_selection_initializes_default_tools(self, mock_tools, mock_sender):
        """Verify pending.selected_tools is initialized with the top recommended tool(s)."""
        mock_tools.return_value = {
            "coco": "全栈编程", "claude": "Claude AI", "codex": "Codex",
            "aiden": "Aiden", "gemini": "Gemini",
        }
        handler = self._make_handler()
        engine = self._make_engine()
        engine.project.pending.selected_tools = None  # Not yet initialized
        handler.ctx.workflow_engine_manager.get_or_create.return_value = engine

        handler._show_tool_selection_card(
            message_id="msg_1",
            chat_id="chat_1",
            requirement="test",
            project=MagicMock(project_id="proj_1"),
            root_path="/tmp/project",
        )

        # Default selection should be top 3 recommended tools: coco, claude, codex
        self.assertIsNotNone(engine.project.pending.selected_tools)
        self.assertIn("coco", engine.project.pending.selected_tools)
        self.assertIn("claude", engine.project.pending.selected_tools)
        self.assertIn("codex", engine.project.pending.selected_tools)
        self.assertEqual(engine.project.pending.selected_tools[:3], ["coco", "claude", "codex"])


if __name__ == "__main__":
    unittest.main()
