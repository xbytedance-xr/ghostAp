from dataclasses import replace

import pytest

from src.autonomous.models import (
    Effect,
    EffectEvent,
    EffectState,
    GoalCriterion,
    Plan,
    PlanEvent,
    PlanState,
    PlanStep,
    Run,
    RunEvent,
    RunState,
    StepEvent,
    StepState,
    TransitionRecord,
    TransitionRejected,
    transition_effect,
    transition_plan,
    transition_run,
    transition_step,
)


def test_run_happy_path_records_guarded_transitions() -> None:
    run = Run(run_id="run_1", state=RunState.RECEIVED, aggregate_version=0)

    run, record, emitted = transition_run(
        run,
        RunEvent.GOAL_DEFINED,
        expected_aggregate_version=0,
    )
    run, second, _ = transition_run(
        run,
        RunEvent.PLAN_VALIDATED,
        expected_aggregate_version=1,
    )
    run, third, _ = transition_run(
        run,
        RunEvent.ACTIVATED,
        expected_aggregate_version=2,
    )
    run, fourth, _ = transition_run(
        run,
        RunEvent.STARTED,
        expected_aggregate_version=3,
    )

    assert run.state is RunState.EXECUTING
    assert run.aggregate_version == 4
    assert isinstance(record, TransitionRecord)
    assert record.from_state == RunState.RECEIVED.value
    assert record.to_state == RunState.PLAN_READY.value
    assert record.aggregate_version == 1
    assert TransitionRecord.from_dict(record.to_dict()) == record
    assert emitted == ("run.plan_requested",)
    assert [
        second.to_state,
        third.to_state,
        fourth.to_state,
    ] == [
        RunState.PLAN_READY.value,
        RunState.SCHEDULED.value,
        RunState.EXECUTING.value,
    ]


def test_run_illegal_transition_fails_closed() -> None:
    run = Run(state=RunState.RECEIVED)

    with pytest.raises(TransitionRejected, match="illegal"):
        transition_run(run, RunEvent.VERIFICATION_PASSED)


def test_run_expected_version_is_required_when_provided() -> None:
    run = Run(state=RunState.RECEIVED, aggregate_version=3)

    with pytest.raises(TransitionRejected, match="aggregate version"):
        transition_run(
            run,
            RunEvent.GOAL_DEFINED,
            expected_aggregate_version=2,
        )


def test_run_supersede_waits_for_safe_drain_before_replan() -> None:
    run = Run(state=RunState.EXECUTING)

    draining, _, emitted = transition_run(
        run,
        RunEvent.SUPERSEDE_REQUESTED,
    )
    replanning, record, _ = transition_run(
        draining,
        RunEvent.SUPERSEDE_DRAINED,
    )

    assert draining.state is RunState.SUPERSEDED_PENDING_DRAIN
    assert emitted == ("run.safe_drain_requested",)
    assert replanning.state is RunState.REPLAN_PENDING
    assert record.guard == "old attempts reached a safe point"


def test_plan_state_machine_supersedes_only_after_drain() -> None:
    plan = Plan(state=PlanState.DRAFT)
    plan, _, _ = transition_plan(plan, PlanEvent.COMPILED)
    plan, _, _ = transition_plan(plan, PlanEvent.VALIDATED)
    plan, _, _ = transition_plan(plan, PlanEvent.ACTIVATED)
    plan, _, _ = transition_plan(plan, PlanEvent.SUPERSEDE_REQUESTED)

    assert plan.state is PlanState.SUPERSEDED_PENDING_DRAIN

    plan, record, _ = transition_plan(plan, PlanEvent.DRAINED)
    assert plan.state is PlanState.SUPERSEDED
    assert record.to_state == PlanState.SUPERSEDED.value


def test_step_retry_and_orphan_paths_are_explicit() -> None:
    step = PlanStep(step_id="step_1", state=StepState.PENDING)
    for event in (
        StepEvent.DEPENDENCIES_SATISFIED,
        StepEvent.LEASE_GRANTED,
        StepEvent.WORKER_STARTED,
        StepEvent.ATTEMPT_FAILED,
    ):
        step, _, _ = transition_step(
            step,
            event,
            expected_aggregate_version=step.aggregate_version,
        )
    assert step.state is StepState.RETRY_WAIT
    assert step.aggregate_version == 4

    step, _, _ = transition_step(step, StepEvent.RETRY_READY)
    step, _, _ = transition_step(step, StepEvent.LEASE_GRANTED)
    step, record, _ = transition_step(step, StepEvent.WORKER_LOST)
    assert step.state is StepState.ORPHANED
    assert record.aggregate_id == "step_1"


