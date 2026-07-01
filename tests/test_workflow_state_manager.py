"""Tests for WorkflowStateManager thread-safe state mutations.

Validates:
- on_phase_changed creates a PhaseProgress entry
- on_agent_started / on_agent_done / on_agent_failed update metrics atomically
- on_workflow_done / on_workflow_failed set terminal states
- snapshot() returns the same project reference safely
- Concurrent mutations don't race
"""

import threading
import time
import unittest

from src.workflow_engine.models import (
    AgentStatus,
    WorkflowProject,
    WorkflowStatus,
)
from src.workflow_engine.state_manager import WorkflowStateManager


class TestOnPhaseChanged(unittest.TestCase):
    """Test on_phase_changed behavior."""

    def test_creates_phase_entry(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("Phase 1")

        self.assertEqual(len(project.phases), 1)
        self.assertEqual(project.phases[0].title, "Phase 1")
        self.assertIsNotNone(project.phases[0].started_at)

    def test_sets_status_running(self):
        project = WorkflowProject(status=WorkflowStatus.IDLE)
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("Phase 1")

        self.assertEqual(project.status, WorkflowStatus.RUNNING)

    def test_multiple_phases_append(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("Phase 1")
        mgr.on_phase_changed("Phase 2")

        self.assertEqual(len(project.phases), 2)
        self.assertEqual(project.phases[1].title, "Phase 2")


class TestOnAgentStarted(unittest.TestCase):
    """Test on_agent_started behavior."""

    def test_adds_agent_to_matching_phase(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("Phase 1")
        mgr.on_agent_started("agent_1", "coco", "Phase 1")

        self.assertEqual(len(project.phases[0].agents), 1)
        self.assertEqual(project.phases[0].agents[0].label, "agent_1")
        self.assertEqual(project.phases[0].agents[0].tool, "coco")
        self.assertEqual(project.phases[0].agents[0].status, AgentStatus.RUNNING)

    def test_creates_phase_if_not_found(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_agent_started("agent_1", "claude", "New Phase")

        self.assertEqual(len(project.phases), 1)
        self.assertEqual(project.phases[0].title, "New Phase")
        self.assertEqual(len(project.phases[0].agents), 1)

    def test_increments_total_agents(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        mgr.on_agent_started("a1", "coco", "P1")
        mgr.on_agent_started("a2", "coco", "P1")

        self.assertEqual(project.metrics.total_agents, 2)

    def test_records_started_at_timestamp(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")

        before = time.time()
        effective_label = mgr.on_agent_started("a1", "coco", "P1")
        after = time.time()

        self.assertEqual(effective_label, "a1")
        agent = project.phases[0].agents[0]
        self.assertGreaterEqual(agent.started_at, before)
        self.assertLessEqual(agent.started_at, after)
        self.assertIsNone(agent.finished_at)

    def test_duplicate_labels_get_unique_effective_labels(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")

        first = mgr.on_agent_started("task-analysis", "traex", "P1")
        second = mgr.on_agent_started("task-analysis", "traex", "P1")

        labels = [agent.label for agent in project.phases[0].agents]
        self.assertEqual(first, "task-analysis")
        self.assertEqual(second, "task-analysis #2")
        self.assertEqual(labels, ["task-analysis", "task-analysis #2"])
        self.assertIn("task-analysis", mgr._label_to_agent)
        self.assertIn("task-analysis #2", mgr._label_to_agent)

    def test_duplicate_labels_do_not_show_same_agent_running_and_failed(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")

        first = mgr.on_agent_started("task-analysis", "traex", "P1")
        second = mgr.on_agent_started("task-analysis", "traex", "P1")
        mgr.on_agent_failed(second, "TimeoutError: ACP prompt 执行超时 (300s)")

        running_labels = [
            agent.label
            for agent in project.phases[0].agents
            if agent.status == AgentStatus.RUNNING
        ]
        failed_labels = [
            agent.label
            for agent in project.phases[0].agents
            if agent.status == AgentStatus.FAILED
        ]

        self.assertEqual(running_labels, [first])
        self.assertEqual(failed_labels, [second])
        self.assertTrue(set(running_labels).isdisjoint(failed_labels))


class TestOnAgentDone(unittest.TestCase):
    """Test on_agent_done behavior."""

    def test_marks_agent_done(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        mgr.on_agent_started("a1", "coco", "P1")
        mgr.on_agent_done("a1", {"token_usage": 1000, "duration_s": 2.5})

        agent = project.phases[0].agents[0]
        self.assertEqual(agent.status, AgentStatus.DONE)
        self.assertEqual(agent.token_usage, 1000)
        self.assertEqual(agent.duration_s, 2.5)
        self.assertIsNotNone(agent.finished_at)

    def test_marks_agent_cached(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        mgr.on_agent_started("a1", "coco", "P1")
        mgr.on_agent_done("a1", {"token_usage": 500, "duration_s": 0.1, "cached": True})

        agent = project.phases[0].agents[0]
        self.assertEqual(agent.status, AgentStatus.CACHED)
        self.assertEqual(project.metrics.cached_agents, 1)

    def test_updates_metrics(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        mgr.on_agent_started("a1", "coco", "P1")
        mgr.on_agent_done("a1", {"token_usage": 2000, "duration_s": 3.0})

        self.assertEqual(project.metrics.completed_agents, 1)
        self.assertEqual(project.metrics.total_tokens, 2000)
        self.assertEqual(project.metrics.total_duration_s, 3.0)

    def test_unknown_agent_is_noop(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        # Agent never started — should not raise
        mgr.on_agent_done("unknown", {"token_usage": 100})

        self.assertEqual(project.metrics.completed_agents, 0)


class TestOnAgentFailed(unittest.TestCase):
    """Test on_agent_failed behavior."""

    def test_marks_agent_failed(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        mgr.on_agent_started("a1", "coco", "P1")
        mgr.on_agent_failed("a1", "timeout")

        agent = project.phases[0].agents[0]
        self.assertEqual(agent.status, AgentStatus.FAILED)
        self.assertEqual(agent.error, "timeout")
        self.assertEqual(project.metrics.failed_agents, 1)
        self.assertEqual(project.metrics.completed_agents, 1)
        self.assertIsNotNone(agent.finished_at)


class TestOnWorkflowDone(unittest.TestCase):
    """Test on_workflow_done behavior."""

    def test_sets_completed_status(self):
        project = WorkflowProject(status=WorkflowStatus.RUNNING)
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        mgr.on_workflow_done("All tasks finished")

        self.assertEqual(project.status, WorkflowStatus.COMPLETED)
        self.assertEqual(project.result, "All tasks finished")
        self.assertIsNotNone(project.finished_at)

    def test_closes_last_phase(self):
        project = WorkflowProject(status=WorkflowStatus.RUNNING)
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        self.assertIsNone(project.phases[0].finished_at)

        mgr.on_workflow_done("done")
        self.assertIsNotNone(project.phases[0].finished_at)

    def test_sets_phases_completed_count(self):
        project = WorkflowProject(status=WorkflowStatus.RUNNING)
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        mgr.on_phase_changed("P2")
        mgr.on_workflow_done("done")

        self.assertEqual(project.metrics.phases_completed, 2)


class TestOnWorkflowFailed(unittest.TestCase):
    """Test on_workflow_failed behavior."""

    def test_sets_failed_status(self):
        project = WorkflowProject(status=WorkflowStatus.RUNNING)
        mgr = WorkflowStateManager(project)
        mgr.on_workflow_failed("execution failed")

        self.assertEqual(project.status, WorkflowStatus.FAILED)
        self.assertEqual(project.error, "execution failed")
        self.assertIsNotNone(project.finished_at)

    def test_workflow_failed_marks_running_agents_failed_and_closes_phase(self):
        project = WorkflowProject(status=WorkflowStatus.RUNNING)
        mgr = WorkflowStateManager(project)
        mgr.on_phase_changed("P1")
        mgr.on_agent_started("running-agent", "traex", "P1")

        mgr.on_workflow_failed("runtime crashed")

        agent = project.phases[0].agents[0]
        self.assertEqual(agent.status, AgentStatus.FAILED)
        self.assertIn("runtime crashed", agent.error)
        self.assertIsNotNone(agent.finished_at)
        self.assertIsNotNone(project.phases[0].finished_at)
        self.assertEqual(project.metrics.failed_agents, 1)
        self.assertEqual(project.metrics.completed_agents, 1)


class TestSnapshot(unittest.TestCase):
    """Test snapshot() returns the project safely."""

    def test_returns_deep_copy(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        snap = mgr.snapshot()

        # snapshot returns a deep copy, not the same instance
        self.assertIsNot(snap, project)
        # But values should be equal
        self.assertEqual(snap.workflow_id, project.workflow_id)
        self.assertEqual(snap.status, project.status)

    def test_property_access(self):
        project = WorkflowProject()
        mgr = WorkflowStateManager(project)
        self.assertIs(mgr.project, project)


class TestLabelToAgentMap(unittest.TestCase):
    """Tests for the ``_label_to_agent`` O(1) fast-lookup map."""

    @staticmethod
    def _mgr() -> WorkflowStateManager:
        project = WorkflowProject()
        return WorkflowStateManager(project)

    def test_on_agent_started_inserts_in_map(self):
        mgr = self._mgr()
        mgr.on_phase_changed("p1")
        mgr.on_agent_started("a1", "coco", "p1")

        self.assertIn("a1", mgr._label_to_agent)
        # Map entry must reference the *same* object stored in phases.
        agent_in_list = mgr.project.phases[-1].agents[-1]
        self.assertIs(mgr._label_to_agent["a1"], agent_in_list)

    def test_on_agent_done_uses_fast_lookup_and_matches_legacy(self):
        mgr = self._mgr()
        mgr.on_phase_changed("p1")
        mgr.on_agent_started("a1", "coco", "p1")
        mgr.on_agent_done("a1", {"token_usage": 10, "duration_s": 1.0})

        agent = mgr._label_to_agent["a1"]
        self.assertEqual(agent.status, AgentStatus.DONE)
        self.assertEqual(agent.token_usage, 10)

    def test_on_agent_failed_uses_fast_lookup(self):
        mgr = self._mgr()
        mgr.on_phase_changed("p1")
        mgr.on_agent_started("a1", "coco", "p1")
        mgr.on_agent_failed("a1", "boom")

        agent = mgr._label_to_agent["a1"]
        self.assertEqual(agent.status, AgentStatus.FAILED)
        self.assertEqual(agent.error, "boom")

    def test_unknown_label_is_noop(self):
        mgr = self._mgr()
        mgr.on_phase_changed("p1")
        # Neither of these should raise or leak keys into the map.
        mgr.on_agent_done("ghost", {"token_usage": 0})
        mgr.on_agent_failed("ghost", "none")
        self.assertNotIn("ghost", mgr._label_to_agent)

    def test_map_size_matches_total_agents(self):
        mgr = self._mgr()
        mgr.on_phase_changed("p1")

        N = 200
        for i in range(N):
            mgr.on_agent_started(f"x-{i}", "coco", "p1")
        for i in range(N):
            if i % 3 == 0:
                mgr.on_agent_failed(f"x-{i}", "err")
            else:
                mgr.on_agent_done(f"x-{i}", {"token_usage": 1, "duration_s": 0.1})

        self.assertEqual(len(mgr._label_to_agent), N)
        self.assertEqual(mgr.project.metrics.total_agents, N)
        self.assertEqual(mgr.project.metrics.completed_agents, N)

    def test_fallback_repairs_map_after_desync(self):
        """If an agent entry somehow bypassed the map insert, the O(n)
        fallback must find it and repair the map."""
        mgr = self._mgr()
        mgr.on_phase_changed("p1")
        mgr.on_agent_started("ghost", "coco", "p1")

        # Simulate external desync: drop the map entry only.
        mgr._label_to_agent.pop("ghost", None)
        self.assertNotIn("ghost", mgr._label_to_agent)

        mgr.on_agent_done("ghost", {"token_usage": 1, "duration_s": 0.1})

        agent_in_list = mgr.project.phases[-1].agents[-1]
        self.assertIs(mgr._label_to_agent["ghost"], agent_in_list)
        self.assertEqual(agent_in_list.status, AgentStatus.DONE)

    def test_later_start_with_same_label_creates_distinct_map_entries(self):
        """Starting the same label in a newer phase must not hide the old run."""
        mgr = self._mgr()
        mgr.on_phase_changed("p1")
        first_label = mgr.on_agent_started("dup", "coco", "p1")
        first = mgr._label_to_agent["dup"]

        mgr.on_phase_changed("p2")
        second_label = mgr.on_agent_started("dup", "coco", "p2")
        second = mgr._label_to_agent[second_label]

        self.assertEqual(first_label, "dup")
        self.assertEqual(second_label, "dup #2")
        self.assertIsNot(first, second)
        self.assertIs(second, mgr.project.phases[-1].agents[-1])
        self.assertIs(first, mgr.project.phases[-2].agents[-1])

    def test_concurrent_starts_and_done_no_keyerror(self):
        mgr = self._mgr()
        mgr.on_phase_changed("p1")

        N = 200
        errors: list[Exception] = []
        barrier = threading.Barrier(N)

        def worker(i: int):
            label = f"w-{i}"
            try:
                barrier.wait(timeout=5.0)
                mgr.on_agent_started(label, "coco", "p1")
                if i % 2 == 0:
                    mgr.on_agent_done(
                        label, {"token_usage": 1, "duration_s": 0.01}
                    )
                else:
                    mgr.on_agent_failed(label, "boom")
                # Re-lookup via the map to exercise the read path.
                _ = mgr._label_to_agent[label]
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(mgr._label_to_agent), N)
        self.assertEqual(mgr.project.metrics.completed_agents, N)
        self.assertEqual(mgr.project.metrics.failed_agents, N // 2)

    def test_thundering_reads_and_writes(self):
        mgr = self._mgr()
        mgr.on_phase_changed("p1")

        N = 200
        errors: list[Exception] = []
        stop = threading.Event()

        def producer(i: int):
            try:
                mgr.on_agent_started(f"p-{i}", "coco", "p1")
            except Exception as exc:
                errors.append(exc)

        def reader():
            try:
                while not stop.is_set():
                    snap = mgr.snapshot()
                    for phase in snap.phases:
                        for agent in phase.agents:
                            _ = agent.status
                    time.sleep(0)
            except Exception as exc:
                errors.append(exc)

        prods = [threading.Thread(target=producer, args=(i,)) for i in range(N)]
        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        for t in prods:
            t.start()
        for t in prods:
            t.join()
        stop.set()
        reader_thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(mgr._label_to_agent), N)


if __name__ == "__main__":
    unittest.main()
