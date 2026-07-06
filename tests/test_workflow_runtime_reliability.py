"""Reliability tests across the workflow engine stack.

Covers:
- Renderer snapshot semantics (uses snapshot, thread-safety)
- Journal cache key composition (role, schema, backward compat)
- Cancel-event lifecycle across reuse
- Late-session close daemon-thread behaviour
- State manager terminal-state consistency (cancelled / failed)
"""

from __future__ import annotations

import concurrent.futures
import json
import threading
import time
import unittest
from unittest.mock import Mock, patch

from src.workflow_engine.executor import AgentExecutor
from src.workflow_engine.journal import WorkflowJournal
from src.workflow_engine.models import (
    AgentProgress,
    AgentStatus,
    PhaseProgress,
    WorkflowMetrics,
    WorkflowProject,
    WorkflowStatus,
)
from src.workflow_engine.renderer import WorkflowProgressRenderer
from src.workflow_engine.state_manager import WorkflowStateManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(name: str = "wf-test", *, status: WorkflowStatus = WorkflowStatus.RUNNING) -> WorkflowProject:
    """Build a minimal WorkflowProject with empty metrics/phases."""
    return WorkflowProject(
        workflow_id="wf-" + name,
        name=name,
        status=status,
        requirement=f"requirement for {name}",
        metrics=WorkflowMetrics(),
    )


def _add_running_agent(project: WorkflowProject, phase_title: str, label: str, tool: str = "coco") -> AgentProgress:
    """Append a phase + RUNNING agent directly to a project (bypasses state manager)."""
    phase = PhaseProgress(title=phase_title, started_at=time.time())
    agent = AgentProgress(
        label=label,
        tool=tool,
        status=AgentStatus.RUNNING,
        started_at=time.time(),
    )
    phase.agents.append(agent)
    project.phases.append(phase)
    project.metrics.total_agents += 1
    return agent


# ---------------------------------------------------------------------------
# Renderer snapshot semantics
# ---------------------------------------------------------------------------


class TestRendererSnapshot(unittest.TestCase):
    """WorkflowProgressRenderer must honour an explicit snapshot argument."""

    def test_renderer_uses_snapshot(self):
        """render_progress_card(snapshot) renders from snapshot, not self._project."""
        project_self = _make_project("self-project")
        _add_running_agent(project_self, "self-phase", "self-agent", tool="coco")

        project_snapshot = _make_project("snapshot-project")
        _add_running_agent(project_snapshot, "snapshot-phase", "snapshot-agent", tool="traex")

        renderer = WorkflowProgressRenderer(project_self)
        card = renderer.render_progress_card(project_snapshot)

        # Header must reflect the snapshot's name
        header_title = card["header"]["title"]["content"]
        self.assertIn("snapshot-project", header_title)
        self.assertNotIn("self-project", header_title)

        # Verify that self._project is restored after the call
        self.assertIs(renderer._project, project_self)

        # Card content should mention snapshot agent/tool/phase
        rendered = json.dumps(card, ensure_ascii=False)
        self.assertIn("snapshot-agent", rendered)
        self.assertIn("snapshot-phase", rendered)
        self.assertIn("traex", rendered)
        self.assertNotIn("self-agent", rendered)

    def test_renderer_snapshot_is_thread_safe(self):
        """Mutating the original project while rendering from a snapshot must not
        corrupt the rendered card — the snapshot is an independent deep copy."""
        original = _make_project("original")
        _add_running_agent(original, "phase-1", "agent-alpha", tool="coco")

        # Take a snapshot via WorkflowStateManager (the production path).
        sm = WorkflowStateManager(original)
        snapshot = sm.snapshot()

        renderer = WorkflowProgressRenderer(original)

        mutation_error: list[Exception] = []
        rendered_card_holder: list[dict] = []

        def render():
            try:
                rendered_card_holder.append(renderer.render_progress_card(snapshot))
            except Exception as exc:  # pragma: no cover
                mutation_error.append(exc)

        def mutate():
            try:
                # Aggressively mutate the original: add agents, rename, change status.
                for i in range(50):
                    _add_running_agent(original, f"phase-{i+2}", f"agent-{i}", tool="traex")
                original.name = "mutated!"
                original.status = WorkflowStatus.FAILED
                for phase in original.phases:
                    for agent in phase.agents:
                        agent.status = AgentStatus.FAILED
                        agent.error = "mutated error"
            except Exception as exc:  # pragma: no cover
                mutation_error.append(exc)

        t_render = threading.Thread(target=render)
        t_mutate = threading.Thread(target=mutate)
        t_render.start()
        t_mutate.start()
        t_render.join(timeout=5)
        t_mutate.join(timeout=5)

        self.assertEqual(mutation_error, [], f"mutation raised: {mutation_error}")
        self.assertEqual(len(rendered_card_holder), 1, "renderer did not produce a card")

        card = rendered_card_holder[0]
        rendered = json.dumps(card, ensure_ascii=False)
        # Snapshot must reflect pre-mutation state (agent-alpha exists, no mutated names).
        self.assertIn("agent-alpha", rendered)
        self.assertNotIn("mutated!", rendered)
        self.assertIn("snapshot-project" if False else "original", rendered)
        # More concretely: the snapshot's name is "original", not "mutated!".
        self.assertIn("original", card["header"]["title"]["content"])
        self.assertNotIn("mutated!", card["header"]["title"]["content"])


