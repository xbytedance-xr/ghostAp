"""Integration tests for step timeout handling in CollaborationOrchestrator.

Tests verify that:
1. A step that times out gets status PlanStepStatus.TIMED_OUT (not DONE)
2. After timeout, the next step is started
3. If all steps are done/timed_out/skipped, plan status becomes COMPLETED
4. Timeout on the only remaining step completes the plan
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.collaboration_orchestrator import CollaborationOrchestrator
from src.slock_engine.models import (
    AgentIdentity,
    CollaborationPlan,
    CollaborationPlanStatus,
    PlanStep,
    PlanStepStatus,
)
from src.slock_engine.observer_queue import TaskStatusNotifier

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_resolve_agent():
    """Returns a mock resolve_agent callable that always returns a valid agent."""
    agent = AgentIdentity(
        agent_id="agent-001",
        name="TestCoder",
        role="coder",
    )
    return MagicMock(return_value=agent)


@pytest.fixture
def mock_dispatch_task():
    """Returns a mock dispatch_task callable."""
    return MagicMock()


@pytest.fixture
def notifier():
    """Creates a real TaskStatusNotifier."""
    return TaskStatusNotifier()


@pytest.fixture
def orchestrator(mock_resolve_agent, mock_dispatch_task, notifier):
    """Creates a CollaborationOrchestrator with mocked dependencies.

    Uses a short role_response_timeout so timers are manageable in tests,
    but we call _handle_step_timeout directly to avoid flakiness.
    """
    with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
        settings = MagicMock()
        settings.slock_auto_plan_timeout = 9999  # effectively disable auto-start
        settings.slock_role_response_timeout = 5
        mock_settings.return_value = settings

        chain_manager = MagicMock()
        orch = CollaborationOrchestrator(
            chain_manager=chain_manager,
            notifier=notifier,
            resolve_agent=mock_resolve_agent,
            dispatch_task=mock_dispatch_task,
            auto_plan_timeout=9999,
            role_response_timeout=5,
        )
    return orch


def _make_plan_with_steps(
    orchestrator: CollaborationOrchestrator,
    num_steps: int = 3,
    channel_id: str = "ch-test",
) -> CollaborationPlan:
    """Helper: inject a plan in EXECUTING state with sequential steps.

    Steps are set up as a sequential chain (each depends on the previous).
    The first step is IN_PROGRESS, the rest are TODO.
    """
    steps = []
    for i in range(num_steps):
        step = PlanStep(
            step_id=str(uuid.uuid4()),
            role=f"role-{i}",
            agent_id=f"agent-{i:03d}",
            description=f"Step {i} work",
            order=i,
            status=PlanStepStatus.IN_PROGRESS if i == 0 else PlanStepStatus.TODO,
            task_id=f"task-{i}" if i == 0 else "",
            depends_on=[steps[i - 1].step_id] if i > 0 else [],
        )
        steps.append(step)

    plan = CollaborationPlan(
        plan_id=str(uuid.uuid4()),
        task_id="parent-task-001",
        steps=steps,
        status=CollaborationPlanStatus.EXECUTING,
        chain_template="test-chain",
        planner_agent_id="",
    )

    # Inject directly into orchestrator internal state
    orchestrator._plans[plan.plan_id] = plan
    orchestrator._task_to_plan[plan.task_id] = plan.plan_id
    orchestrator._channel_map[plan.plan_id] = channel_id

    return plan


# ---------------------------------------------------------------------------
# Test Cases
# ---------------------------------------------------------------------------


class TestStepTimeoutMarksTimedOut:
    """Test 1: A step that times out gets status PlanStepStatus.TIMED_OUT."""

    def test_timed_out_step_has_correct_status(self, orchestrator, mock_dispatch_task):
        """When _handle_step_timeout is called on an IN_PROGRESS step,
        its status must become TIMED_OUT — never DONE."""
        plan = _make_plan_with_steps(orchestrator, num_steps=3)
        target_step = plan.steps[0]

        assert target_step.status == PlanStepStatus.IN_PROGRESS

        orchestrator._handle_step_timeout(plan.plan_id, target_step.step_id)

        assert target_step.status == PlanStepStatus.TIMED_OUT
        assert target_step.status != PlanStepStatus.DONE

    def test_timeout_does_nothing_for_non_in_progress_step(self, orchestrator):
        """If the step is already DONE (e.g. completed before timer fires),
        the timeout handler should be a no-op."""
        plan = _make_plan_with_steps(orchestrator, num_steps=2)
        target_step = plan.steps[0]
        target_step.status = PlanStepStatus.DONE

        orchestrator._handle_step_timeout(plan.plan_id, target_step.step_id)

        # Status stays DONE, not overwritten to TIMED_OUT
        assert target_step.status == PlanStepStatus.DONE

    def test_timeout_does_nothing_for_non_executing_plan(self, orchestrator):
        """If the plan is no longer EXECUTING, timeout is a no-op."""
        plan = _make_plan_with_steps(orchestrator, num_steps=2)
        plan.status = CollaborationPlanStatus.CANCELLED
        target_step = plan.steps[0]

        orchestrator._handle_step_timeout(plan.plan_id, target_step.step_id)

        # Step untouched
        assert target_step.status == PlanStepStatus.IN_PROGRESS


class TestTimeoutStartsNextStep:
    """Test 2: After timeout, the next step is started."""

    def test_next_step_dispatched_after_timeout(
        self, orchestrator, mock_resolve_agent, mock_dispatch_task
    ):
        """After the first step times out, _start_next_step should dispatch
        a task for the next TODO step in the chain."""
        plan = _make_plan_with_steps(orchestrator, num_steps=3)
        first_step = plan.steps[0]
        second_step = plan.steps[1]

        assert second_step.status == PlanStepStatus.TODO

        orchestrator._handle_step_timeout(plan.plan_id, first_step.step_id)

        # The second step should now be IN_PROGRESS
        assert second_step.status == PlanStepStatus.IN_PROGRESS
        # dispatch_task should have been called for the new step
        assert mock_dispatch_task.called

    def test_resolve_agent_called_for_next_step(
        self, orchestrator, mock_resolve_agent, mock_dispatch_task
    ):
        """After timeout, resolve_agent is called for the next step's role."""
        plan = _make_plan_with_steps(orchestrator, num_steps=2)
        first_step = plan.steps[0]

        orchestrator._handle_step_timeout(plan.plan_id, first_step.step_id)

        # resolve_agent should be called for the second step's role
        calls = mock_resolve_agent.call_args_list
        # At least one call should be for role-1 (second step)
        role_args = [call[0][0] for call in calls]
        assert "role-1" in role_args


