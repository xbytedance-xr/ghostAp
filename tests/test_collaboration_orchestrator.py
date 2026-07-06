"""Unit tests for CollaborationOrchestrator.

Tests cover:
1. create_plan creates a plan with correct steps from chain template
2. approve_plan transitions PENDING_APPROVAL -> EXECUTING and starts first step
3. cancel_plan transitions to CANCELLED
4. on_task_status_changed marks step DONE and advances to next
5. Plan completion when all steps are done/skipped/timed_out
6. pause_plan / resume_plan lifecycle (skipped — methods not present)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.collaboration_orchestrator import CollaborationOrchestrator
from src.slock_engine.models import (
    AgentIdentity,
    CollaborationPlan,
    CollaborationPlanStatus,
    PlanStep,
    PlanStepStatus,
    SlockTask,
    TaskStatus,
)
from src.slock_engine.observer_queue import TaskStatusNotifier
from src.slock_engine.task_chain_manager import ChainStep, ChainTemplate, TaskChainManager


def _wait_for_mock_call_count(mock: MagicMock, expected: int, *, timeout: float = 1.0) -> None:
    """Wait for async dispatch executor callbacks to reach the expected count."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mock.call_count >= expected:
            return
        time.sleep(0.01)
    assert mock.call_count == expected


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_template() -> ChainTemplate:
    """A chain template with 3 roles: coder -> reviewer -> tester."""
    return ChainTemplate(
        name="coder->reviewer->tester",
        steps=[
            ChainStep(role="coder", order=0),
            ChainStep(role="reviewer", order=1),
            ChainStep(role="tester", order=2),
        ],
    )


@pytest.fixture
def chain_manager(chain_template: ChainTemplate) -> MagicMock:
    """Mock TaskChainManager that returns the chain template."""
    mgr = MagicMock(spec=TaskChainManager)
    mgr.get_template_by_name.return_value = chain_template
    mgr.find_chain_for_task.return_value = chain_template
    return mgr


@pytest.fixture
def notifier() -> TaskStatusNotifier:
    """Real TaskStatusNotifier instance."""
    return TaskStatusNotifier()


@pytest.fixture
def agents() -> dict[str, AgentIdentity]:
    """Pre-built agents keyed by role."""
    return {
        "coder": AgentIdentity(agent_id="agent-coder", name="Coder", role="coder"),
        "reviewer": AgentIdentity(agent_id="agent-reviewer", name="Reviewer", role="reviewer"),
        "tester": AgentIdentity(agent_id="agent-tester", name="Tester", role="tester"),
    }


@pytest.fixture
def resolve_agent(agents: dict[str, AgentIdentity]):
    """Mock resolve_agent callable that returns correct agent by role."""

    def _resolve(role: str, channel_id: str):
        return agents.get(role)

    return MagicMock(side_effect=_resolve)


@pytest.fixture
def dispatch_task() -> MagicMock:
    """Mock dispatch_task callable."""
    return MagicMock()


@pytest.fixture
def task() -> SlockTask:
    """A sample task for plan creation."""
    return SlockTask(
        task_id="task-001",
        content="Implement login feature",
        status=TaskStatus.TODO,
        created_in="channel-1",
    )


@pytest.fixture
def orchestrator(
    chain_manager: MagicMock,
    notifier: TaskStatusNotifier,
    resolve_agent: MagicMock,
    dispatch_task: MagicMock,
) -> CollaborationOrchestrator:
    """Build orchestrator with short timeouts and mocked dependencies."""
    with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
        settings = MagicMock()
        settings.slock_auto_plan_timeout = 9999  # large so auto-start won't fire
        settings.slock_role_response_timeout = 9999
        mock_settings.return_value = settings

        orch = CollaborationOrchestrator(
            chain_manager=chain_manager,
            notifier=notifier,
            resolve_agent=resolve_agent,
            dispatch_task=dispatch_task,
            auto_plan_timeout=9999,
            role_response_timeout=9999,
        )
    yield orch
    orch.shutdown()


