"""Regression tests for Workflow state consistency across runs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.feishu.handlers.workflow import WorkflowHandler
from src.workflow_engine.engine import WorkflowEngine
from src.workflow_engine.models import PendingConfirmation, WorkflowProject, WorkflowStatus
from src.workflow_engine.selection_flow import SelectionFlowController


class _Project:
    def __init__(self, root_path: str) -> None:
        self.project_id = "proj_1"
        self.project_name = "ghostAp"
        self.root_path = root_path


def _walk_card_text(node):
    if isinstance(node, dict):
        text = node.get("text")
        if isinstance(text, dict):
            yield str(text.get("content", ""))
        if "content" in node:
            yield str(node.get("content", ""))
        for value in node.values():
            yield from _walk_card_text(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_card_text(item)


def test_workflow_engine_clears_stale_cancel_event_before_new_run(tmp_path):
    script_path = tmp_path / "workflow.js"
    script_path.write_text(
        """
export const meta = { name: "smoke", description: "", phases: [], tools: [] };
export default async function workflow() { return "ok"; }
""",
        encoding="utf-8",
    )

    class FakeBridge:
        def __init__(self, *args, cancel_event, **kwargs):
            self.cancel_event = cancel_event

        @staticmethod
        def check_node_available():
            return True

        def start(self):
            return None

        def run(self):
            if self.cancel_event.is_set():
                raise RuntimeError("Workflow cancelled")
            return "ok"

        def stop(self):
            return None

    engine = WorkflowEngine(chat_id="chat_1", root_path=str(tmp_path))
    engine.cancel_event.set()

    with patch("src.workflow_engine.engine.RuntimeBridge", FakeBridge):
        project = engine.execute_workflow("run cleanly", str(script_path))

    assert project.status == WorkflowStatus.COMPLETED
    assert not engine.cancel_event.is_set()


def test_workflow_engine_preserves_cancelled_terminal_status(tmp_path):
    script_path = tmp_path / "workflow.js"
    script_path.write_text(
        """
export const meta = { name: "cancel", description: "", phases: [], tools: [] };
export default async function workflow() { return "never"; }
""",
        encoding="utf-8",
    )

    class FakeBridge:
        def __init__(self, *args, cancel_event, **kwargs):
            self.cancel_event = cancel_event

        @staticmethod
        def check_node_available():
            return True

        def start(self):
            self.cancel_event.set()

        def run(self):
            raise RuntimeError("Workflow cancelled")

        def stop(self):
            return None

    engine = WorkflowEngine(chat_id="chat_1", root_path=str(tmp_path))

    with patch("src.workflow_engine.engine.RuntimeBridge", FakeBridge):
        project = engine.execute_workflow("cancel cleanly", str(script_path))

    assert project.status == WorkflowStatus.CANCELLED
    assert project.error == "Workflow cancelled"


def test_fresh_workflow_agent_selection_does_not_restore_previous_selection(tmp_path):
    project = _Project(str(tmp_path))

    stale = SelectionFlowController(step=2)
    stale.add_or_update_selection(
        {
            "tool_name": "coco",
            "display_name": "Coco",
            "model_name": "old-model",
            "supports_model": True,
        },
        is_review=False,
    )
    project._wf_selection_snapshot = stale.snapshot()

    engine = WorkflowEngine(chat_id="chat_1", root_path=str(tmp_path))

    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = MagicMock()
    handler.ctx.workflow_engine_manager.get_or_create.return_value = engine
    handler.get_engine_name = MagicMock(return_value="Coco")
    handler._resolve_tool_lists = MagicMock(
        return_value=({"coco": "Coco tool"}, ["coco"], [], ["coco"])
    )
    handler._get_workflow_models_for_tool = MagicMock(return_value=[])
    handler.send_card_to_chat = MagicMock(return_value="card_msg_1")

    with patch("src.thread.get_current_sender_id", return_value="user_1"):
        handler._show_agent_selection_card(
            chat_id="chat_1",
            requirement="build something new",
            project=project,
            root_path=str(tmp_path),
        )

    sent_card = handler.send_card_to_chat.call_args.args[1]
    rendered_text = "\n".join(_walk_card_text(sent_card))

    assert "old-model" not in rendered_text
    assert "**已选**" not in rendered_text
    assert project._wf_selection_snapshot["orchestrator_selections"] == {}
    assert project._wf_selection_snapshot["review_selections"] == {}


def test_old_script_generation_task_does_not_apply_after_session_changes(tmp_path):
    engine = WorkflowEngine(chat_id="chat_1", root_path=str(tmp_path))
    engine._project = WorkflowProject(
        status=WorkflowStatus.GENERATING_SCRIPT,
        pending=PendingConfirmation(
            requirement="old task",
            engine_session_key="old_session",
            initiator_user_id="user_1",
        ),
    )
    project = _Project(str(tmp_path))

    captured = {}

    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = MagicMock()
    handler.ctx.workflow_engine_manager.get.return_value = engine
    handler._submit_engine_task = MagicMock(
        side_effect=lambda fn, *args, **kwargs: captured.setdefault("task", fn)
    )
    handler._generate_and_show_confirm_card = MagicMock()
    handler._reply_workflow_error = MagicMock()

    handler._schedule_generate_and_show_confirm_card(
        message_id="old_card",
        chat_id="chat_1",
        requirement="old task",
        project=project,
        root_path=str(tmp_path),
        selected_tools=["coco"],
        engine=engine,
    )

    engine.project.status = WorkflowStatus.AWAITING_AGENT_SELECT
    engine.project.pending = PendingConfirmation(
        requirement="new task",
        engine_session_key="new_session",
        initiator_user_id="user_1",
    )

    captured["task"]()

    handler._generate_and_show_confirm_card.assert_not_called()
    assert engine.project.status == WorkflowStatus.AWAITING_AGENT_SELECT
    assert engine.project.pending.requirement == "new task"


def test_script_generation_result_is_ignored_if_session_changes_mid_generation(tmp_path):
    script_path = tmp_path / "generated.js"
    script_path.write_text(
        """
