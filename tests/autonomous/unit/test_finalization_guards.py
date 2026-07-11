import pytest

from src.autonomous.models import (
    CriterionMutationError,
    Effect,
    EffectDisposition,
    EffectDispositionType,
    EffectState,
    GoalCriterion,
    Run,
    RunEvent,
    RunState,
    RunTransitionContext,
    TransitionRejected,
    assert_replan_criteria_compatible,
    transition_run,
)


def test_run_cannot_succeed_with_unresolved_effect() -> None:
    run = Run(state=RunState.VERIFYING)

    with pytest.raises(TransitionRejected, match="unresolved"):
        transition_run(
            run,
            RunEvent.VERIFICATION_PASSED,
            context=RunTransitionContext(
                run_id=run.run_id,
                plan_epoch=run.plan_epoch,
                source_sequence=1,
                unresolved_effect_ids=("effect_1",),
                criteria_verified=True,
                verification_attestation_ids=("attestation_1",),
                finalization_record_id="finalization_1",
            ),
        )


def test_run_cannot_succeed_with_undisposed_committed_effect() -> None:
    run = Run(state=RunState.VERIFYING)

    with pytest.raises(TransitionRejected, match="disposition"):
        transition_run(
            run,
            RunEvent.VERIFICATION_PASSED,
            context=RunTransitionContext(
                run_id=run.run_id,
                plan_epoch=run.plan_epoch,
                source_sequence=1,
                undisposed_committed_effect_ids=("effect_1",),
                criteria_verified=True,
                verification_attestation_ids=("attestation_1",),
                finalization_record_id="finalization_1",
            ),
        )


def test_run_cannot_succeed_without_all_criteria_verified() -> None:
    run = Run(state=RunState.VERIFYING)

    with pytest.raises(TransitionRejected, match="criteria"):
        transition_run(
            run,
            RunEvent.VERIFICATION_PASSED,
            context=RunTransitionContext(
                run_id=run.run_id,
                plan_epoch=run.plan_epoch,
                source_sequence=1,
                criteria_verified=False,
                verification_attestation_ids=("attestation_1",),
                finalization_record_id="finalization_1",
            ),
        )


def test_verified_run_can_succeed_when_finalization_is_clear() -> None:
    run = Run(state=RunState.VERIFYING)

    updated, record, emitted = transition_run(
        run,
        RunEvent.VERIFICATION_PASSED,
        context=RunTransitionContext(
            run_id=run.run_id,
            plan_epoch=run.plan_epoch,
            source_sequence=1,
            criteria_verified=True,
            verification_attestation_ids=("attestation_1",),
            finalization_record_id="finalization_1",
        ),
    )

    assert updated.state is RunState.SUCCEEDED
    assert record.guard == "criteria verified and effects finalized"
    assert emitted == ("run.terminal_report_required",)


def test_human_acceptance_requires_dedicated_event() -> None:
    run = Run(state=RunState.ACCEPTANCE_PENDING)

    with pytest.raises(TransitionRejected, match="illegal"):
        transition_run(
            run,
            RunEvent.VERIFICATION_PASSED,
            context=RunTransitionContext(
                run_id=run.run_id,
                plan_epoch=run.plan_epoch,
                source_sequence=1,
                criteria_verified=True,
                verification_attestation_ids=("attestation_1",),
                finalization_record_id="finalization_1",
            ),
        )

    updated, record, _ = transition_run(
        run,
        RunEvent.HUMAN_ACCEPTED,
        context=RunTransitionContext(
            run_id=run.run_id,
            plan_epoch=run.plan_epoch,
            source_sequence=1,
            criteria_verified=True,
            finalization_record_id="finalization_1",
            human_acceptance_decision_id="decision_1",
            actor_principal_id="principal_owner",
            decision_nonce_consumed=True,
            decision_epoch_valid=True,
        ),
    )

    assert updated.state is RunState.SUCCEEDED
    assert record.guard == "human acceptance recorded and effects finalized"


def test_success_requires_attestation_and_finalization_records() -> None:
    run = Run(state=RunState.VERIFYING)

    with pytest.raises(TransitionRejected, match="attestation"):
        transition_run(
            run,
            RunEvent.VERIFICATION_PASSED,
            context=RunTransitionContext(
                run_id=run.run_id,
                plan_epoch=run.plan_epoch,
                source_sequence=1,
                criteria_verified=True,
                finalization_record_id="finalization_1",
            ),
        )
    with pytest.raises(TransitionRejected, match="finalization"):
        transition_run(
            run,
            RunEvent.VERIFICATION_PASSED,
            context=RunTransitionContext(
                run_id=run.run_id,
                plan_epoch=run.plan_epoch,
                source_sequence=1,
                criteria_verified=True,
                verification_attestation_ids=("attestation_1",),
            ),
        )


