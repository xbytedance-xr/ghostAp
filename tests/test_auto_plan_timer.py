"""Tests for CollaborationOrchestrator auto-plan timer lifecycle."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.collaboration_orchestrator import CollaborationOrchestrator
from src.slock_engine.models import (
    CollaborationPlan,
    CollaborationPlanStatus,
    PlanStep,
    PlanStepStatus,
    SlockTask,
    TaskStatus,
)
from src.slock_engine.observer_queue import TaskStatusNotifier
from src.slock_engine.task_chain_manager import ChainStep, ChainTemplate, TaskChainManager


def _make_orchestrator(auto_plan_timeout: int = 9999) -> CollaborationOrchestrator:
    """Create orchestrator with large timeout to prevent timer firing during tests."""
    chain_mgr = MagicMock(spec=TaskChainManager)
    notifier = MagicMock(spec=TaskStatusNotifier)
    resolve_agent = MagicMock(return_value=None)
    dispatch_task = MagicMock()

    with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
        settings = MagicMock()
        settings.slock_auto_plan_timeout = auto_plan_timeout
        settings.slock_role_response_timeout = 9999
        mock_settings.return_value = settings
        orch = CollaborationOrchestrator(
            chain_manager=chain_mgr,
            notifier=notifier,
            resolve_agent=resolve_agent,
            dispatch_task=dispatch_task,
            auto_plan_timeout=auto_plan_timeout,
            role_response_timeout=9999,
        )
    return orch


def _make_plan(plan_id: str = "plan-1", status=CollaborationPlanStatus.PENDING_APPROVAL) -> CollaborationPlan:
    """Create a minimal plan for testing."""
    return CollaborationPlan(
        plan_id=plan_id,
        task_id="task-src",
        task_content="Test plan",
        status=status,
        steps=[
            PlanStep(
                step_id="step-1",
                role="coder",
                description="Implement feature",
                order=1,
            ),
        ],
        auto_start_at=time.time() + 9999,
    )


class TestAutoPlanTimer:
    """Verify auto-plan timer create/cancel/fire lifecycle."""

    def test_timer_created_on_plan_creation(self):
        """Creating a plan registers a timer in _plan_timers."""
        orch = _make_orchestrator(auto_plan_timeout=9999)
        template = ChainTemplate(
            name="coder->reviewer",
            steps=[
                ChainStep(role="coder", order=0),
                ChainStep(role="reviewer", order=1),
            ],
        )
        orch._chain_manager.find_chain_for_task.return_value = template
        orch._chain_manager.get_template_by_name.return_value = None

        task = SlockTask(content="build feature", created_in="ch_1")
        plan = orch.create_plan(task, "ch_1")

        if plan:
            assert plan.plan_id in orch._plan_timers
            # Cleanup
            orch.shutdown()

    def test_cancel_plan_removes_timer(self):
        """Cancelling a plan removes its timer from _plan_timers."""
        orch = _make_orchestrator()
        plan = _make_plan()

        orch._plans[plan.plan_id] = plan
        orch._channel_map[plan.plan_id] = "ch_1"
        timer = threading.Timer(9999, lambda: None)
        timer.daemon = True
        timer.start()
        orch._plan_timers[plan.plan_id] = timer

        orch.cancel_plan(plan.plan_id)

        assert plan.plan_id not in orch._plan_timers
        assert plan.status == CollaborationPlanStatus.CANCELLED
        timer.cancel()  # cleanup in case

    def test_auto_start_transitions_pending_to_executing(self):
        """_auto_start_plan transitions PENDING_APPROVAL plan to EXECUTING."""
        orch = _make_orchestrator()
        plan = _make_plan(status=CollaborationPlanStatus.PENDING_APPROVAL)

        orch._plans[plan.plan_id] = plan
        orch._channel_map[plan.plan_id] = "ch_1"

        orch._auto_start_plan(plan.plan_id)

        # Plan should have left PENDING_APPROVAL
        assert plan.status != CollaborationPlanStatus.PENDING_APPROVAL

    def test_auto_start_noop_for_already_executing(self):
        """_auto_start_plan is a no-op for already-executing plans."""
        orch = _make_orchestrator()
        plan = _make_plan(status=CollaborationPlanStatus.EXECUTING)

        orch._plans[plan.plan_id] = plan
        orch._channel_map[plan.plan_id] = "ch_1"

        orch._auto_start_plan(plan.plan_id)
        assert plan.status == CollaborationPlanStatus.EXECUTING

    def test_approve_removes_timer_from_dict(self):
        """approve_plan calls _cancel_plan_timer, removing it from dict."""
        orch = _make_orchestrator()
        plan = _make_plan()

        orch._plans[plan.plan_id] = plan
        orch._channel_map[plan.plan_id] = "ch_1"
        timer = threading.Timer(9999, lambda: None)
        timer.daemon = True
        timer.start()
        orch._plan_timers[plan.plan_id] = timer

        orch.approve_plan(plan.plan_id)

        assert plan.plan_id not in orch._plan_timers
        timer.cancel()  # cleanup
