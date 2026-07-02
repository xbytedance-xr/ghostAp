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


import threading

from src.workflow_engine.models import AgentStatus, WorkflowMetrics
from src.workflow_engine.state_manager import WorkflowStateManager


def _make_state_manager():
    """Create a minimal state manager with a single running agent."""
    project = WorkflowProject(
        workflow_id="test",
        status=WorkflowStatus.RUNNING,
        metrics=WorkflowMetrics(),
    )
    sm = WorkflowStateManager(project)
    sm.on_phase_changed("phase1")
    return sm


class TestStateManagerStickyTerminal:
    """Agent terminal states are final — no transition may overwrite another."""

    def test_done_not_overwritten_by_failed(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_done(label, {"token_usage": 10, "duration_s": 1.0})

        snap = sm.snapshot()
        agent = snap.phases[0].agents[0]
        assert agent.status == AgentStatus.DONE
        assert snap.metrics.completed_agents == 1
        assert snap.metrics.failed_agents == 0

        sm.on_agent_failed(label, "some error")

        snap = sm.snapshot()
        agent = snap.phases[0].agents[0]
        assert agent.status == AgentStatus.DONE, "DONE must not be overwritten by FAILED"
        assert agent.error is None or agent.error == ""
        assert snap.metrics.completed_agents == 1
        assert snap.metrics.failed_agents == 0, "failed_agents must not increment for already-done agent"

    def test_done_not_overwritten_by_cancelled(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_done(label, {"token_usage": 10, "duration_s": 1.0})

        sm.on_agent_aborted(label, "race loser")

        snap = sm.snapshot()
        agent = snap.phases[0].agents[0]
        assert agent.status == AgentStatus.DONE, "DONE must not be overwritten by CANCELLED"
        assert snap.metrics.completed_agents == 1

    def test_failed_not_overwritten_by_done(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_failed(label, "timeout")

        snap = sm.snapshot()
        assert snap.phases[0].agents[0].status == AgentStatus.FAILED
        assert snap.metrics.failed_agents == 1

        sm.on_agent_done(label, {"token_usage": 5, "duration_s": 2.0})

        snap = sm.snapshot()
        agent = snap.phases[0].agents[0]
        assert agent.status == AgentStatus.FAILED, "FAILED must not be overwritten by DONE"
        assert agent.error == "timeout"
        assert snap.metrics.failed_agents == 1
        assert snap.metrics.total_tokens == 0, "token_usage from late done must not be counted"

    def test_failed_not_overwritten_by_cancelled(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_failed(label, "connection error")

        sm.on_agent_aborted(label, "race loser")

        snap = sm.snapshot()
        agent = snap.phases[0].agents[0]
        assert agent.status == AgentStatus.FAILED, "FAILED must not be overwritten by CANCELLED"
        assert snap.metrics.failed_agents == 1

    def test_cancelled_not_overwritten_by_done(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_aborted(label, "race loser")

        snap = sm.snapshot()
        assert snap.phases[0].agents[0].status == AgentStatus.CANCELLED
        assert snap.metrics.completed_agents == 1

        sm.on_agent_done(label, {"token_usage": 10, "duration_s": 1.0})

        snap = sm.snapshot()
        agent = snap.phases[0].agents[0]
        assert agent.status == AgentStatus.CANCELLED, "CANCELLED must not be overwritten by DONE"
        assert snap.metrics.completed_agents == 1
        assert snap.metrics.total_tokens == 0

    def test_cancelled_not_overwritten_by_failed(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_aborted(label, "race loser")

        sm.on_agent_failed(label, "some error")

        snap = sm.snapshot()
        agent = snap.phases[0].agents[0]
        assert agent.status == AgentStatus.CANCELLED, "CANCELLED must not be overwritten by FAILED"
        assert snap.metrics.failed_agents == 0

    def test_done_idempotent(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_done(label, {"token_usage": 10, "duration_s": 1.0})
        sm.on_agent_done(label, {"token_usage": 20, "duration_s": 2.0})

        snap = sm.snapshot()
        assert snap.metrics.completed_agents == 1, "completed_agents must not double-count"
        assert snap.metrics.total_tokens == 10, "second done must not overwrite token count"

    def test_failed_idempotent(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_failed(label, "error1")
        sm.on_agent_failed(label, "error2")

        snap = sm.snapshot()
        assert snap.metrics.failed_agents == 1, "failed_agents must not double-count"
        assert snap.metrics.completed_agents == 1

    def test_aborted_idempotent(self):
        sm = _make_state_manager()
        label = sm.on_agent_started("agent1", "coco", "phase1")
        sm.on_agent_aborted(label, "reason1")
        sm.on_agent_aborted(label, "reason2")

        snap = sm.snapshot()
        assert snap.metrics.completed_agents == 1, "completed_agents must not double-count on abort"

    def test_workflow_failed_overwritten_by_cancelled(self):
        """User-initiated cancel takes precedence over a runtime failure.

        When the user stops a failing workflow, the final status should be
        CANCELLED — the user's explicit action takes precedence over the
        failure that was in progress.
        """
        sm = _make_state_manager()
        sm.on_workflow_failed("fatal error")

        sm.on_workflow_cancelled("user cancelled")

        snap = sm.snapshot()
        assert snap.status == WorkflowStatus.CANCELLED, (
            "FAILED workflow should be overwritten by CANCELLED "
            "(user stop takes precedence)"
        )
        assert snap.error == "user cancelled"


class TestStateManagerMetricsAtomicity:
    """Metrics counters must be consistent even under concurrent updates."""

    def test_concurrent_done_no_double_count(self):
        sm = _make_state_manager()
        labels = [sm.on_agent_started(f"agent{i}", "coco", "phase1") for i in range(20)]
        barrier = threading.Barrier(20)

        def mark_done(label):
            barrier.wait()
            sm.on_agent_done(label, {"token_usage": 5, "duration_s": 0.1})

        threads = [threading.Thread(target=mark_done, args=(lbl,)) for lbl in labels]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        snap = sm.snapshot()
        assert snap.metrics.total_agents == 20
        assert snap.metrics.completed_agents == 20, (
            f"completed_agents must be exactly 20, got {snap.metrics.completed_agents}"
        )
        assert snap.metrics.failed_agents == 0
        assert snap.metrics.total_tokens == 100  # 20 * 5

    def test_concurrent_mixed_statuses_consistent(self):
        """Concurrent done/failed/abort calls must produce consistent totals."""
        sm = _make_state_manager()
        n_done = 15
        n_failed = 10
        n_aborted = 5
        total = n_done + n_failed + n_aborted

        done_labels = [sm.on_agent_started(f"d{i}", "coco", "phase1") for i in range(n_done)]
        failed_labels = [sm.on_agent_started(f"f{i}", "coco", "phase1") for i in range(n_failed)]
        aborted_labels = [sm.on_agent_started(f"a{i}", "coco", "phase1") for i in range(n_aborted)]

        barrier = threading.Barrier(total)
        threads = []

        for lbl in done_labels:
            def _d(l=lbl):
                barrier.wait()
                sm.on_agent_done(l, {"token_usage": 2, "duration_s": 0.1})
            threads.append(threading.Thread(target=_d))

        for lbl in failed_labels:
            def _f(l=lbl):
                barrier.wait()
                sm.on_agent_failed(l, "fail")
            threads.append(threading.Thread(target=_f))

        for lbl in aborted_labels:
            def _a(l=lbl):
                barrier.wait()
                sm.on_agent_aborted(l, "abort")
            threads.append(threading.Thread(target=_a))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        snap = sm.snapshot()
        assert snap.metrics.total_agents == total
        assert snap.metrics.completed_agents == total, (
            f"all agents must be terminal, completed_agents={snap.metrics.completed_agents}"
        )
        assert snap.metrics.failed_agents == n_failed
        # done + cached count: n_done agents
        done_count = sum(
            1 for ph in snap.phases for a in ph.agents
            if a.status == AgentStatus.DONE or a.status == AgentStatus.CACHED
        )
        assert done_count == n_done
        assert snap.metrics.total_tokens == n_done * 2
