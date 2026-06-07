"""Tests for the Workflow Engine confirmation flow (AC1).

Validates:
- /wf generates a script and shows a confirmation card (AWAITING_CONFIRM)
- Confirm action triggers execute_workflow
- Cancel action resets state to IDLE
- AI fallback works when script generation fails
"""

import os
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from src.card.actions.dispatch import WORKFLOW_CANCEL, WORKFLOW_CONFIRM_START
from src.card.events.types import CardEventType
from src.card.events.workflow import workflow_confirm
from src.feishu.ws_card_action_handler import SYSTEM_CARD_ACTIONS
from src.workflow_engine.models import PendingConfirmation, WorkflowProject, WorkflowStatus
from src.workflow_engine.script_gen import validate_generated_script


class TestWorkflowConfirmConstants(unittest.TestCase):
    """Verify action_ids and registrations are in place."""

    def test_workflow_confirm_start_in_system_card_actions(self):
        self.assertIn("workflow_confirm_start", SYSTEM_CARD_ACTIONS)

    def test_workflow_cancel_in_system_card_actions(self):
        self.assertIn("workflow_cancel", SYSTEM_CARD_ACTIONS)

    def test_action_id_constants_exist(self):
        self.assertEqual(WORKFLOW_CONFIRM_START, "workflow_confirm_start")
        self.assertEqual(WORKFLOW_CANCEL, "workflow_cancel")

    def test_workflow_confirm_event_type_exists(self):
        self.assertEqual(CardEventType.WORKFLOW_CONFIRM.value, "workflow_confirm")

    def test_forwarding_map_has_workflow_confirm(self):
        from src.feishu.router import FORWARDING_MAP
        self.assertIn("_handle_workflow_confirm_start", FORWARDING_MAP)
        self.assertIn("_handle_workflow_cancel", FORWARDING_MAP)
        self.assertEqual(
            FORWARDING_MAP["_handle_workflow_confirm_start"],
            ("workflow", "handle_workflow_confirm_start"),
        )
        self.assertEqual(
            FORWARDING_MAP["_handle_workflow_cancel"],
            ("workflow", "handle_workflow_cancel"),
        )


class TestWorkflowConfirmFactory(unittest.TestCase):
    """Test the workflow_confirm factory function."""

    def test_workflow_confirm_creates_event(self):
        event = workflow_confirm(
            script_name="test-workflow",
            description="Test workflow description",
            phases=[{"title": "Phase 1", "detail": "Do something"}],
            tools=["coco", "claude"],
            budget_total=2_000_000,
            requirement="test requirement",
            initiator_user_id="user_001",
            engine_session_key="abc123",
            project_id="proj_123",
            chat_id="chat_456",
        )
        self.assertEqual(event.type, CardEventType.WORKFLOW_CONFIRM)
        self.assertEqual(event.payload["script_name"], "test-workflow")
        self.assertEqual(event.payload["tools"], ["coco", "claude"])
        self.assertEqual(event.payload["budget_total"], 2_000_000)
        self.assertEqual(event.payload["project_id"], "proj_123")
        self.assertEqual(event.payload["initiator_user_id"], "user_001")
        self.assertEqual(event.payload["engine_session_key"], "abc123")

    def test_workflow_confirm_fallback_flag(self):
        event = workflow_confirm(
            script_name="fallback",
            description="",
            phases=[],
            tools=["coco"],
            budget_total=2_000_000,
            requirement="req",
            initiator_user_id="user_001",
            engine_session_key="key123",
            is_fallback=True,
        )
        self.assertTrue(event.payload.get("is_fallback"))


class TestWorkflowProjectPendingFields(unittest.TestCase):
    """Test that WorkflowProject supports pending state via PendingConfirmation sub-model."""

    def test_pending_default_none(self):
        project = WorkflowProject()
        self.assertIsNone(project.pending)

    def test_pending_fields_settable(self):
        project = WorkflowProject()
        project.pending = PendingConfirmation(
            script_path="/tmp/wf.js",
            requirement="do stuff",
            meta={"name": "test", "tools": ["coco"]},
            is_fallback=True,
        )

        self.assertEqual(project.pending.script_path, "/tmp/wf.js")
        self.assertEqual(project.pending.requirement, "do stuff")
        self.assertEqual(project.pending.meta, {"name": "test", "tools": ["coco"]})
        self.assertTrue(project.pending.is_fallback)

    def test_serialization_roundtrip(self):
        project = WorkflowProject(
            pending=PendingConfirmation(
                script_path="/tmp/wf.js",
                requirement="build feature",
                meta={"name": "x", "phases": []},
                is_fallback=True,
            )
        )
        data = project.to_dict()
        restored = WorkflowProject.from_dict(data)
        self.assertEqual(restored.pending.script_path, "/tmp/wf.js")
        self.assertEqual(restored.pending.requirement, "build feature")
        self.assertTrue(restored.pending.is_fallback)

    def test_legacy_format_migration(self):
        """Test that legacy flat pending_* fields are migrated to PendingConfirmation."""
        legacy_data = {
            "pending_script_path": "/tmp/legacy.js",
            "pending_requirement": "legacy req",
            "pending_meta": {"name": "legacy"},
            "pending_is_fallback": True,
            "pending_initiator_user_id": "user_legacy",
            "pending_engine_session_key": "key_legacy",
            "pending_selected_tools": ["coco"],
            "pending_budget": 1000000,
            "pending_tools_mismatch": False,
        }
        restored = WorkflowProject.from_dict(legacy_data)
        self.assertIsNotNone(restored.pending)
        self.assertEqual(restored.pending.script_path, "/tmp/legacy.js")
        self.assertEqual(restored.pending.requirement, "legacy req")
        self.assertEqual(restored.pending.initiator_user_id, "user_legacy")
        self.assertEqual(restored.pending.selected_tools, ["coco"])

    def test_start_execution_migrates_fields(self):
        """Test that start_execution() moves fields from pending to runtime."""
        project = WorkflowProject(
            pending=PendingConfirmation(
                initiator_user_id="exec_user",
                selected_tools=["coco", "gemini"],
                script_path="/tmp/exec.js",
            )
        )
        self.assertIsNone(project.initiator_user_id)
        self.assertIsNone(project.selected_tools)
        self.assertIsNotNone(project.pending)

        project.start_execution()

        self.assertEqual(project.initiator_user_id, "exec_user")
        self.assertEqual(project.selected_tools, ["coco", "gemini"])
        self.assertIsNone(project.pending)

    def test_new_pending_fields(self):
        """Test that new PendingConfirmation fields work correctly."""
        pc = PendingConfirmation(
            orchestrator_agent="super-orchestrator",
            budget_tokens=10000000,
        )
        self.assertEqual(pc.orchestrator_agent, "super-orchestrator")
        self.assertEqual(pc.budget_tokens, 10000000)


class TestValidateGeneratedScriptRegression(unittest.TestCase):
    """Regression tests for validate_generated_script."""

    def test_valid_script_passes(self):
        script = '''export const meta = {
  name: "test-workflow",
  description: "Test",
  phases: [{ title: "Phase 1", detail: "Do stuff" }],
  tools: ["coco"]
};

export default async function() {
  const result = await agent("do something", { tool: "coco" });
  return result;
}
'''
        is_valid, errors = validate_generated_script(script)
        self.assertTrue(is_valid, f"Expected valid, got errors: {errors}")

    def test_empty_script_fails(self):
        is_valid, errors = validate_generated_script("")
        self.assertFalse(is_valid)

    def test_missing_meta_fails(self):
        script = 'export default async function() { await agent("x"); }'
        is_valid, errors = validate_generated_script(script)
        self.assertFalse(is_valid)
        self.assertTrue(any("meta" in e.lower() for e in errors))

    def test_dangerous_pattern_warns(self):
        script = '''export const meta = {
  name: "bad", description: "Bad",
  phases: [{title: "P1", detail: "d"}]
};
const fs = require('fs');
export default async function() { await agent("x"); }
'''
        is_valid, messages = validate_generated_script(script)
        # Dangerous patterns are now fail-closed blocking errors (not warnings).
        # The validator emits "[capability] Forbidden pattern:" messages.
        self.assertFalse(is_valid)
        self.assertTrue(any(
            "[capability]" in m or "Forbidden pattern" in m
            for m in messages
        ))


