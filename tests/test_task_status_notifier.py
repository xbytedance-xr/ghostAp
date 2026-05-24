"""Unit tests for TaskStatusNotifier and TaskStatusObserver."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.slock_engine.observer_queue import TaskStatusNotifier, TaskStatusObserver


class MockObserver:
    """Mock observer implementing the TaskStatusObserver protocol."""

    def __init__(self) -> None:
        self.status_changed_calls: list[tuple[str, str, str, str, str]] = []
        self.plan_step_calls: list[tuple[str, str, str, str]] = []

    def on_task_status_changed(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
        agent_id: str,
        channel_id: str,
    ) -> None:
        self.status_changed_calls.append((task_id, old_status, new_status, agent_id, channel_id))

    def on_plan_step_completed(
        self,
        plan_id: str,
        step_id: str,
        role: str,
        agent_id: str,
    ) -> None:
        self.plan_step_calls.append((plan_id, step_id, role, agent_id))


class TestSubscribeUnsubscribe:
    """Tests for subscribe and unsubscribe behavior."""

    def test_subscribe_adds_observer(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()

        notifier.subscribe(obs)

        assert notifier.observer_count == 1

    def test_subscribe_multiple_observers(self) -> None:
        notifier = TaskStatusNotifier()
        obs1 = MockObserver()
        obs2 = MockObserver()

        notifier.subscribe(obs1)
        notifier.subscribe(obs2)

        assert notifier.observer_count == 2

    def test_subscribe_same_observer_twice_is_idempotent(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()

        notifier.subscribe(obs)
        notifier.subscribe(obs)

        assert notifier.observer_count == 1

    def test_unsubscribe_removes_observer(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()

        notifier.subscribe(obs)
        notifier.unsubscribe(obs)

        assert notifier.observer_count == 0

    def test_unsubscribe_nonexistent_observer_does_not_error(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()

        # Should not raise
        notifier.unsubscribe(obs)

        assert notifier.observer_count == 0


class TestNotifyStatusChanged:
    """Tests for notify_status_changed dispatching."""

    def test_dispatches_to_single_observer(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()
        notifier.subscribe(obs)

        notifier.notify_status_changed("task-1", "todo", "in_progress", "agent-a", "ch-001")

        assert len(obs.status_changed_calls) == 1
        assert obs.status_changed_calls[0] == ("task-1", "todo", "in_progress", "agent-a", "ch-001")

    def test_dispatches_to_all_subscribers(self) -> None:
        notifier = TaskStatusNotifier()
        obs1 = MockObserver()
        obs2 = MockObserver()
        obs3 = MockObserver()
        notifier.subscribe(obs1)
        notifier.subscribe(obs2)
        notifier.subscribe(obs3)

        notifier.notify_status_changed("task-2", "in_progress", "done", "agent-b", "ch-002")

        for obs in (obs1, obs2, obs3):
            assert len(obs.status_changed_calls) == 1
            assert obs.status_changed_calls[0] == (
                "task-2", "in_progress", "done", "agent-b", "ch-002"
            )

    def test_multiple_notifications_accumulate(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()
        notifier.subscribe(obs)

        notifier.notify_status_changed("t1", "a", "b", "ag1", "c1")
        notifier.notify_status_changed("t2", "b", "c", "ag2", "c2")

        assert len(obs.status_changed_calls) == 2


class TestNotifyPlanStepCompleted:
    """Tests for notify_plan_step_completed dispatching."""

    def test_dispatches_to_single_observer(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()
        notifier.subscribe(obs)

        notifier.notify_plan_step_completed("plan-1", "step-1", "coder", "agent-x")

        assert len(obs.plan_step_calls) == 1
        assert obs.plan_step_calls[0] == ("plan-1", "step-1", "coder", "agent-x")

    def test_dispatches_to_all_subscribers(self) -> None:
        notifier = TaskStatusNotifier()
        obs1 = MockObserver()
        obs2 = MockObserver()
        notifier.subscribe(obs1)
        notifier.subscribe(obs2)

        notifier.notify_plan_step_completed("plan-2", "step-3", "reviewer", "agent-y")

        for obs in (obs1, obs2):
            assert len(obs.plan_step_calls) == 1
            assert obs.plan_step_calls[0] == ("plan-2", "step-3", "reviewer", "agent-y")

    def test_multiple_plan_step_notifications(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()
        notifier.subscribe(obs)

        notifier.notify_plan_step_completed("p1", "s1", "coder", "a1")
        notifier.notify_plan_step_completed("p1", "s2", "reviewer", "a2")

        assert len(obs.plan_step_calls) == 2
        assert obs.plan_step_calls[0] == ("p1", "s1", "coder", "a1")
        assert obs.plan_step_calls[1] == ("p1", "s2", "reviewer", "a2")


class TestUnsubscribedObserverDoesNotReceive:
    """Tests that unsubscribed observers are excluded from notifications."""

    def test_unsubscribed_observer_misses_status_changed(self) -> None:
        notifier = TaskStatusNotifier()
        obs1 = MockObserver()
        obs2 = MockObserver()
        notifier.subscribe(obs1)
        notifier.subscribe(obs2)

        notifier.unsubscribe(obs1)
        notifier.notify_status_changed("task-5", "todo", "done", "agent-z", "ch-005")

        assert len(obs1.status_changed_calls) == 0
        assert len(obs2.status_changed_calls) == 1

    def test_unsubscribed_observer_misses_plan_step_completed(self) -> None:
        notifier = TaskStatusNotifier()
        obs1 = MockObserver()
        obs2 = MockObserver()
        notifier.subscribe(obs1)
        notifier.subscribe(obs2)

        notifier.unsubscribe(obs2)
        notifier.notify_plan_step_completed("plan-x", "step-x", "tester", "agent-t")

        assert len(obs1.plan_step_calls) == 1
        assert len(obs2.plan_step_calls) == 0

    def test_observer_receives_before_unsub_but_not_after(self) -> None:
        notifier = TaskStatusNotifier()
        obs = MockObserver()
        notifier.subscribe(obs)

        notifier.notify_status_changed("t1", "a", "b", "ag", "ch")
        assert len(obs.status_changed_calls) == 1

        notifier.unsubscribe(obs)
        notifier.notify_status_changed("t2", "b", "c", "ag", "ch")
        assert len(obs.status_changed_calls) == 1  # no new call


class TestChainProgressionViaNotifier:
    """Tests that notifier-driven events trigger chain progression in an orchestrator observer."""

    @pytest.fixture
    def notifier(self) -> TaskStatusNotifier:
        return TaskStatusNotifier()

    @pytest.fixture
    def chain_manager(self):
        from src.slock_engine.task_chain_manager import ChainStep, ChainTemplate, TaskChainManager

        template = ChainTemplate(
            name="coder->reviewer->tester",
            steps=[
                ChainStep(role="coder", order=0),
                ChainStep(role="reviewer", order=1),
                ChainStep(role="tester", order=2),
            ],
        )
        cm = MagicMock(spec=TaskChainManager)
        cm.get_template_by_name.return_value = template
        cm.find_chain_for_task.return_value = template
        return cm

    @pytest.fixture
    def orchestrator(self, notifier, chain_manager):
        from src.slock_engine.collaboration_orchestrator import CollaborationOrchestrator
        from src.slock_engine.models import AgentIdentity

        resolve_agent = MagicMock(
            side_effect=lambda role, channel: AgentIdentity(agent_id=f"agent-{role}", role=role)
        )
        dispatch_task = MagicMock()
        orch = CollaborationOrchestrator(
            chain_manager=chain_manager,
            notifier=notifier,
            resolve_agent=resolve_agent,
            dispatch_task=dispatch_task,
        )
        notifier.subscribe(orch)
        return orch

    @pytest.fixture
    def executing_plan(self, orchestrator):
        from src.slock_engine.models import SlockTask, TaskStatus

        task = SlockTask(task_id="chain-task", content="Build feature", status=TaskStatus.TODO)
        plan = orchestrator.create_plan(task, channel_id="ch-chain")
        orchestrator.approve_plan(plan.plan_id)
        return plan

    def test_notify_status_changed_advances_plan_step(self, notifier, executing_plan):
        """When notifier dispatches a DONE event, the orchestrator advances to next step."""
        from src.slock_engine.models import PlanStepStatus

        step0 = executing_plan.steps[0]
        # Simulate task completion via the notifier (as the engine would)
        notifier.notify_status_changed(
            step0.task_id, "in_progress", "done", "agent-coder", "ch-chain"
        )

        assert step0.status == PlanStepStatus.DONE
        # Next step should now be IN_PROGRESS
        assert executing_plan.steps[1].status == PlanStepStatus.IN_PROGRESS

    def test_notify_fires_plan_step_completed_event(self, notifier, executing_plan):
        """Completing a step via notifier fires notify_plan_step_completed to other observers."""
        spy = MockObserver()
        notifier.subscribe(spy)

        step0 = executing_plan.steps[0]
        notifier.notify_status_changed(
            step0.task_id, "in_progress", "done", "agent-coder", "ch-chain"
        )

        assert len(spy.plan_step_calls) == 1
        assert spy.plan_step_calls[0][0] == executing_plan.plan_id
        assert spy.plan_step_calls[0][1] == step0.step_id
        assert spy.plan_step_calls[0][2] == "coder"

    def test_full_chain_progression_completes_plan(self, notifier, executing_plan):
        """Completing all steps sequentially via notifier results in COMPLETED plan."""
        from src.slock_engine.models import CollaborationPlanStatus, PlanStepStatus

        for step in executing_plan.steps:
            notifier.notify_status_changed(
                step.task_id, "in_progress", "done", f"agent-{step.role}", "ch-chain"
            )

        assert executing_plan.status == CollaborationPlanStatus.COMPLETED
        assert all(s.status == PlanStepStatus.DONE for s in executing_plan.steps)

    def test_non_done_status_does_not_advance(self, notifier, executing_plan):
        """Non-DONE status changes are ignored by the orchestrator."""
        from src.slock_engine.models import PlanStepStatus

        step0 = executing_plan.steps[0]
        notifier.notify_status_changed(
            step0.task_id, "todo", "in_progress", "agent-coder", "ch-chain"
        )

        # Step should remain IN_PROGRESS (unchanged)
        assert step0.status == PlanStepStatus.IN_PROGRESS
        assert executing_plan.steps[1].status == PlanStepStatus.TODO


class TestObserverExceptionHandling:
    """Tests that observer exceptions do not crash the notifier."""

    def test_exception_in_on_task_status_changed_does_not_crash(self) -> None:
        notifier = TaskStatusNotifier()
        bad_obs = MagicMock(spec=TaskStatusObserver)
        bad_obs.on_task_status_changed.side_effect = RuntimeError("observer exploded")
        good_obs = MockObserver()

        notifier.subscribe(bad_obs)
        notifier.subscribe(good_obs)

        # Should not raise despite bad_obs throwing
        notifier.notify_status_changed("task-err", "a", "b", "ag", "ch")

        # The good observer still receives the notification
        assert len(good_obs.status_changed_calls) == 1
        assert good_obs.status_changed_calls[0] == ("task-err", "a", "b", "ag", "ch")

    def test_exception_in_on_plan_step_completed_does_not_crash(self) -> None:
        notifier = TaskStatusNotifier()
        bad_obs = MagicMock(spec=TaskStatusObserver)
        bad_obs.on_plan_step_completed.side_effect = ValueError("boom")
        good_obs = MockObserver()

        notifier.subscribe(bad_obs)
        notifier.subscribe(good_obs)

        # Should not raise despite bad_obs throwing
        notifier.notify_plan_step_completed("plan-err", "step-err", "coder", "ag")

        assert len(good_obs.plan_step_calls) == 1
        assert good_obs.plan_step_calls[0] == ("plan-err", "step-err", "coder", "ag")

    def test_all_observers_called_even_if_first_throws(self) -> None:
        notifier = TaskStatusNotifier()
        obs1 = MagicMock(spec=TaskStatusObserver)
        obs1.on_task_status_changed.side_effect = Exception("fail")
        obs2 = MockObserver()
        obs3 = MockObserver()

        notifier.subscribe(obs1)
        notifier.subscribe(obs2)
        notifier.subscribe(obs3)

        notifier.notify_status_changed("t", "old", "new", "a", "c")

        # obs2 and obs3 still get called
        assert len(obs2.status_changed_calls) == 1
        assert len(obs3.status_changed_calls) == 1