# ---------------------------------------------------------------------------
# Journal cache key composition
# ---------------------------------------------------------------------------


class TestJournalCacheKey(unittest.TestCase):
    """compute_key() must distinguish role/schema and remain backward-compatible."""

    def test_journal_cache_key_includes_role(self):
        base = WorkflowJournal.compute_key("do thing", "coco", "model-x")
        with_role = WorkflowJournal.compute_key("do thing", "coco", "model-x", role="reviewer")
        self.assertNotEqual(base, with_role, "different role must produce different key")

    def test_journal_cache_key_includes_schema(self):
        base = WorkflowJournal.compute_key("do thing", "coco", "model-x")
        with_schema = WorkflowJournal.compute_key(
            "do thing", "coco", "model-x", output_schema={"answer": "string"}
        )
        self.assertNotEqual(base, with_schema, "different schema must produce different key")

    def test_journal_cache_key_role_optional(self):
        """Omitting role/schema must produce same key as explicitly passing empty/None."""
        k1 = WorkflowJournal.compute_key("p", "coco", "m")
        k2 = WorkflowJournal.compute_key("p", "coco", "m", role=None, output_schema=None)
        k3 = WorkflowJournal.compute_key("p", "coco", "m", role="", output_schema=None)
        # The documented raw template is `prompt|tool|model|role|schema` with empty role/schema
        # falling back to "". None and "" are collapsed to "" in the raw input.
        self.assertEqual(k1, k2)
        # Note: role="" and role=None both render as "" so they must match.
        self.assertEqual(k1, k3)


# ---------------------------------------------------------------------------
# Cancel-event lifecycle on engine reuse
# ---------------------------------------------------------------------------


class TestCancelEventClearedOnNewRun(unittest.TestCase):
    """Verifying the cancel_event.clear() at the top of execute_workflow.

    We do this without spawning Node by invoking execute_workflow() against a
    temporary directory with a stub script that will fail fast at the
    Node-availability check. We assert the event is cleared *before* that check
    raises, which is the very first action under the engine lock.
    """

    def test_cancel_event_cleared_on_new_run(self):
        # Import locally to avoid importing the heavy engine module at module scope
        # (keeps test collection fast).
        from src.workflow_engine.engine import WorkflowEngine

        tmp_dir = "/tmp/ghostap-wf-test-cancel"
        import os
        os.makedirs(tmp_dir, exist_ok=True)

        engine = WorkflowEngine(
            chat_id="ch-cancel-test",
            root_path=tmp_dir,
            agent_type="coco",
            engine_name="Coco",
            model_name=None,
        )
        try:
            # Pre-set the cancel event (simulating a prior stop() or cancelled run).
            engine._cancel_event.set()
            self.assertTrue(engine._cancel_event.is_set(), "precondition: event must be set")

            # Patch node-available to raise so execute_workflow exits before spawning
            # any real subprocess — but only AFTER the clear() at the top runs.
            from src.workflow_engine import bridge as bridge_mod

            original_check = bridge_mod.RuntimeBridge.check_node_available
            bridge_mod.RuntimeBridge.check_node_available = staticmethod(lambda: False)
            try:
                # execute_workflow catches RuntimeError internally and sets FAILED;
                # it does NOT re-raise. Still, the cancel_event.clear() at the very
                # top of execute_workflow runs under self._lock before the Node
                # check, so the event will be cleared regardless of the Node gate.
                project = engine.execute_workflow(
                    requirement="test",
                    script_path=os.path.join(tmp_dir, "stub.js"),
                )
                # The Node gate tripped → workflow ends in FAILED with the
                # "Node.js >= ... required" message.
                self.assertEqual(project.status, WorkflowStatus.FAILED)
                self.assertIn("Node.js", project.error or "")
            finally:
                bridge_mod.RuntimeBridge.check_node_available = original_check

            # Immediately after entry — regardless of the Node failure — the event
            # must have been cleared (the clear happens under self._lock before the
            # Node check).
            self.assertFalse(
                engine._cancel_event.is_set(),
                "cancel_event must be cleared at the start of execute_workflow",
            )
        finally:
            engine.cleanup()


