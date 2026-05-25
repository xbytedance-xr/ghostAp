"""Tests for CollaborationOrchestrator.get_all_plans() public accessor.

Verifies:
1. Returns empty list when no plans exist
2. Returns snapshot list (not internal dict reference)
3. Snapshot is thread-safe (copy semantics)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.collaboration_orchestrator import CollaborationOrchestrator
from src.slock_engine.models import (
    CollaborationPlanStatus,
    SlockTask,
    TaskStatus,
)
from src.slock_engine.observer_queue import TaskStatusNotifier
from src.slock_engine.task_chain_manager import ChainStep, ChainTemplate, TaskChainManager


@pytest.fixture
def orchestrator():
    chain_template = ChainTemplate(
        name="coder->reviewer",
        steps=[ChainStep(role="coder", order=0), ChainStep(role="reviewer", order=1)],
    )
    chain_mgr = MagicMock(spec=TaskChainManager)
    chain_mgr.get_template_by_name.return_value = chain_template
    chain_mgr.find_chain_for_task.return_value = chain_template

    notifier = TaskStatusNotifier()
    resolve = MagicMock(side_effect=lambda role, ch: MagicMock(agent_id=f"agent-{role}", role=role))
    dispatch = MagicMock()

    with patch("src.slock_engine.collaboration_orchestrator.get_settings") as ms:
        settings = MagicMock()
        settings.slock_auto_plan_timeout = 9999
        settings.slock_role_response_timeout = 9999
        ms.return_value = settings
        orch = CollaborationOrchestrator(
            chain_manager=chain_mgr,
            notifier=notifier,
            resolve_agent=resolve,
            dispatch_task=dispatch,
            auto_plan_timeout=9999,
            role_response_timeout=9999,
        )
    yield orch
    orch.shutdown()


class TestGetAllPlans:
    def test_empty_when_no_plans(self, orchestrator):
        result = orchestrator.get_all_plans()
        assert result == []

    def test_returns_all_created_plans(self, orchestrator):
        t1 = SlockTask(task_id="t1", content="task 1", status=TaskStatus.TODO, created_in="ch")
        t2 = SlockTask(task_id="t2", content="task 2", status=TaskStatus.TODO, created_in="ch")

        orchestrator.create_plan(t1, channel_id="ch")
        orchestrator.create_plan(t2, channel_id="ch")

        plans = orchestrator.get_all_plans()
        assert len(plans) == 2
        plan_ids = {p.plan_id for p in plans}
        assert len(plan_ids) == 2

    def test_returns_copy_not_reference(self, orchestrator):
        """Mutating the returned list must not affect internal state."""
        t = SlockTask(task_id="t3", content="task 3", status=TaskStatus.TODO, created_in="ch")
        orchestrator.create_plan(t, channel_id="ch")

        plans = orchestrator.get_all_plans()
        plans.clear()

        # Internal state unchanged
        assert len(orchestrator.get_all_plans()) == 1

    def test_plans_contain_correct_data(self, orchestrator):
        t = SlockTask(task_id="t4", content="build feature", status=TaskStatus.TODO, created_in="ch")
        created = orchestrator.create_plan(t, channel_id="ch")

        plans = orchestrator.get_all_plans()
        assert plans[0].plan_id == created.plan_id
        assert plans[0].task_id == "t4"
        assert plans[0].status == CollaborationPlanStatus.PENDING_APPROVAL