class TestWorkflowHandlerConfirmFlow(unittest.TestCase):
    """Integration tests for the start_workflow → confirm → execute flow."""

    def _make_handler(self):
        """Create a WorkflowHandler with mocked dependencies."""
        from src.feishu.handlers.workflow import WorkflowHandler

        ctx = MagicMock()
        ctx.workflow_engine_manager = MagicMock()
        ctx.project_manager = MagicMock()
        ctx.progress_reporter = MagicMock()

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = ctx
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.reply_error = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg_card_123")
        handler.update_card = MagicMock(return_value=True)
        handler.add_reaction = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler.get_engine_name = MagicMock(return_value="coco")
        handler.ensure_request_id = MagicMock(return_value="req_123")
        handler._submit_engine_task = MagicMock()

        return handler, ctx

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    @patch("src.feishu.handlers.workflow.WorkflowHandler._generate_script_via_ai")
    @patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True)
    @patch("src.workflow_engine.templates.discover_templates", return_value=[])
    def test_start_workflow_sets_awaiting_confirm(
        self, mock_templates, mock_node, mock_gen, mock_sender
    ):
        handler, ctx = self._make_handler()

        # Mock project
        project = MagicMock()
        project.root_path = "/tmp/project"
        project.project_id = "proj_1"
        project.project_name = "test"
        handler._ensure_project = MagicMock(return_value=project)
        handler._resolve_project_from_id = MagicMock(return_value=project)

        # Mock engine with project
        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject()
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        # Step 1: start_workflow shows agent selection card (AWAITING_AGENT_SELECT)
        handler.start_workflow("msg_1", "chat_1", "do code review", project)

        # Should have sent agent selection card
        self.assertEqual(handler.send_card_to_chat.call_count, 1)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_AGENT_SELECT)
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key)
        # Default orchestrator agent should be set
        self.assertEqual(engine.project.pending.orchestrator_agent if engine.project.pending else None, "coco")

        # Step 1b: Select agent to proceed to tool selection
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler._resolve_project_from_id = MagicMock(return_value=project)
        handler.handle_workflow_select_agent(
            "msg_1b", "chat_1",
            "proj_1",
            {"action": "workflow_select_agent", "agent_type": "coco", "engine_session_key": session_key, "project_id": "proj_1"}
        )
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)
        # Agent selection should have sent tool selection card
        self.assertEqual(handler.send_card_to_chat.call_count, 2)
        # Get the new session key after tool selection state is initialized
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key)

        # Mock AI generation result for confirm_tools step
        mock_gen.return_value = (
            "/tmp/project/.ghostap/workflow_scripts/generated_workflow.js",
            {"name": "test-wf", "description": "Test", "phases": [], "tools": ["coco"]},
            False,
        )

        # Step 2: confirm tool selection transitions to role selection (step 3)
        handler.handle_workflow_confirm_tools(
            "msg_2", "chat_1", "proj_1",
            {"action": "workflow_confirm_tools", "engine_session_key": session_key}
        )

        # 4-step flow: agent select (1) + tool select (1) = 2 so far.
        # confirm_tools shows role-selection card via update_card (no extra send_card_to_chat).
        self.assertEqual(handler.send_card_to_chat.call_count, 2)
        handler.update_card.assert_called_once()
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_ROLE_SELECT)

        # Step 3: confirm role selection proceeds to script generation / AWAITING_CONFIRM
        session_key_3 = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key_3)
        handler.handle_workflow_confirm_roles_and_generate(
            "msg_3", "chat_1", "proj_1",
            {"action": "workflow_confirm_roles_and_generate", "engine_session_key": session_key_3}
        )

        # Engine project should now be AWAITING_CONFIRM (step 4)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)
        self.assertIsNotNone(engine.project.pending.script_path if engine.project.pending else None)
        self.assertEqual(engine.project.pending.requirement if engine.project.pending else None, "do code review")

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_confirm_start_triggers_execution(self, mock_sender):
        import hashlib
        import tempfile

        handler, ctx = self._make_handler()

        # Write a valid script to a real temp file so TOCTOU checks pass
        script_content = (
            "export const meta = {\n"
            "  name: 'test',\n"
            "  description: 'test workflow',\n"
            "  tools: ['coco'],\n"
            "};\n"
            "\n"
            "export default async function() {\n"
            "  await agent('do work');\n"
            "}\n"
        )
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write(script_content)
        tmp.close()
        script_hash = hashlib.sha256(script_content.encode("utf-8")).hexdigest()

        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path=tmp.name,
                requirement="do X",
                meta={"name": "x", "tools": ["coco"]},
                initiator_user_id="user_123",
                engine_session_key="test_session_key",
                selected_tools=["coco"],
                script_hash=script_hash,
            ),
        )
        engine.is_running = False
        ctx.workflow_engine_manager.get.return_value = engine
        handler._get_root_path = MagicMock(return_value="/tmp/project")

        project_mock = MagicMock()
        project_mock.project_name = "test"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_start(
            "msg_1", "chat_1", "proj_1",
            {"action": WORKFLOW_CONFIRM_START, "engine_session_key": "test_session_key"}
        )

        try:
            os.unlink(tmp.name)
        except OSError:
            pass

        # Should have submitted the workflow task
        handler._submit_engine_task.assert_called_once()
        # Pending state should be cleared
        self.assertIsNone(engine.project.pending)

    @patch("src.thread.get_current_sender_id", return_value="user_abc")
    def test_cancel_resets_to_idle(self, mock_sender):
        handler, ctx = self._make_handler()

        # Set up engine in AWAITING_CONFIRM state
        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path="/tmp/nonexistent_wf.js",
                requirement="do Y",
                engine_session_key="sess_cancel_key",
                initiator_user_id="user_abc",
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine
        handler._get_root_path = MagicMock(return_value="/tmp/project")

        handler.handle_workflow_cancel(
            "msg_1", "chat_1", "proj_1",
            {"action": WORKFLOW_CANCEL, "engine_session_key": "sess_cancel_key"}
        )

        # State should be IDLE
        self.assertEqual(engine.project.status, WorkflowStatus.IDLE)
        self.assertIsNone(engine.project.pending)
        # Card should be updated
        handler.update_card.assert_called_once()

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    @patch("src.feishu.handlers.workflow.WorkflowHandler._generate_script_via_ai")
    @patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True)
    @patch("src.workflow_engine.templates.discover_templates", return_value=[])
    def test_fallback_still_shows_confirm_card(
        self, mock_templates, mock_node, mock_gen, mock_sender
    ):
        handler, ctx = self._make_handler()

        project = MagicMock()
        project.root_path = "/tmp/project"
        project.project_id = "proj_1"
        project.project_name = "test"
        handler._ensure_project = MagicMock(return_value=project)
        handler._resolve_project_from_id = MagicMock(return_value=project)

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject()
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        # Step 1: start_workflow shows agent selection card
        handler.start_workflow("msg_1", "chat_1", "complex task", project)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_AGENT_SELECT)
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None

        # Step 1b: Select agent to proceed to tool selection
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler.handle_workflow_select_agent(
            "msg_1b", "chat_1",
            "proj_1",
            {"action": "workflow_select_agent", "agent_type": "coco", "engine_session_key": session_key, "project_id": "proj_1"}
        )
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)
        # Get the new session key after tool selection state is initialized
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key)

        # Step 2: confirm tool selection transitions to role selection
        mock_gen.return_value = (
            "/tmp/project/.ghostap/workflow_scripts/generated_workflow.js",
            None,  # No meta from fallback
            True,  # is_fallback=True
        )

        handler.handle_workflow_confirm_tools(
            "msg_2", "chat_1", "proj_1",
            {"action": "workflow_confirm_tools", "engine_session_key": session_key}
        )

        # After confirm_tools, status is AWAITING_ROLE_SELECT (not yet AWAITING_CONFIRM)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_ROLE_SELECT)
        session_key_3 = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key_3)

        # Step 3: confirm roles - this is where script generation actually happens
        handler.handle_workflow_confirm_roles_and_generate(
            "msg_3", "chat_1", "proj_1",
            {"action": "workflow_confirm_roles_and_generate", "engine_session_key": session_key_3}
        )

        # Should show confirm card with fallback flag
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)
        self.assertTrue(engine.project.pending.is_fallback if engine.project.pending else False)
        # update_card should have been called at least once (role select) and then again for confirm
        self.assertGreaterEqual(handler.update_card.call_count, 1)


    @patch("src.thread.get_current_sender_id", return_value="user_BBB")
    def test_confirm_rejected_for_non_initiator(self, mock_sender):
        handler, ctx = self._make_handler()

        # Set up engine in AWAITING_CONFIRM state with user_AAA as initiator
        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path="/tmp/wf.js",
                requirement="do X",
                meta={"name": "x"},
                initiator_user_id="user_AAA",
                engine_session_key="valid_key",
            ),
        )
        engine.is_running = False
        ctx.workflow_engine_manager.get.return_value = engine
        handler._get_root_path = MagicMock(return_value="/tmp/project")

        project_mock = MagicMock()
        project_mock.project_name = "test"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_start(
            "msg_1", "chat_1", "proj_1",
            {"action": WORKFLOW_CONFIRM_START, "engine_session_key": "valid_key"}
        )

        # Should reject with message about initiator only
        handler.reply_card.assert_called_once()
        rejection_card = handler.reply_card.call_args[0][1]
        rejection_title = rejection_card["header"]["title"]["content"]
        rejection_msg = rejection_card["body"]["elements"][0]["content"]
        self.assertEqual(rejection_title, "无操作权限")
        self.assertIn("发起者", rejection_msg)
        # Should NOT have submitted the engine task
        handler._submit_engine_task.assert_not_called()
        # Status should still be AWAITING_CONFIRM
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)

    @patch("src.thread.get_current_sender_id", return_value="user_AAA")
    def test_confirm_rejected_for_session_key_mismatch(self, mock_sender):
        handler, ctx = self._make_handler()

        # Set up engine in AWAITING_CONFIRM state with correct_key
        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path="/tmp/wf.js",
                requirement="do X",
                meta={"name": "x"},
                initiator_user_id="user_AAA",
                engine_session_key="correct_key",
            ),
        )
        engine.is_running = False
        ctx.workflow_engine_manager.get.return_value = engine
        handler._get_root_path = MagicMock(return_value="/tmp/project")

        project_mock = MagicMock()
        project_mock.project_name = "test"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_start(
            "msg_1", "chat_1", "proj_1",
            {"action": WORKFLOW_CONFIRM_START, "engine_session_key": "wrong_key"}
        )

        # Should reject with message about invalid token
        handler.reply_card.assert_called_once()
        rejection_card = handler.reply_card.call_args[0][1]
        rejection_title = rejection_card["header"]["title"]["content"]
        self.assertEqual(rejection_title, "会话已过期")
        # Should NOT have submitted the engine task
        handler._submit_engine_task.assert_not_called()
        # Status should still be AWAITING_CONFIRM
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)