def test_step_reconciliation_rejection_skip_failure_and_cancel_paths() -> None:
    orphaned = PlanStep(step_id="orphaned", state=StepState.ORPHANED)
    reconciling, _, _ = transition_step(
        orphaned,
        StepEvent.RECONCILE_STARTED,
    )
    retry, _, _ = transition_step(
        reconciling,
        StepEvent.RECONCILE_RETRY,
    )
    assert retry.state is StepState.READY

    rejected = PlanStep(step_id="rejected", state=StepState.REJECTED)
    retry_wait, _, _ = transition_step(
        rejected,
        StepEvent.RETRY_AUTHORIZED,
    )
    failed, _, _ = transition_step(
        retry_wait,
        StepEvent.ATTEMPTS_EXHAUSTED,
    )
    assert failed.state is StepState.FAILED

    pending = PlanStep(step_id="skipped", state=StepState.PENDING)
    skipped, _, _ = transition_step(
        pending,
        StepEvent.SKIP_AUTHORIZED,
    )
    assert skipped.state is StepState.SKIPPED

    verifying = PlanStep(step_id="cancel", state=StepState.VERIFYING)
    cancelling, _, _ = transition_step(
        verifying,
        StepEvent.CANCEL_REQUESTED,
    )
    canceled, _, _ = transition_step(
        cancelling,
        StepEvent.CANCELED,
    )
    assert canceled.state is StepState.CANCELED


def test_plan_can_be_invalidated_before_and_after_validation() -> None:
    compiled = Plan(state=PlanState.COMPILED)
    invalid_compiled, _, _ = transition_plan(
        compiled,
        PlanEvent.INVALIDATED,
    )
    validated = Plan(state=PlanState.VALIDATED)
    invalid_validated, _, _ = transition_plan(
        validated,
        PlanEvent.INVALIDATED,
    )

    assert invalid_compiled.state is PlanState.INVALID
    assert invalid_validated.state is PlanState.INVALID


def test_effect_state_machine_requires_dispatch_and_reconciliation() -> None:
    effect = Effect(
        effect_instance_id="effect_1",
        effect_id="effect_1",
        state=EffectState.PROPOSED,
    )
    effect, _, _ = transition_effect(effect, EffectEvent.POLICY_ALLOWED)
    effect, _, _ = transition_effect(effect, EffectEvent.PREPARED)
    effect, _, _ = transition_effect(effect, EffectEvent.DISPATCH_STARTED)
    assert effect.state is EffectState.EXECUTING
    assert effect.active_dispatch is True

    effect, _, _ = transition_effect(effect, EffectEvent.DISPATCH_UNKNOWN)
    assert effect.state is EffectState.UNKNOWN_EFFECT
    assert effect.active_dispatch is False

    effect, _, _ = transition_effect(effect, EffectEvent.RECONCILE_STARTED)
    effect, _, _ = transition_effect(effect, EffectEvent.REMOTE_COMMITTED)
    assert effect.state is EffectState.COMMITTED


def test_effect_cannot_abort_after_dispatch_might_have_started() -> None:
    effect = Effect(
        effect_instance_id="effect_1",
        effect_id="effect_1",
        state=EffectState.EXECUTING,
        active_dispatch=True,
    )

    with pytest.raises(TransitionRejected, match="abort"):
        transition_effect(effect, EffectEvent.ABORT_BEFORE_DISPATCH)


def test_effect_denial_prepare_failure_and_negative_reconciliation_paths() -> None:
    proposed = Effect(
        effect_instance_id="effect_1",
        effect_id="effect_1",
        state=EffectState.PROPOSED,
    )
    denied, _, _ = transition_effect(proposed, EffectEvent.POLICY_DENIED)
    assert denied.state is EffectState.POLICY_DENIED

    allowed, _, _ = transition_effect(proposed, EffectEvent.POLICY_ALLOWED)
    aborted, _, _ = transition_effect(allowed, EffectEvent.PREPARE_FAILED)
    assert aborted.state is EffectState.ABORTED_NO_DISPATCH

    unknown = Effect(
        effect_instance_id="effect_2",
        effect_id="effect_2",
        state=EffectState.UNKNOWN_EFFECT,
    )
    reconciling, _, _ = transition_effect(
        unknown,
        EffectEvent.RECONCILE_STARTED,
    )
    failed_safe, _, _ = transition_effect(
        reconciling,
        EffectEvent.REMOTE_NOT_EXECUTED,
    )
    assert failed_safe.state is EffectState.FAILED_SAFE


def test_effect_state_and_active_dispatch_must_be_consistent() -> None:
    with pytest.raises(ValueError, match="active_dispatch"):
        Effect(
            effect_id="effect_1",
            effect_instance_id="effect_1",
            state=EffectState.PREPARED,
            active_dispatch=True,
        )
    with pytest.raises(ValueError, match="active_dispatch"):
        Effect(
            effect_id="effect_2",
            effect_instance_id="effect_2",
            state=EffectState.EXECUTING,
            active_dispatch=False,
        )


def test_manual_reconciliation_can_resolve_to_committed_or_failed_safe() -> None:
    manual = Effect(
        effect_id="effect_1",
        effect_instance_id="effect_1",
        state=EffectState.MANUAL_RECONCILIATION,
    )

    committed, _, _ = transition_effect(manual, EffectEvent.REMOTE_COMMITTED)
    failed_safe, _, _ = transition_effect(
        manual,
        EffectEvent.DISPATCH_FAILED_SAFE,
    )

    assert committed.state is EffectState.COMMITTED
    assert failed_safe.state is EffectState.FAILED_SAFE


def test_criterion_hash_is_stable_and_description_change_changes_hash() -> None:
    criterion = GoalCriterion(
        criterion_id="criterion_1",
        description="tests pass",
    )

    changed = replace(
        criterion,
        description="some tests pass",
        criterion_hash="",
    )

    assert criterion.criterion_hash != changed.criterion_hash