export const meta = { name: "generated", description: "", phases: [], tools: ["coco"] };
export default async function workflow() { return await agent("do it", { tool: "coco" }); }
""",
        encoding="utf-8",
    )

    engine = WorkflowEngine(chat_id="chat_1", root_path=str(tmp_path))
    engine._project = WorkflowProject(
        status=WorkflowStatus.GENERATING_SCRIPT,
        pending=PendingConfirmation(
            requirement="old task",
            engine_session_key="old_session",
            initiator_user_id="user_1",
            selected_tools=["coco"],
        ),
    )
    project = _Project(str(tmp_path))

    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = MagicMock()
    handler.ctx.workflow_engine_manager.get_or_create.return_value = engine
    handler.get_engine_name = MagicMock(return_value="Coco")
    handler.send_card_to_chat = MagicMock(return_value="generating_card")
    handler.update_card = MagicMock()
    handler._replace_or_send_workflow_card = MagicMock()

    def generation_finishes_after_cancel(*args, **kwargs):
        engine.project.status = WorkflowStatus.IDLE
        engine.project.pending = None
        return str(script_path), {"name": "generated", "tools": ["coco"]}, False

    handler._generate_script_via_ai = MagicMock(side_effect=generation_finishes_after_cancel)

    with (
        patch("src.thread.get_current_sender_id", return_value="user_1"),
        patch("src.workflow_engine.templates.discover_templates", return_value=[]),
    ):
        handler._generate_and_show_confirm_card(
            message_id="old_card",
            chat_id="chat_1",
            requirement="old task",
            project=project,
            root_path=str(tmp_path),
            selected_tools=["coco"],
            expected_session_key="old_session",
        )

    assert engine.project.status == WorkflowStatus.IDLE
    assert engine.project.pending is None
    handler._replace_or_send_workflow_card.assert_not_called()


def test_generating_script_state_blocks_new_workflow(tmp_path):
    project = _Project(str(tmp_path))
    engine = WorkflowEngine(chat_id="chat_1", root_path=str(tmp_path))
    engine._project = WorkflowProject(
        status=WorkflowStatus.GENERATING_SCRIPT,
        pending=PendingConfirmation(
            requirement="old task",
            engine_session_key="old_session",
            initiator_user_id="user_1",
        ),
    )

    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = MagicMock()
    handler.ctx.workflow_engine_manager.get.return_value = engine
    handler.ctx.workflow_engine_manager.get_or_create.return_value = engine
    handler.add_reaction = MagicMock()
    handler._ensure_topic_engine_context = MagicMock()
    handler._show_agent_selection_card = MagicMock()
    handler._reply_workflow_error = MagicMock()
    handler._ensure_project = MagicMock(return_value=project)

    with (
        patch("src.thread.get_current_sender_id", return_value="user_1"),
        patch("src.workflow_engine.bridge.RuntimeBridge.check_node_available", return_value=True),
        patch("src.workflow_engine.templates.discover_templates", return_value=[]),
    ):
        handler.start_workflow("msg_1", "chat_1", "new task", project)

    handler._reply_workflow_error.assert_called_once()
    assert handler._reply_workflow_error.call_args.args[1] == "invalid_state"
    handler._show_agent_selection_card.assert_not_called()


def test_stop_workflow_clears_generating_script_state(tmp_path):
    project = _Project(str(tmp_path))
    engine = WorkflowEngine(chat_id="chat_1", root_path=str(tmp_path))
    engine._project = WorkflowProject(
        status=WorkflowStatus.GENERATING_SCRIPT,
        pending=PendingConfirmation(
            requirement="old task",
            engine_session_key="old_session",
            initiator_user_id="user_1",
        ),
    )

    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = MagicMock()
    handler.ctx.workflow_engine_manager.get.return_value = engine
    handler.ctx.settings.admin_user_ids = []
    handler.reply_text = MagicMock()
    handler._reply_workflow_error = MagicMock()

    with patch("src.thread.get_current_sender_id", return_value="user_1"):
        handler.stop_workflow("msg_1", "chat_1", project)

    assert engine.project.status == WorkflowStatus.IDLE
    assert engine.project.pending is None
    handler.reply_text.assert_called_once()
    handler._reply_workflow_error.assert_not_called()