class TestAllTerminalStatusesCompletePlan:
    """Test 3: If all steps are done/timed_out/skipped, plan becomes COMPLETED."""

    def test_plan_completed_when_all_steps_terminal(self, orchestrator, mock_dispatch_task):
        """Plan with mixed DONE, TIMED_OUT, and SKIPPED steps is COMPLETED."""
        plan = _make_plan_with_steps(orchestrator, num_steps=3)

        # Manually set first two steps to terminal states
        plan.steps[0].status = PlanStepStatus.DONE
        plan.steps[1].status = PlanStepStatus.SKIPPED
        # Third step is IN_PROGRESS (will be timed out)
        plan.steps[2].status = PlanStepStatus.IN_PROGRESS
        plan.steps[2].depends_on = []  # remove dependency for simplicity

        orchestrator._handle_step_timeout(plan.plan_id, plan.steps[2].step_id)

        assert plan.steps[2].status == PlanStepStatus.TIMED_OUT
        assert plan.status == CollaborationPlanStatus.COMPLETED

    def test_plan_not_completed_when_todo_steps_remain(self, orchestrator, mock_dispatch_task):
        """Plan should remain EXECUTING if there are still TODO steps after timeout."""
        plan = _make_plan_with_steps(orchestrator, num_steps=3)
        first_step = plan.steps[0]

        # Steps 1 and 2 are still TODO
        orchestrator._handle_step_timeout(plan.plan_id, first_step.step_id)

        # Plan should still be executing (next step started)
        assert plan.status == CollaborationPlanStatus.EXECUTING

    def test_plan_completed_with_all_done(self, orchestrator):
        """Plan becomes COMPLETED when all steps are DONE (no timeout involved
        for the final step — verifying the completion check logic)."""
        plan = _make_plan_with_steps(orchestrator, num_steps=2)

        plan.steps[0].status = PlanStepStatus.DONE
        plan.steps[1].status = PlanStepStatus.IN_PROGRESS

        orchestrator._handle_step_timeout(plan.plan_id, plan.steps[1].step_id)

        assert plan.steps[1].status == PlanStepStatus.TIMED_OUT
        assert plan.status == CollaborationPlanStatus.COMPLETED