class TestConfirmCardContent(unittest.TestCase):
    """Verify confirm card includes script preview and phase tool tags."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        ctx = MagicMock()
        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = ctx
        return handler

    def _get_elements(self, card: dict) -> list:
        """Extract elements from card structure (handles body.elements wrapping)."""
        body = card.get("body", card)
        return body.get("elements", card.get("elements", []))

    def test_card_contains_script_preview(self):
        """Confirm card should include script preview markdown when script_content provided."""
        handler = self._make_handler()
        meta = {
            "name": "test-wf",
            "description": "Test workflow",
            "phases": [{"title": "Plan", "detail": "Make a plan"}],
            "tools": ["coco"],
        }
        script = "export const meta = {};\nawait agent('do stuff');"

        card = handler._build_confirm_card(
            meta=meta,
            requirement="do code review",
            engine_session_key="key_123",
            chat_id="chat_1",
            project_id="proj_1",
            script_content=script,
        )

        # Find all markdown elements (including inside collapsible_panel)
        elements = self._get_elements(card)
        all_md = " ".join(
            el.get("content", "") for el in elements if el.get("tag") == "markdown"
        )
        # Script preview is inside a collapsible_panel — extract nested markdown
        for el in elements:
            if el.get("tag") == "collapsible_panel":
                for inner in el.get("elements", []):
                    if inner.get("tag") == "markdown":
                        all_md += " " + inner.get("content", "")
        # Check collapsible header text
        panel_headers = " ".join(
            el.get("header", {}).get("title", {}).get("content", "")
            for el in elements if el.get("tag") == "collapsible_panel"
        )
        self.assertIn("编排脚本预览", panel_headers)
        self.assertIn("```javascript", all_md)
        self.assertIn("agent('do stuff')", all_md)

    def test_card_without_script_has_no_preview(self):
        """Confirm card should not include preview section when no script_content."""
        handler = self._make_handler()
        meta = {
            "name": "test-wf",
            "description": "Test",
            "phases": [],
            "tools": ["coco"],
        }

        card = handler._build_confirm_card(
            meta=meta,
            requirement="task",
            engine_session_key="key_1",
            chat_id="chat_1",
            project_id="proj_1",
            script_content="",
        )

        elements = self._get_elements(card)
        all_md = " ".join(
            el.get("content", "") for el in elements if el.get("tag") == "markdown"
        )
        self.assertNotIn("编排脚本预览", all_md)

    def test_phases_include_tool_tags(self):
        """Phase lines should show tool tags when phase_tool_mapping is provided.

        Phases now live inside a collapsible_panel, so we search recursively through
        top-level and collapsible_panel markdown elements.
        """
        handler = self._make_handler()
        meta = {
            "name": "review-wf",
            "description": "Code review",
            "phases": [
                {"title": "Analysis", "detail": "Analyze code"},
                {"title": "Review", "detail": "Review findings"},
            ],
            "tools": ["coco", "claude"],
            "phase_tool_mapping": {
                "Analysis": ["coco"],
                "Review": ["claude"],
            },
        }

        card = handler._build_confirm_card(
            meta=meta,
            requirement="review code",
            engine_session_key="key_2",
            chat_id="chat_1",
            project_id="proj_1",
        )

        elements = self._get_elements(card)

        def flatten_md(els: list[dict]) -> str:
            out: list[str] = []
            for e in els:
                if e.get("tag") == "markdown":
                    out.append(e.get("content", ""))
                if e.get("tag") == "collapsible_panel":
                    out.append(flatten_md(e.get("elements", [])))
            return "\n".join(out)

        all_md = flatten_md(elements)
        self.assertIn("`coco`", all_md)
        self.assertIn("`claude`", all_md)
        # Tool tags should appear near the corresponding phase lines (following sub-line)
        lines = all_md.split("\n")
        # Find phase header index and check following lines for tool tag
        def tool_after_phase_title(phase_title: str, tool_name: str) -> bool:
            for i, l in enumerate(lines):
                if phase_title in l:
                    # Check next few lines for the tool label
                    window_start = max(0, i)
                    window_end = min(len(lines), i + 4)
                    window = " ".join(lines[window_start:window_end])
                    return f"`{tool_name}`" in window
            return False

        self.assertTrue(tool_after_phase_title("Analysis", "coco"))
        self.assertTrue(tool_after_phase_title("Review", "claude"))

    def test_payload_script_preview_field(self):
        """WorkflowConfirmPayload should accept script_preview as NotRequired field."""
        from src.card.events.payloads import WorkflowConfirmPayload

        # Should be able to construct with script_preview
        payload: WorkflowConfirmPayload = {
            "script_name": "test",
            "description": "desc",
            "phases": [],
            "tools": ["coco"],
            "budget_total": 2_000_000,
            "requirement": "req",
            "initiator_user_id": "u1",
            "engine_session_key": "k1",
            "script_preview": "```javascript\nconsole.log('hi');\n```",
        }
        self.assertEqual(payload["script_preview"], "```javascript\nconsole.log('hi');\n```")

        # Should also work without script_preview (NotRequired)
        payload_no_preview: WorkflowConfirmPayload = {
            "script_name": "test",
            "description": "desc",
            "phases": [],
            "tools": ["coco"],
            "budget_total": 2_000_000,
            "requirement": "req",
            "initiator_user_id": "u1",
            "engine_session_key": "k1",
        }
        self.assertNotIn("script_preview", payload_no_preview)


class TestWorkflowE2EConfirmFlow(unittest.TestCase):
    """E2E: /wf '重构登录模块' → confirm card with full structure."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        ctx = MagicMock()
        ctx.workflow_engine_manager = MagicMock()
        ctx.project_manager = MagicMock()
        ctx.progress_reporter = MagicMock()

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = ctx
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.reply_error = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg_card_e2e")
        handler.update_card = MagicMock(return_value=True)
        handler.add_reaction = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler.get_engine_name = MagicMock(return_value="coco")
        handler.ensure_request_id = MagicMock(return_value="req_e2e")
        handler._submit_engine_task = MagicMock()

        return handler, ctx

    def _get_elements(self, card: dict) -> list:
        body = card.get("body", card)
        return body.get("elements", card.get("elements", []))

    @patch("src.thread.get_current_sender_id", return_value="user_e2e")
    @patch("src.feishu.handlers.workflow.WorkflowHandler._generate_script_via_ai")
    @patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True)
    @patch("src.workflow_engine.templates.discover_templates", return_value=[])
    def test_e2e_confirm_card_structure(self, mock_templates, mock_node, mock_gen, mock_sender):
        """Full E2E: start_workflow → tool select → confirm card contains script preview, tools, buttons."""
        import tempfile

        handler, ctx = self._make_handler()

        project = MagicMock()
        project.root_path = "/tmp/project"
        project.project_id = "proj_e2e"
        project.project_name = "login-refactor"
        handler._ensure_project = MagicMock(return_value=project)
        handler._resolve_project_from_id = MagicMock(return_value=project)

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject()
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        # Simulate successful AI script generation with phases and tools
        test_script = '''export const meta = {
  name: "login-refactor",
  description: "重构登录模块",
  phases: [
    { title: "分析现有代码", detail: "分析登录模块结构" },
    { title: "实施重构", detail: "执行代码重构" },
    { title: "验证", detail: "运行测试验证" }
  ],
  tools: ["coco", "claude"]
};

export default async function() {
  await phase("分析现有代码");
  const analysis = await agent("分析登录模块代码结构", { tool: "coco" });
  await phase("实施重构");
  const refactor = await agent("根据分析结果重构登录模块", { tool: "claude" });
  await phase("验证");
  const verify = await agent("运行测试确认重构无回归", { tool: "coco" });
  return verify;
}'''
        # Write script to a real temp file so start_workflow can read it
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write(test_script)
        tmp.close()
        script_path = tmp.name

        # Step 1: start_workflow shows agent selection card
        handler.start_workflow("msg_e2e", "chat_e2e", "重构登录模块", project)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_AGENT_SELECT)
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key)

        # Step 1b: Select agent to proceed to tool selection
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler._resolve_project_from_id = MagicMock(return_value=project)
        handler.handle_workflow_select_agent(
            "msg_e2e_1b", "chat_e2e",
            "proj_e2e",
            {"action": "workflow_select_agent", "agent_type": "coco", "engine_session_key": session_key, "project_id": "proj_e2e"}
        )
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)
        # Get the new session key after tool selection state is initialized
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key)

        # Step 2: confirm tool selection transitions to role selection (step 3)
        mock_gen.return_value = (
            script_path,
            {
                "name": "login-refactor",
                "description": "重构登录模块",
                "phases": [
                    {"title": "分析现有代码", "detail": "分析登录模块结构"},
                    {"title": "实施重构", "detail": "执行代码重构"},
                    {"title": "验证", "detail": "运行测试验证"},
                ],
                "tools": ["coco", "claude"],
            },
            False,  # not fallback
        )

        handler.handle_workflow_confirm_tools(
            "msg_e2e_2", "chat_e2e", "proj_e2e",
            {"action": "workflow_confirm_tools", "engine_session_key": session_key}
        )

        # After confirm_tools, state should be AWAITING_ROLE_SELECT (step 3)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_ROLE_SELECT)
        session_key_3 = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key_3)

        # Step 3: confirm roles - this actually produces the confirm card
        handler.handle_workflow_confirm_roles_and_generate(
            "msg_e2e_3", "chat_e2e", "proj_e2e",
            {"action": "workflow_confirm_roles_and_generate", "engine_session_key": session_key_3}
        )
        os.unlink(script_path)

        # Verify engine state
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)
        self.assertEqual(engine.project.pending.requirement if engine.project.pending else None, "重构登录模块")
        self.assertIsNotNone(engine.project.pending.engine_session_key if engine.project.pending else None)
        self.assertFalse(engine.project.pending.is_fallback if engine.project.pending else False)

        # Verify confirm card was sent (update_card called at least twice: role select + confirm)
        self.assertGreaterEqual(handler.update_card.call_count, 1)
        # Use the final update_card call (the one with the confirm card that has the script)
        # Look at the last update_card call for a card that has the script preview
        last_card = None
        for call in handler.update_card.call_args_list:
            candidate = call[0][1] if len(call[0]) > 1 else None
            if candidate and isinstance(candidate, dict):
                text = str(candidate)
                if "```javascript" in text or "confirm_start" in text:
                    last_card = candidate
        if last_card is None:
            # Fallback: use the last update_card call
            last_call = handler.update_card.call_args_list[-1]
            last_card = last_call[0][1]
        card = last_card

        # Extract all card content
        elements = self._get_elements(card)
        all_md = " ".join(
            el.get("content", "") for el in elements if el.get("tag") == "markdown"
        )
        # Also extract markdown inside collapsible_panel elements
        for el in elements:
            if el.get("tag") == "collapsible_panel":
                for inner in el.get("elements", []):
                    if inner.get("tag") == "markdown":
                        all_md += " " + inner.get("content", "")
        # Collect all action button values
        all_actions = []
        for el in elements:
            if el.get("tag") == "action":
                for action in el.get("actions", []):
                    all_actions.append(action)
            # Also check column_set patterns (Schema 2.0 compliant)
            if el.get("tag") == "column_set":
                for col in el.get("columns", []):
                    for col_el in col.get("elements", []):
                        if col_el.get("tag") == "action":
                            for action in col_el.get("actions", []):
                                all_actions.append(action)
                        # Schema 2.0: buttons directly inside columns
                        if col_el.get("tag") == "button":
                            all_actions.append(col_el)

        # AC1 check: requirement is displayed
        self.assertIn("重构登录模块", all_md)

        # AC1 check: script preview with javascript code fence
        self.assertIn("```javascript", all_md)

        # AC1 check: phases are shown
        self.assertIn("分析现有代码", all_md)
        self.assertIn("实施重构", all_md)
        self.assertIn("验证", all_md)

        # AC1 check: confirm and cancel buttons present
        action_values = []
        for a in all_actions:
            val = a.get("value", {})
            if isinstance(val, dict):
                action_values.append(val.get("action", ""))
            elif isinstance(val, str):
                import json
                try:
                    parsed = json.loads(val)
                    action_values.append(parsed.get("action", ""))
                except (json.JSONDecodeError, TypeError):
                    pass

        self.assertIn("workflow_confirm_start", action_values)
        self.assertIn("workflow_cancel", action_values)

    @patch("src.feishu.handlers.workflow.WorkflowHandler._generate_script_via_ai")
    @patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True)
    @patch("src.workflow_engine.templates.discover_templates", return_value=[])
    @patch("src.thread.get_current_sender_id", return_value="user_e2e")
    def test_e2e_confirm_then_execute(self, mock_sender, mock_templates, mock_node, mock_gen):
        """Full E2E: start → tool select → confirm button → engine.execute_workflow is called."""
        import hashlib
        import tempfile

        handler, ctx = self._make_handler()

        project = MagicMock()
        project.root_path = "/tmp/project"
        project.project_id = "proj_e2e2"
        project.project_name = "test"
        handler._ensure_project = MagicMock(return_value=project)
        handler._resolve_project_from_id = MagicMock(return_value=project)

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject()
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        # Step 1: start_workflow shows agent selection card
        handler.start_workflow("msg_1", "chat_e2e2", "重构登录模块", project)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_AGENT_SELECT)
        tool_select_session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(tool_select_session_key)

        # Step 1b: Select agent to proceed to tool selection
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler.handle_workflow_select_agent(
            "msg_1b", "chat_e2e2",
            "proj_e2e2",
            {"action": "workflow_select_agent", "agent_type": "coco", "engine_session_key": tool_select_session_key, "project_id": "proj_e2e2"}
        )
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)
        # Get the new session key after tool selection state is initialized
        tool_select_session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(tool_select_session_key)

        # Step 2: confirm tool selection transitions to role selection (step 3)
        # Write a valid script to a real temp file so the confirm-time
        # TOCTOU re-read succeeds.
        e2e_script = (
            "export const meta = {\n"
            "  name: 'wf',\n"
            "  description: 'test',\n"
            "  tools: ['coco'],\n"
            "};\n"
            "\n"
            "export default async function() {\n"
            "  await agent('do work');\n"
            "}\n"
        )
        e2e_tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
        e2e_tmp.write(e2e_script)
        e2e_tmp.close()
        mock_gen.return_value = (
            e2e_tmp.name,
            {"name": "wf", "description": "test", "phases": [], "tools": ["coco"]},
            False,
        )

        handler.handle_workflow_confirm_tools(
            "msg_2", "chat_e2e2", "proj_e2e2",
            {"action": "workflow_confirm_tools", "engine_session_key": tool_select_session_key},
        )

        # After confirm_tools, engine is at AWAITING_ROLE_SELECT
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_ROLE_SELECT)
        role_session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(role_session_key)

        # Step 3: confirm roles — this actually produces AWAITING_CONFIRM
        handler.handle_workflow_confirm_roles_and_generate(
            "msg_3", "chat_e2e2", "proj_e2e2",
            {"action": "workflow_confirm_roles_and_generate", "engine_session_key": role_session_key},
        )

        # Now simulate the user pressing confirm button
        # The engine should be in AWAITING_CONFIRM with a session key
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key)

        # Ensure pending.script_hash is populated so the TOCTOU guard passes
        if engine.project.pending and engine.project.pending.script_hash is None:
            engine.project.pending.script_hash = hashlib.sha256(e2e_script.encode("utf-8")).hexdigest()

        handler._get_root_path = MagicMock(return_value="/tmp/project")

        handler.handle_workflow_confirm_start(
            "msg_card_e2e", "chat_e2e2", "proj_e2e2",
            {"action": "workflow_confirm_start", "engine_session_key": session_key},
        )

        try:
            os.unlink(e2e_tmp.name)
        except OSError:
            pass

        # Should have submitted the engine task
        handler._submit_engine_task.assert_called_once()
        # Pending state cleared
        self.assertIsNone(engine.project.pending)