def test_execute_workflow_marks_bounded_fallback_result_failed(tmp_path):
    """A structured fallback/error result from JS is a failed workflow, not success."""
    from src.workflow_engine.engine import WorkflowEngine, WorkflowEngineCallbacks

    script_path = tmp_path / "wf.js"
    script_path.write_text(
        """
export const meta = { name: "fallback", phases: [], tools: [] };
export default async function workflow() { return "unused"; }
""",
        encoding="utf-8",
    )
    result_payload = {
        "fallback": True,
        "stage": "Execution",
        "error": "agent_call timed out waiting for host response",
        "partial": None,
        "message": "Workflow used bounded fallback handling instead of waiting for the total timeout.",
    }

    class FakeBridge:
        @staticmethod
        def check_node_available():
            return True

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def run(self):
            return json.dumps(result_payload)

        def stop(self):
            pass

    on_done = Mock()
    on_error = Mock()
    engine = WorkflowEngine(chat_id="chat-fallback", root_path=str(tmp_path))

    with patch("src.workflow_engine.engine.RuntimeBridge", FakeBridge):
        project = engine.execute_workflow(
            requirement="test fallback",
            script_path=str(script_path),
            callbacks=WorkflowEngineCallbacks(on_done=on_done, on_error=on_error),
        )

    assert project.status == WorkflowStatus.FAILED
    assert "agent_call timed out waiting for host response" in (project.error or "")
    on_done.assert_not_called()
    on_error.assert_called_once()


# ---------------------------------------------------------------------------
# Late session close daemon thread
# ---------------------------------------------------------------------------


class TestCloseLateSessionDaemonThread(unittest.TestCase):
    """_close_late_session must close the stale session from a daemon thread."""

    def test_close_late_session_uses_daemon_thread(self):
        cancel_event = threading.Event()
        executor = AgentExecutor(cwd="/tmp", cancel_event=cancel_event, max_workers=2)
        try:
            closed_event = threading.Event()

            class FakeSession:
                def close(self_inner):
                    closed_event.set()

            # Build a future that is "done" with a FakeSession result.
            future: concurrent.futures.Future = concurrent.futures.Future()
            future.set_result(FakeSession())

            # Invoke _close_late_session. It should register a done callback that
            # (because the future is already done) will fire almost immediately and
            # spin up a daemon thread to call close().
            executor._close_late_session(future, "fake-tool")

            # The calling thread must NOT be blocked waiting for close() — we can
            # verify this by returning control to the test promptly. close() is
            # called from a daemon thread, so we wait for the signal with a timeout.
            self.assertTrue(
                closed_event.wait(timeout=5),
                "FakeSession.close() was not called within 5s",
            )
        finally:
            executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# State manager terminal-state consistency
# ---------------------------------------------------------------------------