class TestTimeoutOnOnlyRemainingStepCompletesPlan:
    """Test 4: Timeout on the only remaining step completes the plan."""

    def test_single_step_plan_timeout_completes(self, orchestrator):
        """A plan with a single IN_PROGRESS step that times out should
        have its status become COMPLETED."""
        plan = _make_plan_with_steps(orchestrator, num_steps=1)
        only_step = plan.steps[0]

        assert only_step.status == PlanStepStatus.IN_PROGRESS
        assert plan.status == CollaborationPlanStatus.EXECUTING

        orchestrator._handle_step_timeout(plan.plan_id, only_step.step_id)

        assert only_step.status == PlanStepStatus.TIMED_OUT
        assert plan.status == CollaborationPlanStatus.COMPLETED

    def test_last_remaining_step_timeout_completes(self, orchestrator):
        """When previous steps are already DONE and the last IN_PROGRESS step
        times out, the plan should become COMPLETED."""
        plan = _make_plan_with_steps(orchestrator, num_steps=3)

        # First two steps already done
        plan.steps[0].status = PlanStepStatus.DONE
        plan.steps[1].status = PlanStepStatus.DONE
        # Last step is the one that will timeout
        plan.steps[2].status = PlanStepStatus.IN_PROGRESS
        plan.steps[2].depends_on = []

        orchestrator._handle_step_timeout(plan.plan_id, plan.steps[2].step_id)

        assert plan.steps[2].status == PlanStepStatus.TIMED_OUT
        assert plan.status == CollaborationPlanStatus.COMPLETED

    def test_last_remaining_with_mixed_terminal_states(self, orchestrator):
        """Plan with DONE + SKIPPED + last step TIMED_OUT -> COMPLETED."""
        plan = _make_plan_with_steps(orchestrator, num_steps=4)

        plan.steps[0].status = PlanStepStatus.DONE
        plan.steps[1].status = PlanStepStatus.SKIPPED
        plan.steps[2].status = PlanStepStatus.TIMED_OUT
        plan.steps[3].status = PlanStepStatus.IN_PROGRESS
        plan.steps[3].depends_on = []

        orchestrator._handle_step_timeout(plan.plan_id, plan.steps[3].step_id)

        assert plan.steps[3].status == PlanStepStatus.TIMED_OUT
        assert plan.status == CollaborationPlanStatus.COMPLETED

    def test_completed_plan_does_not_dispatch_next(
        self, orchestrator, mock_dispatch_task
    ):
        """When timeout on the last step completes the plan, no further
        dispatch_task calls should be made."""
        plan = _make_plan_with_steps(orchestrator, num_steps=1)

        mock_dispatch_task.reset_mock()
        orchestrator._handle_step_timeout(plan.plan_id, plan.steps[0].step_id)

        assert plan.status == CollaborationPlanStatus.COMPLETED
        mock_dispatch_task.assert_not_called()