def test_human_acceptance_requires_authenticated_decision_record() -> None:
    run = Run(state=RunState.ACCEPTANCE_PENDING)

    with pytest.raises(TransitionRejected, match="decision"):
        transition_run(
            run,
            RunEvent.HUMAN_ACCEPTED,
            context=RunTransitionContext(
                run_id=run.run_id,
                plan_epoch=run.plan_epoch,
                source_sequence=1,
                criteria_verified=True,
                finalization_record_id="finalization_1",
            ),
        )


def test_terminal_context_must_be_bound_to_current_run_plan_and_journal() -> None:
    run = Run(run_id="run_1", plan_epoch=3, state=RunState.VERIFYING)

    for context in (
        RunTransitionContext(
            run_id="other",
            plan_epoch=3,
            source_sequence=1,
            criteria_verified=True,
            verification_attestation_ids=("attestation_1",),
            finalization_record_id="finalization_1",
        ),
        RunTransitionContext(
            run_id="run_1",
            plan_epoch=2,
            source_sequence=1,
            criteria_verified=True,
            verification_attestation_ids=("attestation_1",),
            finalization_record_id="finalization_1",
        ),
        RunTransitionContext(
            run_id="run_1",
            plan_epoch=3,
            source_sequence=0,
            criteria_verified=True,
            verification_attestation_ids=("attestation_1",),
            finalization_record_id="finalization_1",
        ),
    ):
        with pytest.raises(TransitionRejected, match="context"):
            transition_run(
                run,
                RunEvent.VERIFICATION_PASSED,
                context=context,
            )


def test_cancel_with_unresolved_effect_enters_reconciliation_pending() -> None:
    run = Run(state=RunState.CANCELLING)

    updated, _, _ = transition_run(
        run,
        RunEvent.CANCEL_DRAINED,
        context=RunTransitionContext(
            run_id=run.run_id,
            plan_epoch=run.plan_epoch,
            source_sequence=1,
            unresolved_effect_ids=("effect_1",),
        ),
    )

    assert updated.state is RunState.RECONCILIATION_PENDING
    assert updated.target_terminal_state is RunState.CANCELED


def test_reconciliation_finalizes_to_recorded_target_only_when_clear() -> None:
    run = Run(
        state=RunState.RECONCILIATION_PENDING,
        target_terminal_state=RunState.FAILED,
    )

    with pytest.raises(TransitionRejected):
        transition_run(
            run,
            RunEvent.RECONCILIATION_COMPLETED,
            context=RunTransitionContext(
                run_id=run.run_id,
                plan_epoch=run.plan_epoch,
                source_sequence=1,
                unresolved_effect_ids=("effect_1",),
            ),
        )

    updated, _, _ = transition_run(
        run,
        RunEvent.RECONCILIATION_COMPLETED,
        context=RunTransitionContext(
            run_id=run.run_id,
            plan_epoch=run.plan_epoch,
            source_sequence=1,
        ),
    )
    assert updated.state is RunState.FAILED


def test_replan_cannot_delete_or_weaken_criteria() -> None:
    original = GoalCriterion(
        criterion_id="criterion_1",
        description="all tests pass",
    )
    weakened = GoalCriterion(
        criterion_id="criterion_1",
        description="some tests pass",
    )

    with pytest.raises(CriterionMutationError, match="changed"):
        assert_replan_criteria_compatible((original,), (weakened,))
    with pytest.raises(CriterionMutationError, match="missing"):
        assert_replan_criteria_compatible((original,), ())


def test_effect_disposition_requires_compatible_effect_state() -> None:
    committed = Effect(
        effect_id="effect_1",
        effect_instance_id="effect_1",
        state=EffectState.COMMITTED,
    )
    unknown = Effect(
        effect_id="effect_2",
        effect_instance_id="effect_2",
        state=EffectState.UNKNOWN_EFFECT,
    )

    retained = EffectDisposition.create(
        committed,
        EffectDispositionType.RETAINED,
        actor_principal_id="principal_admin",
    )
    assert retained.effect_instance_id == committed.effect_instance_id
    assert EffectDisposition.from_dict(retained.to_dict()) == retained

    with pytest.raises(TypeError):
        EffectDisposition(  # type: ignore[call-arg]
            disposition_id="bypass",
            effect_instance_id=committed.effect_instance_id,
            disposition=EffectDispositionType.RETAINED,
            actor_principal_id="principal_admin",
            created_at=0,
            _validated=True,
        )

    with pytest.raises(ValueError, match="unresolved"):
        EffectDisposition.create(
            unknown,
            EffectDispositionType.RETAINED,
            actor_principal_id="principal_admin",
        )