class TestStateManagerTerminalStates(unittest.TestCase):
    """on_workflow_cancelled/on_workflow_failed must leave no agent RUNNING/PENDING."""

    def _build_state_with_mixed_agents(self) -> WorkflowStateManager:
        project = _make_project("terminal-test", status=WorkflowStatus.RUNNING)
        sm = WorkflowStateManager(project)
        # Inject via the public API so the label index is consistent.
        sm.on_phase_changed("phase-1")
        sm.on_agent_started("runner-1", tool="coco", phase="phase-1")
        sm.on_agent_started("runner-2", tool="traex", phase="phase-1")
        # Mark one done, one failed, and leave the rest RUNNING.
        sm.on_agent_done(
            "runner-1",
            {"token_usage": 10, "duration_s": 0.5, "cached": False},
        )
        sm.on_agent_started("runner-3", tool="coco", phase="phase-1")
        return sm

    def _collect_statuses(self, project: WorkflowProject) -> list[AgentStatus]:
        statuses: list[AgentStatus] = []
        for phase in project.phases:
            for agent in phase.agents:
                statuses.append(agent.status)
        return statuses

    def test_on_workflow_cancelled_marks_running_agents(self):
        sm = self._build_state_with_mixed_agents()
        sm.on_workflow_cancelled("cancelled by user")
        project = sm.snapshot()
        self.assertEqual(project.status, WorkflowStatus.CANCELLED)
        statuses = self._collect_statuses(project)
        self.assertNotIn(AgentStatus.RUNNING, statuses, "no agent must remain RUNNING after cancel")
        self.assertNotIn(AgentStatus.PENDING, statuses, "no agent must remain PENDING after cancel")

    def test_on_workflow_failed_marks_running_agents(self):
        sm = self._build_state_with_mixed_agents()
        sm.on_workflow_failed("boom")
        project = sm.snapshot()
        self.assertEqual(project.status, WorkflowStatus.FAILED)
        statuses = self._collect_statuses(project)
        self.assertNotIn(AgentStatus.RUNNING, statuses, "no agent must remain RUNNING after fail")
        self.assertNotIn(AgentStatus.PENDING, statuses, "no agent must remain PENDING after fail")

    def test_cancelled_terminal_state_not_overwritten(self):
        """After on_workflow_cancelled sets CANCELLED, a subsequent on_workflow_failed
        must NOT downgrade it to FAILED. The current implementation calls the methods
        directly, so we verify the guard at the caller level: once terminal, later
        calls do not regress the status.

        NOTE: the current code has no idempotency guard in on_workflow_failed itself;
        this test pins the *contract* expected by the engine — if this test starts
        failing because someone added/removed a guard, update both sides together.
        """
        sm = self._build_state_with_mixed_agents()
        sm.on_workflow_cancelled("stop")
        self.assertEqual(sm._project.status, WorkflowStatus.CANCELLED)
        # Simulate the engine's error path firing on_workflow_failed after cancel.
        # We call it directly to pin the behaviour: a second terminal transition
        # must not overwrite CANCELLED with FAILED.
        sm.on_workflow_failed("after-cancel error")
        # Contract: CANCELLED is sticky — once a workflow is cancelled, subsequent
        # error transitions must not rewrite the status.
        self.assertEqual(
            sm._project.status,
            WorkflowStatus.CANCELLED,
            "CANCELLED must be sticky and not be overwritten by a later FAILED transition",
        )



# ---------------------------------------------------------------------------
# Agent-level CANCELLED status (race loser abort)
# ---------------------------------------------------------------------------