# ---------------------------------------------------------------------------
# 1. create_plan creates a plan with correct steps from chain template
# ---------------------------------------------------------------------------


class TestCreatePlan:
    def test_creates_plan_with_steps_from_template(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        assert plan is not None
        assert plan.task_id == "task-001"
        assert plan.status == CollaborationPlanStatus.PENDING_APPROVAL
        assert plan.chain_template == "coder->reviewer->tester"
        assert len(plan.steps) == 3

    def test_steps_have_correct_roles_and_order(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        roles = [step.role for step in plan.steps]
        assert roles == ["coder", "reviewer", "tester"]

        orders = [step.order for step in plan.steps]
        assert orders == [0, 1, 2]

    def test_steps_have_agents_resolved(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        assert plan.steps[0].agent_id == "agent-coder"
        assert plan.steps[1].agent_id == "agent-reviewer"
        assert plan.steps[2].agent_id == "agent-tester"

    def test_steps_have_sequential_dependencies(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        # First step has no dependencies
        assert plan.steps[0].depends_on == []
        # Second step depends on first
        assert plan.steps[1].depends_on == [plan.steps[0].step_id]
        # Third step depends on second
        assert plan.steps[2].depends_on == [plan.steps[1].step_id]

    def test_all_steps_start_with_todo_status(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        for step in plan.steps:
            assert step.status == PlanStepStatus.TODO

    def test_returns_none_when_no_template_found(
        self, orchestrator: CollaborationOrchestrator, task: SlockTask, chain_manager: MagicMock
    ):
        chain_manager.find_chain_for_task.return_value = None

        plan = orchestrator.create_plan(task, channel_id="channel-1")
        assert plan is None

    def test_uses_explicit_chain_template_name(
        self, orchestrator: CollaborationOrchestrator, task: SlockTask, chain_manager: MagicMock
    ):
        orchestrator.create_plan(task, channel_id="channel-1", chain_template_name="coder->reviewer->tester")

        chain_manager.get_template_by_name.assert_called_once_with("coder->reviewer->tester")

    def test_plan_stored_and_retrievable(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        retrieved = orchestrator.get_plan(plan.plan_id)
        assert retrieved is plan

        retrieved_by_task = orchestrator.get_plan_for_task("task-001")
        assert retrieved_by_task is plan


# ---------------------------------------------------------------------------
# 2. approve_plan transitions PENDING_APPROVAL -> EXECUTING and starts first step
# ---------------------------------------------------------------------------


class TestApprovePlan:
    def test_approve_transitions_to_executing(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        result = orchestrator.approve_plan(plan.plan_id)

        assert result is True
        assert plan.status == CollaborationPlanStatus.EXECUTING

    def test_approve_starts_first_step(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
        dispatch_task: MagicMock,
    ):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        orchestrator.approve_plan(plan.plan_id)

        # First step should now be IN_PROGRESS
        assert plan.steps[0].status == PlanStepStatus.IN_PROGRESS
        assert plan.steps[0].task_id != ""

        # dispatch_task should have been called with the created task and coder agent
        _wait_for_mock_call_count(dispatch_task, 1)
        dispatch_task.assert_called_once()
        dispatched_task, dispatched_agent = dispatch_task.call_args[0]
        assert dispatched_task.task_id == plan.steps[0].task_id
        assert dispatched_agent.agent_id == "agent-coder"

    def test_approve_nonexistent_plan_returns_false(self, orchestrator: CollaborationOrchestrator):
        result = orchestrator.approve_plan("nonexistent-plan-id")
        assert result is False

    def test_approve_already_executing_plan_returns_false(
        self, orchestrator: CollaborationOrchestrator, task: SlockTask
    ):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Approve again should fail
        result = orchestrator.approve_plan(plan.plan_id)
        assert result is False

    def test_only_first_step_started_on_approve(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
    ):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Only step 0 should be in progress, others still TODO
        assert plan.steps[0].status == PlanStepStatus.IN_PROGRESS
        assert plan.steps[1].status == PlanStepStatus.TODO
        assert plan.steps[2].status == PlanStepStatus.TODO


# ---------------------------------------------------------------------------
# 3. cancel_plan transitions to CANCELLED
# ---------------------------------------------------------------------------


class TestCancelPlan:
    def test_cancel_pending_plan(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")

        result = orchestrator.cancel_plan(plan.plan_id)

        assert result is True
        assert plan.status == CollaborationPlanStatus.CANCELLED

    def test_cancel_executing_plan(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        result = orchestrator.cancel_plan(plan.plan_id)

        assert result is True
        assert plan.status == CollaborationPlanStatus.CANCELLED

    def test_cancel_nonexistent_plan_returns_false(self, orchestrator: CollaborationOrchestrator):
        result = orchestrator.cancel_plan("nonexistent-plan-id")
        assert result is False

    def test_cancel_already_cancelled_plan_returns_false(
        self, orchestrator: CollaborationOrchestrator, task: SlockTask
    ):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.cancel_plan(plan.plan_id)

        result = orchestrator.cancel_plan(plan.plan_id)
        assert result is False

    def test_cancel_completed_plan_returns_false(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        # Force plan to COMPLETED
        plan.status = CollaborationPlanStatus.COMPLETED

        result = orchestrator.cancel_plan(plan.plan_id)
        assert result is False


# ---------------------------------------------------------------------------
# 4. on_task_status_changed marks step DONE and advances to next
# ---------------------------------------------------------------------------


class TestOnTaskStatusChanged:
    def test_marks_step_done_on_task_completion(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        step0_task_id = plan.steps[0].task_id

        # Simulate task completion
        orchestrator.on_task_status_changed(
            task_id=step0_task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        assert plan.steps[0].status == PlanStepStatus.DONE

    def test_advances_to_next_step_on_completion(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
        dispatch_task: MagicMock,
    ):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        step0_task_id = plan.steps[0].task_id

        # Complete first step
        orchestrator.on_task_status_changed(
            task_id=step0_task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        # Second step should now be IN_PROGRESS
        assert plan.steps[1].status == PlanStepStatus.IN_PROGRESS
        assert plan.steps[1].task_id != ""

        # dispatch_task should have been called a second time for reviewer
        _wait_for_mock_call_count(dispatch_task, 2)
        assert dispatch_task.call_count == 2
        _, dispatched_agent = dispatch_task.call_args[0]
        assert dispatched_agent.agent_id == "agent-reviewer"

    def test_ignores_non_done_status_changes(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        step0_task_id = plan.steps[0].task_id

        # IN_PROGRESS -> IN_REVIEW should be ignored
        orchestrator.on_task_status_changed(
            task_id=step0_task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.IN_REVIEW.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        # Step should still be IN_PROGRESS (not marked done)
        assert plan.steps[0].status == PlanStepStatus.IN_PROGRESS

    def test_ignores_unknown_task_id(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Should not raise or change anything
        orchestrator.on_task_status_changed(
            task_id="unknown-task-id",
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        assert plan.steps[0].status == PlanStepStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# 5. Plan completion when all steps are done/skipped/timed_out
# ---------------------------------------------------------------------------


class TestPlanCompletion:
    def test_plan_completes_when_all_steps_done(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
    ):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Complete step 0
        orchestrator.on_task_status_changed(
            task_id=plan.steps[0].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )
        assert plan.status == CollaborationPlanStatus.EXECUTING

        # Complete step 1
        orchestrator.on_task_status_changed(
            task_id=plan.steps[1].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-reviewer",
            channel_id="channel-1",
        )
        assert plan.status == CollaborationPlanStatus.EXECUTING

        # Complete step 2 (final)
        orchestrator.on_task_status_changed(
            task_id=plan.steps[2].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-tester",
            channel_id="channel-1",
        )
        assert plan.status == CollaborationPlanStatus.COMPLETED

    def test_plan_completes_with_mixed_done_and_skipped(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
        resolve_agent: MagicMock,
    ):
        """Plan completes even if some steps are skipped (no agent available)."""

        # Make reviewer agent unavailable so step gets skipped
        def _resolve(role: str, channel_id: str):
            if role == "reviewer":
                return None
            return AgentIdentity(agent_id=f"agent-{role}", name=role, role=role)

        resolve_agent.side_effect = _resolve

        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Complete step 0 (coder)
        orchestrator.on_task_status_changed(
            task_id=plan.steps[0].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        # Step 1 (reviewer) should be SKIPPED because no agent was found
        assert plan.steps[1].status == PlanStepStatus.SKIPPED

        # Step 2 (tester) should now be IN_PROGRESS (dependency met by skip)
        assert plan.steps[2].status == PlanStepStatus.IN_PROGRESS

        # Complete step 2
        orchestrator.on_task_status_changed(
            task_id=plan.steps[2].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-tester",
            channel_id="channel-1",
        )

        assert plan.status == CollaborationPlanStatus.COMPLETED

    def test_plan_completes_with_timed_out_steps(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
    ):
        """Plan completes even if some steps time out."""
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Simulate step 0 timeout via internal handler
        orchestrator._handle_step_timeout(plan.plan_id, plan.steps[0].step_id)

        assert plan.steps[0].status == PlanStepStatus.TIMED_OUT

        # Step 1 should now be started
        assert plan.steps[1].status == PlanStepStatus.IN_PROGRESS

        # Complete step 1
        orchestrator.on_task_status_changed(
            task_id=plan.steps[1].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-reviewer",
            channel_id="channel-1",
        )

        # Complete step 2
        orchestrator.on_task_status_changed(
            task_id=plan.steps[2].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-tester",
            channel_id="channel-1",
        )

        assert plan.status == CollaborationPlanStatus.COMPLETED


# ---------------------------------------------------------------------------
# 6. pause_plan / resume_plan lifecycle
# ---------------------------------------------------------------------------


class TestPausePlanResumePlan:
    """Test pause_plan and resume_plan lifecycle."""

    def test_pause_executing_plan(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        assert plan.status == CollaborationPlanStatus.EXECUTING

        result = orchestrator.pause_plan(plan.plan_id)
        assert result is True
        assert plan.status == CollaborationPlanStatus.PAUSED

    def test_pause_non_executing_plan_returns_false(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        # Plan is PENDING_APPROVAL, cannot pause
        result = orchestrator.pause_plan(plan.plan_id)
        assert result is False

    def test_resume_paused_plan(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        orchestrator.pause_plan(plan.plan_id)
        assert plan.status == CollaborationPlanStatus.PAUSED

        result = orchestrator.resume_plan(plan.plan_id)
        assert result is True
        assert plan.status == CollaborationPlanStatus.EXECUTING

    def test_resume_non_paused_plan_returns_false(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        # Plan is EXECUTING, cannot resume
        result = orchestrator.resume_plan(plan.plan_id)
        assert result is False

    def test_paused_plan_does_not_advance_on_task_done(self, orchestrator: CollaborationOrchestrator, task: SlockTask):
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        orchestrator.pause_plan(plan.plan_id)

        # Complete step 0 while paused — event is dropped because plan is not EXECUTING
        orchestrator.on_task_status_changed(
            task_id=plan.steps[0].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        # Plan stays paused, step stays IN_PROGRESS (event ignored)
        assert plan.status == CollaborationPlanStatus.PAUSED
        # Next step should NOT have started
        assert plan.steps[1].status == PlanStepStatus.TODO


# ---------------------------------------------------------------------------
# 7. Persistence callback integration
# ---------------------------------------------------------------------------


class TestPersistenceCallbacks:
    """Test that persist_fn is called on state mutations."""

    def test_persist_called_on_create(self, chain_manager, notifier, resolve_agent, dispatch_task):
        persist_fn = MagicMock()

        with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_auto_plan_timeout = 9999
            settings.slock_role_response_timeout = 9999
            mock_settings.return_value = settings

            orch = CollaborationOrchestrator(
                chain_manager=chain_manager,
                notifier=notifier,
                resolve_agent=resolve_agent,
                dispatch_task=dispatch_task,
                auto_plan_timeout=9999,
                role_response_timeout=9999,
                persist_fn=persist_fn,
            )

        task = SlockTask(task_id="t-p1", content="Test persist", status=TaskStatus.TODO, created_in="ch1")
        orch.create_plan(task, channel_id="ch1")

        assert persist_fn.called
        orch.shutdown()

    def test_persist_called_on_approve(self, chain_manager, notifier, resolve_agent, dispatch_task):
        persist_fn = MagicMock()

        with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_auto_plan_timeout = 9999
            settings.slock_role_response_timeout = 9999
            mock_settings.return_value = settings

            orch = CollaborationOrchestrator(
                chain_manager=chain_manager,
                notifier=notifier,
                resolve_agent=resolve_agent,
                dispatch_task=dispatch_task,
                auto_plan_timeout=9999,
                role_response_timeout=9999,
                persist_fn=persist_fn,
            )

        task = SlockTask(task_id="t-p2", content="Test approve", status=TaskStatus.TODO, created_in="ch1")
        plan = orch.create_plan(task, channel_id="ch1")
        persist_fn.reset_mock()

        orch.approve_plan(plan.plan_id)
        assert persist_fn.called
        orch.shutdown()

    def test_persist_called_on_pause_resume(self, chain_manager, notifier, resolve_agent, dispatch_task):
        persist_fn = MagicMock()

        with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_auto_plan_timeout = 9999
            settings.slock_role_response_timeout = 9999
            mock_settings.return_value = settings

            orch = CollaborationOrchestrator(
                chain_manager=chain_manager,
                notifier=notifier,
                resolve_agent=resolve_agent,
                dispatch_task=dispatch_task,
                auto_plan_timeout=9999,
                role_response_timeout=9999,
                persist_fn=persist_fn,
            )

        task = SlockTask(task_id="t-p3", content="Test pause", status=TaskStatus.TODO, created_in="ch1")
        plan = orch.create_plan(task, channel_id="ch1")
        orch.approve_plan(plan.plan_id)
        persist_fn.reset_mock()

        orch.pause_plan(plan.plan_id)
        assert persist_fn.call_count >= 1

        persist_fn.reset_mock()
        orch.resume_plan(plan.plan_id)
        assert persist_fn.call_count >= 1
        orch.shutdown()


# ---------------------------------------------------------------------------
# 8. Chain auto-dispatch: coder → reviewer → tester
# ---------------------------------------------------------------------------


class TestChainAutoDispatch:
    """Verify that coder DONE auto-dispatches reviewer, and reviewer DONE dispatches tester."""

    def test_chain_coder_to_reviewer(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
        dispatch_task: MagicMock,
    ):
        """Mock coder task DONE → on_task_status_changed → _start_next_step dispatches reviewer ≤10s."""
        import time as _time

        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Step 0 (coder) should be in progress
        assert plan.steps[0].status == PlanStepStatus.IN_PROGRESS
        assert plan.steps[0].agent_id == "agent-coder"
        step0_task_id = plan.steps[0].task_id

        _wait_for_mock_call_count(dispatch_task, 1)
        dispatch_task.reset_mock()
        t0 = _time.monotonic()

        # Simulate coder task completion
        orchestrator.on_task_status_changed(
            task_id=step0_task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        elapsed = _time.monotonic() - t0

        # Verify dispatch happened within 10s (synchronous in tests, should be ~instant)
        assert elapsed <= 10.0, f"Dispatch took {elapsed:.2f}s, expected ≤10s"

        # Step 1 (reviewer) should now be in progress
        assert plan.steps[1].status == PlanStepStatus.IN_PROGRESS
        assert plan.steps[1].agent_id == "agent-reviewer"
        assert plan.steps[1].task_id != ""

        # dispatch_task should have been called exactly once for reviewer
        _wait_for_mock_call_count(dispatch_task, 1)
        dispatch_task.assert_called_once()
        dispatched_task, dispatched_agent = dispatch_task.call_args[0]
        assert dispatched_agent.agent_id == "agent-reviewer"
        assert dispatched_agent.role == "reviewer"

    def test_chain_reviewer_to_tester(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
        dispatch_task: MagicMock,
    ):
        """Mock reviewer task DONE → on_task_status_changed → _start_next_step dispatches tester ≤10s."""
        import time as _time

        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        _wait_for_mock_call_count(dispatch_task, 1)
        dispatch_task.reset_mock()

        # Complete coder step first
        orchestrator.on_task_status_changed(
            task_id=plan.steps[0].task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        # Reviewer should now be in progress
        assert plan.steps[1].status == PlanStepStatus.IN_PROGRESS
        assert plan.steps[1].agent_id == "agent-reviewer"
        step1_task_id = plan.steps[1].task_id

        _wait_for_mock_call_count(dispatch_task, 1)
        dispatch_task.reset_mock()
        t0 = _time.monotonic()

        # Simulate reviewer task completion
        orchestrator.on_task_status_changed(
            task_id=step1_task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-reviewer",
            channel_id="channel-1",
        )

        elapsed = _time.monotonic() - t0

        # Verify dispatch happened within 10s
        assert elapsed <= 10.0, f"Dispatch took {elapsed:.2f}s, expected ≤10s"

        # Step 2 (tester) should now be in progress
        assert plan.steps[2].status == PlanStepStatus.IN_PROGRESS
        assert plan.steps[2].agent_id == "agent-tester"
        assert plan.steps[2].task_id != ""

        # dispatch_task should have been called exactly once for tester
        _wait_for_mock_call_count(dispatch_task, 1)
        dispatch_task.assert_called_once()
        dispatched_task, dispatched_agent = dispatch_task.call_args[0]
        assert dispatched_agent.agent_id == "agent-tester"
        assert dispatched_agent.role == "tester"

    def test_full_chain_coder_reviewer_tester_completes_plan(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
        dispatch_task: MagicMock,
    ):
        """Full chain execution: coder → reviewer → tester → plan COMPLETED."""
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Complete all 3 steps sequentially
        for i, (agent_id, role) in enumerate(
            [
                ("agent-coder", "coder"),
                ("agent-reviewer", "reviewer"),
                ("agent-tester", "tester"),
            ]
        ):
            task_id = plan.steps[i].task_id
            assert plan.steps[i].agent_id == agent_id

            orchestrator.on_task_status_changed(
                task_id=task_id,
                old_status=TaskStatus.IN_PROGRESS.value,
                new_status=TaskStatus.DONE.value,
                agent_id=agent_id,
                channel_id="channel-1",
            )

        # Plan should be completed
        assert plan.status == CollaborationPlanStatus.COMPLETED
        for step in plan.steps:
            assert step.status == PlanStepStatus.DONE


# ---------------------------------------------------------------------------
# 7. Task dedup: board flow (add_task + claim_task) vs legacy direct construction
# ---------------------------------------------------------------------------


class TestTaskDedup:
    """Verify orchestrator uses board add_task+claim_task when wired."""

    def test_board_flow_used_when_add_task_fn_present(
        self,
        chain_manager: MagicMock,
        notifier: TaskStatusNotifier,
        resolve_agent: MagicMock,
        dispatch_task: MagicMock,
    ):
        """With add_task_fn + claim_task_fn, board creates and claims the task."""
        add_task_fn = MagicMock(
            side_effect=lambda content: SlockTask(
                task_id="board-task-1",
                content=content,
                status=TaskStatus.TODO,
                created_in="ch-1",
            )
        )
        claim_task_fn = MagicMock(return_value=True)

        with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_auto_plan_timeout = 9999
            settings.slock_role_response_timeout = 9999
            mock_settings.return_value = settings

            orch = CollaborationOrchestrator(
                chain_manager=chain_manager,
                notifier=notifier,
                resolve_agent=resolve_agent,
                dispatch_task=dispatch_task,
                add_task_fn=add_task_fn,
                claim_task_fn=claim_task_fn,
                auto_plan_timeout=9999,
                role_response_timeout=9999,
            )

        task = SlockTask(task_id="src-task", content="build it", status=TaskStatus.TODO, created_in="ch-1")
        plan = orch.create_plan(task, channel_id="ch-1")
        assert plan is not None

        orch.approve_plan(plan.plan_id)

        # Board add_task should have been called for the first step
        add_task_fn.assert_called()
        # claim_task should have been called with the board-created task_id
        claim_task_fn.assert_called()
        first_call_args = claim_task_fn.call_args[0]
        assert first_call_args[0] == "board-task-1"  # task_id
        assert first_call_args[1] == "agent-coder"  # agent_id

        orch.shutdown()

    def test_legacy_fallback_when_no_add_task_fn(
        self,
        chain_manager: MagicMock,
        notifier: TaskStatusNotifier,
        resolve_agent: MagicMock,
        dispatch_task: MagicMock,
    ):
        """Without add_task_fn, orchestrator constructs tasks directly (legacy)."""
        register_task = MagicMock()

        with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_auto_plan_timeout = 9999
            settings.slock_role_response_timeout = 9999
            mock_settings.return_value = settings

            orch = CollaborationOrchestrator(
                chain_manager=chain_manager,
                notifier=notifier,
                resolve_agent=resolve_agent,
                dispatch_task=dispatch_task,
                register_task=register_task,
                auto_plan_timeout=9999,
                role_response_timeout=9999,
            )

        task = SlockTask(task_id="src-task", content="build it", status=TaskStatus.TODO, created_in="ch-1")
        plan = orch.create_plan(task, channel_id="ch-1")
        assert plan is not None

        orch.approve_plan(plan.plan_id)

        # Legacy register_task should have been called
        register_task.assert_called()
        # The dispatched task should have IN_PROGRESS status (set directly)
        _wait_for_mock_call_count(dispatch_task, 1)
        dispatched_task = dispatch_task.call_args[0][0]
        assert dispatched_task.status == TaskStatus.IN_PROGRESS

        orch.shutdown()

    def test_board_claim_failure_skips_step(
        self,
        chain_manager: MagicMock,
        notifier: TaskStatusNotifier,
        resolve_agent: MagicMock,
        dispatch_task: MagicMock,
    ):
        """If claim_task returns False, the step is skipped."""
        add_task_fn = MagicMock(
            side_effect=lambda content: SlockTask(
                task_id="board-task-fail",
                content=content,
                status=TaskStatus.TODO,
                created_in="ch-1",
            )
        )
        claim_task_fn = MagicMock(return_value=False)  # Claim always fails

        with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
            settings = MagicMock()
            settings.slock_auto_plan_timeout = 9999
            settings.slock_role_response_timeout = 9999
            mock_settings.return_value = settings

            orch = CollaborationOrchestrator(
                chain_manager=chain_manager,
                notifier=notifier,
                resolve_agent=resolve_agent,
                dispatch_task=dispatch_task,
                add_task_fn=add_task_fn,
                claim_task_fn=claim_task_fn,
                auto_plan_timeout=9999,
                role_response_timeout=9999,
            )

        task = SlockTask(task_id="src-task", content="build it", status=TaskStatus.TODO, created_in="ch-1")
        plan = orch.create_plan(task, channel_id="ch-1")
        assert plan is not None

        orch.approve_plan(plan.plan_id)

        # First step should be SKIPPED due to claim failure
        assert plan.steps[0].status == PlanStepStatus.SKIPPED
        # Dispatch should NOT have been called for the failed step
        # (but may be called for subsequent steps if they get unblocked)

        orch.shutdown()


# ---------------------------------------------------------------------------
# 9. CAS guard: _start_next_step skips step already IN_PROGRESS
# ---------------------------------------------------------------------------


class TestCASGuard:
    """Verify CAS check prevents double-starting a step."""

    def test_start_next_step_skips_if_already_in_progress(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
        dispatch_task: MagicMock,
    ):
        """If a step is already IN_PROGRESS, _start_next_step should not dispatch again."""
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)

        # Step 0 is now IN_PROGRESS after approve
        assert plan.steps[0].status == PlanStepStatus.IN_PROGRESS
        dispatch_task.reset_mock()

        # Manually call _start_next_step again (simulating concurrent event)
        orchestrator._start_next_step(plan)

        # Should NOT dispatch again — CAS guard rejects already-in-progress step
        dispatch_task.assert_not_called()

    def test_concurrent_task_done_events_dont_double_advance(
        self,
        orchestrator: CollaborationOrchestrator,
        task: SlockTask,
        dispatch_task: MagicMock,
    ):
        """Two on_task_status_changed(DONE) for same step should advance only once."""
        plan = orchestrator.create_plan(task, channel_id="channel-1")
        orchestrator.approve_plan(plan.plan_id)
        step0_task_id = plan.steps[0].task_id

        dispatch_task.reset_mock()

        # Simulate two concurrent DONE events for the same step
        orchestrator.on_task_status_changed(
            task_id=step0_task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )
        orchestrator.on_task_status_changed(
            task_id=step0_task_id,
            old_status=TaskStatus.IN_PROGRESS.value,
            new_status=TaskStatus.DONE.value,
            agent_id="agent-coder",
            channel_id="channel-1",
        )

        # dispatch_task should be called only once (for step 1 -> reviewer)
        _wait_for_mock_call_count(dispatch_task, 1)
        assert dispatch_task.call_count == 1


# ---------------------------------------------------------------------------
# 10. restore_plans deadlock avoidance (collect inside lock, start outside)
# ---------------------------------------------------------------------------


class TestRestorePlansDeadlock:
    """Verify restore_plans doesn't deadlock by starting steps outside the lock."""

    def test_restore_executing_plan_starts_next_step(
        self,
        chain_manager: MagicMock,
        notifier: TaskStatusNotifier,
        resolve_agent: MagicMock,
        dispatch_task: MagicMock,
    ):
        """Restoring an EXECUTING plan should start its next step without deadlock."""
        with patch("src.slock_engine.collaboration_orchestrator.get_settings") as ms:
            settings = MagicMock()
            settings.slock_auto_plan_timeout = 9999
            settings.slock_role_response_timeout = 9999
            ms.return_value = settings

            orch = CollaborationOrchestrator(
                chain_manager=chain_manager,
                notifier=notifier,
                resolve_agent=resolve_agent,
                dispatch_task=dispatch_task,
                auto_plan_timeout=9999,
                role_response_timeout=9999,
            )

        # Create a plan and advance it to step 1 in progress
        plan = CollaborationPlan(
            plan_id="restored-plan-1",
            task_id="task-restored",
            steps=[
                PlanStep(
                    step_id="s0",
                    role="coder",
                    agent_id="agent-coder",
                    description="code it",
                    order=0,
                    status=PlanStepStatus.DONE,
                ),
                PlanStep(
                    step_id="s1",
                    role="reviewer",
                    agent_id="agent-reviewer",
                    description="review it",
                    order=1,
                    status=PlanStepStatus.TODO,
                    depends_on=["s0"],
                ),
                PlanStep(
                    step_id="s2",
                    role="tester",
                    agent_id="agent-tester",
                    description="test it",
                    order=2,
                    status=PlanStepStatus.TODO,
                    depends_on=["s1"],
                ),
            ],
            status=CollaborationPlanStatus.EXECUTING,
            chain_template="coder->reviewer->tester",
        )

        # This should NOT deadlock (collect inside lock, start outside)
        import threading

        result = [None]

        def restore():
            try:
                orch.restore_plans([plan], channel_id="channel-1")
                result[0] = "ok"
            except Exception as e:
                result[0] = str(e)

        t = threading.Thread(target=restore)
        t.start()
        t.join(timeout=5.0)

        assert not t.is_alive(), "restore_plans deadlocked"
        assert result[0] == "ok"

        # Step 1 (reviewer) should have been started
        assert plan.steps[1].status == PlanStepStatus.IN_PROGRESS
        orch.shutdown()