class TestWorkflowFallbackPath(unittest.TestCase):
    """Test that AI script generation failure shows fallback confirm card."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        ctx = MagicMock()
        ctx.workflow_engine_manager = MagicMock()
        ctx.project_manager = MagicMock()
        ctx.progress_reporter = MagicMock()

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = ctx
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.reply_error = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg_fb")
        handler.update_card = MagicMock(return_value=True)
        handler.add_reaction = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler.get_engine_name = MagicMock(return_value="coco")
        handler.ensure_request_id = MagicMock(return_value="req_fb")
        handler._submit_engine_task = MagicMock()

        return handler, ctx

    def _get_elements(self, card: dict) -> list:
        body = card.get("body", card)
        return body.get("elements", card.get("elements", []))

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    @patch("src.feishu.handlers.workflow.WorkflowHandler._generate_script_via_ai")
    @patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True)
    @patch("src.workflow_engine.templates.discover_templates", return_value=[])
    def test_fallback_card_shows_warning(self, mock_templates, mock_node, mock_gen, mock_sender):
        """When AI gen fails, fallback card shows warning and is_fallback=True."""
        handler, ctx = self._make_handler()

        project = MagicMock()
        project.root_path = "/tmp/project"
        project.project_id = "proj_fb"
        project.project_name = "test"
        handler._ensure_project = MagicMock(return_value=project)
        handler._resolve_project_from_id = MagicMock(return_value=project)

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject()
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        # Step 1: start_workflow shows agent selection card
        handler.start_workflow("msg_fb", "chat_fb", "复杂重构任务", project)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_AGENT_SELECT)
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None

        # Step 1b: Select agent to proceed to tool selection
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler.handle_workflow_select_agent(
            "msg_fb_1b", "chat_fb",
            "proj_fb",
            {"action": "workflow_select_agent", "agent_type": "coco", "engine_session_key": session_key, "project_id": "proj_fb"}
        )
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)
        # Get the new session key after tool selection state is initialized
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key)

        # Step 2: confirm tool selection transitions to role selection (step 3)
        mock_gen.return_value = (
            "/tmp/project/.ghostap/workflow_scripts/generated_workflow.js",
            None,  # No meta (fallback generated simple script)
            True,  # is_fallback=True
        )

        handler.handle_workflow_confirm_tools(
            "msg_fb_2", "chat_fb", "proj_fb",
            {"action": "workflow_confirm_tools", "engine_session_key": session_key}
        )

        # After confirm_tools, engine is AWAITING_ROLE_SELECT
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_ROLE_SELECT)
        session_key_3 = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key_3)

        # Step 3: confirm roles - produces script + confirm card
        handler.handle_workflow_confirm_roles_and_generate(
            "msg_fb_3", "chat_fb", "proj_fb",
            {"action": "workflow_confirm_roles_and_generate", "engine_session_key": session_key_3}
        )

        # Engine should be in AWAITING_CONFIRM with fallback flag
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)
        self.assertTrue(engine.project.pending.is_fallback if engine.project.pending else False)
        self.assertEqual(engine.project.pending.requirement if engine.project.pending else None, "复杂重构任务")

        # Verify confirm card shows fallback warning - use last card with script content
        last_card = None
        for call in handler.update_card.call_args_list:
            candidate = call[0][1] if len(call[0]) > 1 else None
            if candidate and isinstance(candidate, dict):
                text = str(candidate)
                if "confirm_start" in text or "javascript" in text:
                    last_card = candidate
        if last_card is None:
            last_card = handler.update_card.call_args_list[-1][0][1]
        card = last_card
        elements = self._get_elements(card)
        # Collect all text content from markdown AND note elements
        all_text = ""
        for el in elements:
            if el.get("tag") == "markdown":
                all_text += " " + el.get("content", "")
            elif el.get("tag") == "note":
                for sub in el.get("elements", []):
                    all_text += " " + sub.get("content", "")

        # Should contain fallback warning indicator
        self.assertTrue(
            "默认模板" in all_text or "fallback" in all_text.lower() or "失败" in all_text,
            f"Fallback warning not found in card content: {all_text[:200]}",
        )

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    @patch("src.feishu.handlers.workflow.WorkflowHandler._generate_script_via_ai")
    @patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True)
    @patch("src.workflow_engine.templates.discover_templates", return_value=[])
    def test_fallback_card_still_has_confirm_button(self, mock_templates, mock_node, mock_gen, mock_sender):
        """Even on fallback, confirm/cancel buttons should be present."""
        handler, ctx = self._make_handler()

        project = MagicMock()
        project.root_path = "/tmp/project"
        project.project_id = "proj_fb2"
        project.project_name = "test"
        handler._ensure_project = MagicMock(return_value=project)
        handler._resolve_project_from_id = MagicMock(return_value=project)

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject()
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        # Step 1: start_workflow shows agent selection card
        handler.start_workflow("msg_fb2", "chat_fb2", "任务描述", project)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_AGENT_SELECT)
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None

        # Step 1b: Select agent to proceed to tool selection
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler.handle_workflow_select_agent(
            "msg_fb2_1b", "chat_fb2",
            "proj_fb2",
            {"action": "workflow_select_agent", "agent_type": "coco", "engine_session_key": session_key, "project_id": "proj_fb2"}
        )
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)
        # Get the new session key after tool selection state is initialized
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key)

        # Step 2: confirm tool selection transitions to role selection
        mock_gen.return_value = (
            "/tmp/project/.ghostap/workflow_scripts/generated_workflow.js",
            None,
            True,
        )

        handler.handle_workflow_confirm_tools(
            "msg_fb2_2", "chat_fb2", "proj_fb2",
            {"action": "workflow_confirm_tools", "engine_session_key": session_key}
        )

        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_ROLE_SELECT)
        session_key_3 = engine.project.pending.engine_session_key if engine.project.pending else None
        self.assertIsNotNone(session_key_3)

        # Step 3: confirm roles to reach AWAITING_CONFIRM and confirm card
        handler.handle_workflow_confirm_roles_and_generate(
            "msg_fb2_3", "chat_fb2", "proj_fb2",
            {"action": "workflow_confirm_roles_and_generate", "engine_session_key": session_key_3}
        )

        # Find the last confirm card - look for confirm_start action
        last_card = None
        for call in handler.update_card.call_args_list:
            candidate = call[0][1] if len(call[0]) > 1 else None
            if candidate and isinstance(candidate, dict):
                text = str(candidate)
                if "confirm_start" in text:
                    last_card = candidate
        if last_card is None:
            last_card = handler.update_card.call_args_list[-1][0][1]
        card = last_card
        elements = self._get_elements(card)

        # Find action buttons (check both legacy action containers and Schema 2.0 column_set)
        action_values = []
        for el in elements:
            if el.get("tag") == "action":
                for action in el.get("actions", []):
                    val = action.get("value", {})
                    if isinstance(val, dict):
                        action_values.append(val.get("action", ""))
            # Schema 2.0: buttons inside column_set columns
            if el.get("tag") == "column_set":
                for col in el.get("columns", []):
                    for col_el in col.get("elements", []):
                        if col_el.get("tag") == "button":
                            val = col_el.get("value", {})
                            if isinstance(val, dict):
                                action_values.append(val.get("action", ""))

        self.assertIn("workflow_confirm_start", action_values)
        self.assertIn("workflow_cancel", action_values)


class TestWorkflowToolSelectionFirstFlow(unittest.TestCase):
    """Tests for the tool-selection-first workflow (AC2)."""

    def _make_handler(self):
        """Create a WorkflowHandler with mocked dependencies."""
        from src.feishu.handlers.workflow import WorkflowHandler

        ctx = MagicMock()
        ctx.workflow_engine_manager = MagicMock()
        ctx.project_manager = MagicMock()
        ctx.progress_reporter = MagicMock()

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = ctx
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.reply_error = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg_card_123")
        handler.update_card = MagicMock(return_value=True)
        handler.add_reaction = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler.get_engine_name = MagicMock(return_value="coco")
        handler.ensure_request_id = MagicMock(return_value="req_123")
        handler._submit_engine_task = MagicMock()

        return handler, ctx

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    @patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True)
    @patch("src.workflow_engine.templates.discover_templates", return_value=[])
    def test_start_workflow_shows_tool_selection_card(
        self, mock_templates, mock_node, mock_sender
    ):
        """Verify start_workflow() shows a tool selection card (not confirm card directly)."""
        handler, ctx = self._make_handler()

        project = MagicMock()
        project.root_path = "/tmp/project"
        project.project_id = "proj_1"
        project.project_name = "test"
        handler._ensure_project = MagicMock(return_value=project)

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject()
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        handler.start_workflow("msg_1", "chat_1", "do code review", project)

        # Should send an agent selection card (not generate script yet, not tool selection yet)
        handler.send_card_to_chat.assert_called_once()
        # update_card should NOT be called (no generating -> confirm transition)
        handler.update_card.assert_not_called()

        # Engine project should be AWAITING_AGENT_SELECT, not AWAITING_TOOL_SELECT or AWAITING_CONFIRM
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_AGENT_SELECT)
        self.assertIsNone(engine.project.pending.script_path if engine.project.pending else None)
        self.assertIsNone(engine.project.pending.meta if engine.project.pending else None)
        # Default orchestrator agent should be set
        self.assertEqual(engine.project.pending.orchestrator_agent if engine.project.pending else None, "coco")
        # selected_tools should NOT be set yet (set after agent selection)
        self.assertIsNone(engine.project.pending.selected_tools if engine.project.pending else None)

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    @patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True)
    @patch("src.workflow_engine.templates.discover_templates", return_value=[])
    def test_start_workflow_sets_pending_selected_tools(
        self, mock_templates, mock_node, mock_sender
    ):
        """Verify default selected tools are stored in pending.selected_tools."""
        handler, ctx = self._make_handler()

        project = MagicMock()
        project.root_path = "/tmp/project"
        project.project_id = "proj_1"
        project.project_name = "test"
        handler._ensure_project = MagicMock(return_value=project)

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject()
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        handler.start_workflow("msg_1", "chat_1", "do code review", project)

        # After start_workflow: orchestrator_agent should be set, but selected_tools not yet
        self.assertEqual(engine.project.pending.orchestrator_agent if engine.project.pending else None, "coco")
        self.assertIsNone(engine.project.pending.selected_tools if engine.project.pending else None)
        # Requirement should be stored
        self.assertEqual(engine.project.pending.requirement if engine.project.pending else None, "do code review")
        # Session key should be set
        self.assertIsNotNone(engine.project.pending.engine_session_key if engine.project.pending else None)
        session_key = engine.project.pending.engine_session_key if engine.project.pending else None

        # Step 1b: Select agent to proceed to tool selection (this sets selected_tools)
        handler._get_root_path = MagicMock(return_value="/tmp/project")
        handler._resolve_project_from_id = MagicMock(return_value=project)
        handler.handle_workflow_select_agent(
            "msg_1b", "chat_1",
            "proj_1",
            {"action": "workflow_select_agent", "agent_type": "coco", "engine_session_key": session_key, "project_id": "proj_1"}
        )

        # After agent selection: pending.selected_tools should be set with default selection
        self.assertIsNotNone(engine.project.pending.selected_tools if engine.project.pending else None)
        self.assertIsInstance(engine.project.pending.selected_tools if engine.project.pending else None, list)
        self.assertGreater(len(engine.project.pending.selected_tools if engine.project.pending else []), 0)
        # Should contain at least "coco" as default
        self.assertIn("coco", engine.project.pending.selected_tools if engine.project.pending else [])

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    @patch("src.feishu.handlers.workflow.WorkflowHandler._generate_script_via_ai")
    def test_handle_workflow_confirm_tools_transitions_state(
        self, mock_gen, mock_sender
    ):
        """Verify handle_workflow_confirm_tools() transitions from AWAITING_TOOL_SELECT to AWAITING_CONFIRM."""
        handler, ctx = self._make_handler()

        # Set up engine in AWAITING_TOOL_SELECT state
        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_TOOL_SELECT,
            pending=PendingConfirmation(
                requirement="do code review",
                initiator_user_id="user_123",
                engine_session_key="valid_session_key",
                selected_tools=["coco", "claude"],
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        project_mock = MagicMock()
        project_mock.root_path = "/tmp/project"
        project_mock.project_id = "proj_1"
        project_mock.project_name = "test"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        # Mock AI generation result
        mock_gen.return_value = (
            "/tmp/project/.ghostap/workflow_scripts/generated_workflow.js",
            {"name": "test-wf", "description": "Test", "phases": [], "tools": ["coco"]},
            False,
        )

        handler.handle_workflow_confirm_tools(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_confirm_tools", "engine_session_key": "valid_session_key"}
        )

        # After confirm_tools, engine should be in AWAITING_ROLE_SELECT (step 3)
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_ROLE_SELECT)
        # Script should NOT be generated yet (happens after role confirmation)
        # After role confirmation, script generation happens
        handler.handle_workflow_confirm_roles_and_generate(
            "msg_2", "chat_1", "proj_1",
            {"action": "workflow_confirm_roles_and_generate", "engine_session_key": "valid_session_key"}
        )
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)
        self.assertIsNotNone(engine.project.pending.script_path if engine.project.pending else None)
        self.assertIsNotNone(engine.project.pending.meta if engine.project.pending else None)
        # _generate_script_via_ai should have been called with selected_tools
        mock_gen.assert_called_once()
        call_args = mock_gen.call_args[0]
        self.assertIn("do code review", call_args)
        # Third arg should be selected_tools
        self.assertEqual(call_args[2], ["coco", "claude"])

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_handle_workflow_confirm_tools_validates_session_key(self, mock_sender):
        """Wrong session key should be rejected."""
        handler, ctx = self._make_handler()

        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_TOOL_SELECT,
            pending=PendingConfirmation(
                requirement="do X",
                initiator_user_id="user_123",
                engine_session_key="correct_key",
                selected_tools=["coco"],
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine

        project_mock = MagicMock()
        project_mock.root_path = "/tmp/project"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_tools(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_confirm_tools", "engine_session_key": "wrong_key"}
        )

        # Should reject with error about session
        handler.reply_card.assert_called_once()
        rejection_card = handler.reply_card.call_args[0][1]
        rejection_title = rejection_card["header"]["title"]["content"]
        self.assertEqual(rejection_title, "会话已过期")
        # State should remain AWAITING_TOOL_SELECT
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)
        # Script path should still be None
        self.assertIsNone(engine.project.pending.script_path if engine.project.pending else None)

    @patch("src.thread.get_current_sender_id", return_value="user_BBB")
    def test_handle_workflow_confirm_tools_validates_initiator(self, mock_sender):
        """Non-initiator should be rejected."""
        handler, ctx = self._make_handler()

        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_TOOL_SELECT,
            pending=PendingConfirmation(
                requirement="do X",
                initiator_user_id="user_AAA",
                engine_session_key="valid_key",
                selected_tools=["coco"],
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine

        project_mock = MagicMock()
        project_mock.root_path = "/tmp/project"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_tools(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_confirm_tools", "engine_session_key": "valid_key"}
        )

        # Should reject with error about initiator
        handler.reply_card.assert_called_once()
        rejection_card = handler.reply_card.call_args[0][1]
        rejection_title = rejection_card["header"]["title"]["content"]
        rejection_msg = rejection_card["body"]["elements"][0]["content"]
        self.assertEqual(rejection_title, "无操作权限")
        self.assertIn("发起者", rejection_msg)
        # State should remain AWAITING_TOOL_SELECT
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_handle_workflow_confirm_tools_requires_at_least_one_tool(self, mock_sender):
        """Empty selected tools should be rejected."""
        handler, ctx = self._make_handler()

        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_TOOL_SELECT,
            pending=PendingConfirmation(
                requirement="do X",
                initiator_user_id="user_123",
                engine_session_key="valid_key",
                selected_tools=[],  # Empty!
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine

        project_mock = MagicMock()
        project_mock.root_path = "/tmp/project"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_tools(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_confirm_tools", "engine_session_key": "valid_key"}
        )

        # Should reject with message about selecting at least one tool
        handler.reply_card.assert_called_once()
        rejection_card = handler.reply_card.call_args[0][1]
        # The error card has body > elements with markdown content
        rejection_msg = ""
        for el in rejection_card.get("body", {}).get("elements", []):
            if isinstance(el, dict) and el.get("tag") == "note":
                for inner in el.get("elements", []):
                    if isinstance(inner, dict) and inner.get("tag") == "plain_text":
                        rejection_msg += inner.get("content", "")
            elif isinstance(el, dict) and el.get("tag") == "markdown":
                rejection_msg += el.get("content", "")
        self.assertIn("至少选择一个工具", rejection_msg)
        # State should remain AWAITING_TOOL_SELECT
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_TOOL_SELECT)


class TestWorkflowRegenerateScript(unittest.TestCase):
    """Tests for handle_workflow_regenerate_script()."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        ctx = MagicMock()
        ctx.workflow_engine_manager = MagicMock()
        ctx.project_manager = MagicMock()
        ctx.progress_reporter = MagicMock()

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = ctx
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.reply_error = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg_card_123")
        handler.update_card = MagicMock(return_value=True)
        handler.add_reaction = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler.get_engine_name = MagicMock(return_value="coco")
        handler.ensure_request_id = MagicMock(return_value="req_123")
        handler._submit_engine_task = MagicMock()

        return handler, ctx

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    @patch("src.feishu.handlers.workflow.WorkflowHandler._generate_script_via_ai")
    def test_handle_workflow_regenerate_script_regenerates(
        self, mock_gen, mock_sender
    ):
        """Verify handle_workflow_regenerate_script() calls _generate_script_via_ai again and updates the card."""
        import tempfile

        handler, ctx = self._make_handler()

        # Create a real temp file for the old script
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write("old script")
        tmp.close()
        old_script_path = tmp.name

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path=old_script_path,
                requirement="do code review",
                meta={"name": "old-wf", "tools": ["coco"]},
                initiator_user_id="user_123",
                engine_session_key="valid_session_key",
                selected_tools=["coco", "claude"],
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine
        ctx.workflow_engine_manager.get_or_create.return_value = engine

        project_mock = MagicMock()
        project_mock.root_path = "/tmp/project"
        project_mock.project_id = "proj_1"
        project_mock.project_name = "test"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        # Mock new AI generation result
        mock_gen.return_value = (
            "/tmp/project/.ghostap/workflow_scripts/regenerated_workflow.js",
            {"name": "regenerated-wf", "description": "Regenerated", "phases": [], "tools": ["coco", "claude"]},
            False,
        )

        handler.handle_workflow_regenerate_script(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_regenerate_script", "engine_session_key": "valid_session_key"}
        )

        # _generate_script_via_ai should have been called again
        self.assertEqual(mock_gen.call_count, 1)
        call_args = mock_gen.call_args[0]
        self.assertEqual(call_args[2], ["coco", "claude"])  # selected_tools passed

        # Should have sent generating card then updated to confirm card
        handler.send_card_to_chat.assert_called_once()
        handler.update_card.assert_called_once()

        # Pending meta should be updated with new script
        self.assertEqual(engine.project.pending.meta["name"] if engine.project.pending and engine.project.pending.meta else None, "regenerated-wf")
        self.assertEqual(engine.project.pending.script_path if engine.project.pending else None, "/tmp/project/.ghostap/workflow_scripts/regenerated_workflow.js")

        # Old script file should have been removed
        self.assertFalse(os.path.exists(old_script_path))

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_handle_workflow_regenerate_script_wrong_state_rejected(self, mock_sender):
        """Only works in AWAITING_CONFIRM state."""
        handler, ctx = self._make_handler()

        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_TOOL_SELECT,  # Wrong state!
            pending=PendingConfirmation(
                requirement="do X",
                initiator_user_id="user_123",
                engine_session_key="valid_key",
                selected_tools=["coco"],
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine

        project_mock = MagicMock()
        project_mock.root_path = "/tmp/project"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_regenerate_script(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_regenerate_script", "engine_session_key": "valid_key"}
        )

        # Should reject with message about wrong state
        handler.reply_card.assert_called_once()
        rejection_card = handler.reply_card.call_args[0][1]
        rejection_title = rejection_card["header"]["title"]["content"]
        self.assertEqual(rejection_title, "状态不匹配")

    @patch("src.thread.get_current_sender_id", return_value="user_BBB")
    def test_handle_workflow_regenerate_script_validates_security(self, mock_sender):
        """Session key and initiator checks."""
        handler, ctx = self._make_handler()

        engine = MagicMock()
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path="/tmp/wf.js",
                requirement="do X",
                meta={"name": "x"},
                initiator_user_id="user_AAA",
                engine_session_key="correct_key",
                selected_tools=["coco"],
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine

        project_mock = MagicMock()
        project_mock.root_path = "/tmp/project"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        # Test 1: Wrong session key
        handler.handle_workflow_regenerate_script(
            "msg_1", "chat_1", "proj_1",
            {"action": "workflow_regenerate_script", "engine_session_key": "wrong_key"}
        )
        handler.reply_card.assert_called_once()
        rejection_card = handler.reply_card.call_args[0][1]
        rejection_title = rejection_card["header"]["title"]["content"]
        self.assertEqual(rejection_title, "会话已过期")

        # Reset mocks
        handler.reply_card.reset_mock()
        handler.reply_text.reset_mock()

        # Test 2: Wrong initiator (with correct session key)
        handler.handle_workflow_regenerate_script(
            "msg_2", "chat_1", "proj_1",
            {"action": "workflow_regenerate_script", "engine_session_key": "correct_key"}
        )
        handler.reply_card.assert_called_once()
        rejection_card = handler.reply_card.call_args[0][1]
        rejection_title = rejection_card["header"]["title"]["content"]
        rejection_msg = rejection_card["body"]["elements"][0]["content"]
        self.assertEqual(rejection_title, "无操作权限")
        self.assertIn("发起者", rejection_msg)


class TestWorkflowToolConsistencyValidation(unittest.TestCase):
    """Tests for tool consistency validation in confirm_start."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        ctx = MagicMock()
        ctx.workflow_engine_manager = MagicMock()
        ctx.project_manager = MagicMock()
        ctx.progress_reporter = MagicMock()

        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = ctx
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.reply_error = MagicMock()
        handler._reply_workflow_error = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg_card_123")
        handler.update_card = MagicMock(return_value=True)
        handler.add_reaction = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp/project")
        handler.get_engine_name = MagicMock(return_value="coco")
        handler.ensure_request_id = MagicMock(return_value="req_123")
        handler._submit_engine_task = MagicMock()

        return handler, ctx

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_confirm_start_validates_tool_consistency(self, mock_sender):
        """When script meta.tools contains tools not in selected_tools, confirmation should be rejected."""
        import hashlib
        import tempfile

        handler, ctx = self._make_handler()

        script_content = (
            "export const meta = {\n"
            "  name: 'x',\n"
            "  description: 'test',\n"
            "  tools: ['coco', 'claude', 'codex'],\n"
            "};\n"
            "\n"
            "export default async function() {\n"
            "  await agent('do work');\n"
            "}\n"
        )
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write(script_content)
        tmp.close()
        script_hash = hashlib.sha256(script_content.encode("utf-8")).hexdigest()

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path=tmp.name,
                requirement="do X",
                meta={"name": "x", "tools": ["coco", "claude", "codex"]},
                initiator_user_id="user_123",
                engine_session_key="valid_key",
                selected_tools=["coco", "claude"],  # codex missing!
                script_hash=script_hash,
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine
        handler._get_root_path = MagicMock(return_value="/tmp/project")

        project_mock = MagicMock()
        project_mock.project_name = "test"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_start(
            "msg_1", "chat_1", "proj_1",
            {"action": WORKFLOW_CONFIRM_START, "engine_session_key": "valid_key"}
        )

        try:
            os.unlink(tmp.name)
        except OSError:
            pass

        # Should reject via the unified _reply_workflow_error surface with
        # "invalid_argument" category; the message must mention the missing
        # tool name.
        handler._reply_workflow_error.assert_called_once()
        call_args = handler._reply_workflow_error.call_args
        call_positional = call_args[0]
        # category is the second positional arg after message_id
        self.assertEqual(call_positional[1], "invalid_argument")
        # the detail kwarg should contain the missing tool name
        detail = call_args[1].get("detail", "") if call_args[1] else ""
        if not detail:
            # fallback: scan all call parts if signature differs
            detail = " ".join(str(c) for c in call_positional[2:])
        self.assertIn("codex", detail)
        # Should NOT have submitted the engine task
        handler._submit_engine_task.assert_not_called()
        # Status should still be AWAITING_CONFIRM
        self.assertEqual(engine.project.status, WorkflowStatus.AWAITING_CONFIRM)

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_confirm_start_allows_subset_tools(self, mock_sender):
        """When script tools are a subset of selected tools, confirmation should proceed."""
        import hashlib
        import tempfile

        handler, ctx = self._make_handler()

        script_content = (
            "export const meta = {\n"
            "  name: 'x',\n"
            "  description: 'test',\n"
            "  tools: ['coco'],\n"
            "};\n"
            "\n"
            "export default async function() {\n"
            "  await agent('do work');\n"
            "}\n"
        )
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write(script_content)
        tmp.close()
        script_hash = hashlib.sha256(script_content.encode("utf-8")).hexdigest()

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path=tmp.name,
                requirement="do X",
                meta={"name": "x", "tools": ["coco"]},
                initiator_user_id="user_123",
                engine_session_key="valid_key",
                selected_tools=["coco", "claude", "codex"],  # superset
                script_hash=script_hash,
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine
        handler._get_root_path = MagicMock(return_value="/tmp/project")

        project_mock = MagicMock()
        project_mock.project_name = "test"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_start(
            "msg_1", "chat_1", "proj_1",
            {"action": WORKFLOW_CONFIRM_START, "engine_session_key": "valid_key"}
        )

        try:
            os.unlink(tmp.name)
        except OSError:
            pass

        # Should NOT reject - subset is allowed
        handler.reply_error.assert_not_called()
        # Should have submitted the engine task
        handler._submit_engine_task.assert_called_once()
        # Pending state should be cleared
        self.assertIsNone(engine.project.pending.script_path if engine.project.pending else None)

    @patch("src.thread.get_current_sender_id", return_value="user_123")
    def test_confirm_start_allows_empty_script_tools(self, mock_sender):
        """When script has no tools declared, validation should pass."""
        import hashlib
        import tempfile

        handler, ctx = self._make_handler()

        # Script with meta but no tools array.
        script_content = (
            "export const meta = {\n"
            "  name: 'x',\n"
            "  description: 'test',\n"
            "};\n"
            "\n"
            "export default async function() {\n"
            "  await agent('do work');\n"
            "}\n"
        )
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
        tmp.write(script_content)
        tmp.close()
        script_hash = hashlib.sha256(script_content.encode("utf-8")).hexdigest()

        engine = MagicMock()
        engine.is_running = False
        engine.project = WorkflowProject(
            status=WorkflowStatus.AWAITING_CONFIRM,
            pending=PendingConfirmation(
                script_path=tmp.name,
                requirement="do X",
                meta={"name": "x", "tools": []},
                initiator_user_id="user_123",
                engine_session_key="valid_key",
                selected_tools=["coco"],
                script_hash=script_hash,
            ),
        )
        ctx.workflow_engine_manager.get.return_value = engine
        handler._get_root_path = MagicMock(return_value="/tmp/project")

        project_mock = MagicMock()
        project_mock.project_name = "test"
        handler._resolve_project_from_id = MagicMock(return_value=project_mock)

        handler.handle_workflow_confirm_start(
            "msg_1", "chat_1", "proj_1",
            {"action": WORKFLOW_CONFIRM_START, "engine_session_key": "valid_key"}
        )

        try:
            os.unlink(tmp.name)
        except OSError:
            pass

        # Should NOT reject - empty script tools is allowed
        handler.reply_error.assert_not_called()
        # Should have submitted the engine task
        handler._submit_engine_task.assert_called_once()


class TestConfirmCardToolDistinction(unittest.TestCase):
    """Tests for confirm card showing script vs allowed tools distinction."""

    def _make_handler(self):
        from src.feishu.handlers.workflow import WorkflowHandler

        ctx = MagicMock()
        handler = WorkflowHandler.__new__(WorkflowHandler)
        handler.ctx = ctx
        return handler

    def _get_elements(self, card: dict) -> list:
        body = card.get("body", card)
        return body.get("elements", card.get("elements", []))

    def _extract_all_text(self, card: dict) -> str:
        """Extract all text content from card elements, including button labels."""

        def _extract(element: dict) -> str:
            tag = element.get("tag")
            if tag == "markdown":
                return " " + element.get("content", "")
            if tag == "plain_text":
                return " " + element.get("content", "")
            if tag == "button":
                text = element.get("text", {})
                if isinstance(text, dict):
                    return " " + text.get("content", "")
                return ""
            # Recurse into any container element that has an "elements" list
            child_text = ""
            for sub in element.get("elements", []):
                if isinstance(sub, dict):
                    child_text += _extract(sub)
            # Column sets nest columns which nest elements; make sure we cover both.
            for column in element.get("columns", []):
                if isinstance(column, dict):
                    child_text += _extract(column)
            return child_text

        return "".join(_extract(el) for el in self._get_elements(card))

    def _extract_all_actions(self, card: dict) -> list:
        """Extract all action buttons from card, including inside collapsible panels."""
        elements = self._get_elements(card)
        all_actions = []

        def _extract_from(el: dict) -> None:
            if el.get("tag") == "action":
                for action in el.get("actions", []):
                    all_actions.append(action)
            if el.get("tag") == "column_set":
                for col in el.get("columns", []):
                    for col_el in col.get("elements", []):
                        if col_el.get("tag") == "button":
                            all_actions.append(col_el)
            # Recurse into collapsible panels
            if el.get("tag") == "collapsible_panel":
                for sub in el.get("elements", []):
                    if isinstance(sub, dict):
                        _extract_from(sub)

        for el in elements:
            if isinstance(el, dict):
                _extract_from(el)
        return all_actions

    def test_confirm_card_shows_script_vs_allowed_tools(self):
        """The confirm card should show both 'script planned tools' and 'allowed tools' sections."""
        handler = self._make_handler()
        meta = {
            "name": "test-wf",
            "description": "Test workflow",
            "phases": [{"title": "Plan", "detail": "Make a plan"}],
            "tools": ["coco", "claude"],
        }

        card = handler._build_confirm_card(
            meta=meta,
            requirement="do code review",
            engine_session_key="key_123",
            chat_id="chat_1",
            project_id="proj_1",
            selected_tools=["coco", "claude", "codex"],
            script_content="",
        )

        all_text = self._extract_all_text(card)

        # Should show script planned tools section
        self.assertIn("脚本计划使用", all_text)
        self.assertIn("`coco`", all_text)
        self.assertIn("`claude`", all_text)

        # Should show allowed tools section
        self.assertIn("允许执行的工具", all_text)
        # Should show codex as allowed even though not in script tools
        self.assertIn("codex", all_text)

    def test_confirm_card_shows_mismatch_warning(self):
        """When there's a tool mismatch, the card should show a warning."""
        handler = self._make_handler()
        meta = {
            "name": "test-wf",
            "description": "Test",
            "phases": [],
            "tools": ["coco", "claude", "codex"],  # codex not in selected_tools
        }

        card = handler._build_confirm_card(
            meta=meta,
            requirement="task",
            engine_session_key="key_1",
            chat_id="chat_1",
            project_id="proj_1",
            selected_tools=["coco", "claude"],  # codex missing!
            script_content="",
        )

        all_text = self._extract_all_text(card)

        # Should show mismatch warning with missing tools highlighted
        self.assertIn("脚本需要这些工具但尚未启用", all_text)
        self.assertIn("`codex`", all_text)
        # Should mention both fill-in and back-to-tools paths
        self.assertIn("一键补齐缺失工具", all_text)
        self.assertIn("返回工具选择", all_text)
        # Regenerate option also present
        self.assertIn("重新生成编排", all_text)

    def test_confirm_card_has_regenerate_button(self):
        """The card should include a '重新生成编排' button with WORKFLOW_REGENERATE_SCRIPT action."""
        from src.card.actions.dispatch import WORKFLOW_REGENERATE_SCRIPT

        handler = self._make_handler()
        meta = {
            "name": "test-wf",
            "description": "Test",
            "phases": [],
            "tools": ["coco"],
        }

        card = handler._build_confirm_card(
            meta=meta,
            requirement="task",
            engine_session_key="key_1",
            chat_id="chat_1",
            project_id="proj_1",
            selected_tools=["coco"],
            script_content="",
        )

        all_actions = self._extract_all_actions(card)
        action_values = []
        for a in all_actions:
            val = a.get("value", {})
            if isinstance(val, dict):
                action_values.append(val.get("action", ""))
            # Also check button text
            btn_text = ""
            text_obj = a.get("text", {})
            if isinstance(text_obj, dict):
                btn_text = text_obj.get("content", "")

        # Should have regenerate button
        self.assertIn(WORKFLOW_REGENERATE_SCRIPT, action_values)

        # Find the regenerate button and check its text
        regenerate_btn = None
        for a in all_actions:
            val = a.get("value", {})
            if isinstance(val, dict) and val.get("action") == WORKFLOW_REGENERATE_SCRIPT:
                regenerate_btn = a
                break
        self.assertIsNotNone(regenerate_btn)
        btn_text = regenerate_btn.get("text", {}).get("content", "")
        self.assertIn("重新生成编排", btn_text)


if __name__ == "__main__":
    unittest.main()