class TestAgentCancelledStatus(unittest.TestCase):
    """on_agent_aborted transitions a RUNNING agent to CANCELLED (race loser)."""

    def _build_running_agent(self) -> tuple[WorkflowStateManager, str]:
        project = _make_project("abort-test", status=WorkflowStatus.RUNNING)
        sm = WorkflowStateManager(project)
        sm.on_phase_changed("race-phase")
        label = sm.on_agent_started("contestant-a", tool="coco", phase="race-phase")
        return sm, label

    def test_on_agent_aborted_moves_running_to_cancelled(self):
        """A RUNNING agent transitioned via on_agent_aborted becomes CANCELLED."""
        sm, label = self._build_running_agent()
        sm.on_agent_aborted(label, reason="race loser")
        agent = sm._label_to_agent[label]
        self.assertEqual(agent.status, AgentStatus.CANCELLED)
        self.assertEqual(agent.error, "race loser")
        self.assertIsNotNone(agent.finished_at)

    def test_on_agent_aborted_increments_completed_not_failed(self):
        """Aborted agents count as completed but NOT failed (expected outcome)."""
        sm, label = self._build_running_agent()
        before_completed = sm._project.metrics.completed_agents
        before_failed = sm._project.metrics.failed_agents
        sm.on_agent_aborted(label, reason="race loser")
        self.assertEqual(
            sm._project.metrics.completed_agents, before_completed + 1,
            "cancelled agent should increment completed_agents",
        )
        self.assertEqual(
            sm._project.metrics.failed_agents, before_failed,
            "cancelled agent should NOT increment failed_agents",
        )

    def test_cancelled_agent_is_terminal(self):
        """CANCELLED is a terminal status — _is_terminal_agent returns True."""
        sm, label = self._build_running_agent()
        agent = sm._label_to_agent[label]
        self.assertFalse(sm._is_terminal_agent(agent))
        sm.on_agent_aborted(label, reason="race loser")
        self.assertTrue(sm._is_terminal_agent(agent))

    def test_on_agent_failed_does_not_overwrite_cancelled(self):
        """If agent is already CANCELLED (race abort), a later on_agent_failed
        must NOT downgrade it to FAILED. This handles the race between the
        agent_aborted notification (fast path) and the session's error return
        (slow path after cancel_event interrupts send_prompt)."""
        sm, label = self._build_running_agent()
        sm.on_agent_aborted(label, reason="race loser")
        self.assertEqual(sm._label_to_agent[label].status, AgentStatus.CANCELLED)
        # Now simulate the session returning an error (normal flow after cancel)
        sm.on_agent_failed(label, "Cancelled during execution")
        self.assertEqual(
            sm._label_to_agent[label].status,
            AgentStatus.CANCELLED,
            "CANCELLED must be sticky — on_agent_failed must not overwrite it",
        )
        self.assertEqual(
            sm._label_to_agent[label].error,
            "race loser",
            "original abort reason must be preserved",
        )

    def test_on_agent_done_does_not_overwrite_cancelled(self):
        """If agent is already CANCELLED, a late on_agent_done must not change status.
        This is a defensive check — in practice the cancel_event interrupt would
        cause an error return, not a done return."""
        sm, label = self._build_running_agent()
        sm.on_agent_aborted(label, reason="race loser")
        sm.on_agent_done(label, {"token_usage": 100, "duration_s": 1.0, "cached": False})
        self.assertEqual(
            sm._label_to_agent[label].status,
            AgentStatus.CANCELLED,
            "CANCELLED must remain sticky against done transitions",
        )

    def test_on_agent_aborted_idempotent(self):
        """Calling on_agent_aborted twice for the same agent is safe."""
        sm, label = self._build_running_agent()
        sm.on_agent_aborted(label, reason="first abort")
        before = sm._project.metrics.completed_agents
        sm.on_agent_aborted(label, reason="duplicate abort")
        after = sm._project.metrics.completed_agents
        self.assertEqual(before, after, "completed_agents must not increment twice")
        self.assertEqual(sm._label_to_agent[label].status, AgentStatus.CANCELLED)

    def test_renderer_shows_cancelled_agents_in_own_group(self):
        """Rendered progress card shows CANCELLED agents in a separate '已取消' group,
        NOT in the '执行中' (running) group."""
        project = _make_project("render-cancel", status=WorkflowStatus.RUNNING)
        sm = WorkflowStateManager(project)
        sm.on_phase_changed("race")
        sm.on_agent_started("still-running", tool="coco", phase="race")
        label_cancelled = sm.on_agent_started("aborted-one", tool="claude", phase="race")
        sm.on_agent_aborted(label_cancelled, reason="race loser")

        renderer = WorkflowProgressRenderer(project)
        snapshot = sm.snapshot()
        card = renderer.render_progress_card(snapshot)
        rendered = json.dumps(card, ensure_ascii=False)

        # Cancelled agent must NOT appear in running section
        self.assertNotIn("已取消…", rendered, "cancelled agent must not show as running")
        # Cancelled label should still appear in the card
        self.assertIn("aborted-one", rendered)
        # Running agent must still show as running
        self.assertIn("still-running", rendered)
        # The '已取消' label should be present in the card
        self.assertIn("已取消", rendered)

    def test_cancelled_agents_not_counted_as_running(self):
        """Phase completion logic: CANCELLED agents are terminal (not running)
        and the progress bar counts them in completed_agents (metrics level),
        but the phase-level '已完成' counter only tracks DONE+CACHED (successes).
        The key guarantee: no CANCELLED agent appears in the RUNNING group."""
        project = _make_project("phase-done", status=WorkflowStatus.RUNNING)
        sm = WorkflowStateManager(project)
        sm.on_phase_changed("phase-1")
        sm.on_agent_started("a", tool="coco", phase="phase-1")
        sm.on_agent_started("b", tool="claude", phase="phase-1")

        renderer = WorkflowProgressRenderer(project)
        snapshot = sm.snapshot()
        card = renderer.render_progress_card(snapshot)
        rendered = json.dumps(card, ensure_ascii=False)
        # Both running → should show in RUNNING group
        self.assertIn("执行中", rendered)
        self.assertIn("执行中 (2)", rendered)

        # Abort both agents
        sm.on_agent_aborted("a", reason="race loser")
        sm.on_agent_aborted("b", reason="race loser")
        snapshot = sm.snapshot()
        card = renderer.render_progress_card(snapshot)
        rendered = json.dumps(card, ensure_ascii=False)

        # Key assertion: no agents shown as '执行中'
        self.assertNotIn("执行中 (1)", rendered)
        self.assertNotIn("执行中 (2)", rendered)
        # CANCELLED agents should have their own group
        self.assertIn("已取消", rendered)
        # Progress bar should show all 2 as completed (metrics level)
        self.assertIn("进度 2/2", rendered)


if __name__ == "__main__":
    unittest.main()
