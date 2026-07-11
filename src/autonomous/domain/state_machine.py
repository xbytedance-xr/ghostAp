"""Pure guarded state transitions for autonomous aggregates."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from enum import Enum

from .effects import Effect
from .enums import (
    EffectEvent,
    EffectState,
    PlanEvent,
    PlanState,
    RunEvent,
    RunState,
    StepEvent,
    StepState,
)
from .goals import GoalCriterion, Run
from .ids import strict_bool, strict_float, strict_int, strict_str
from .plans import Plan, PlanStep


class TransitionRejected(ValueError):
    """A requested state change violates its transition guards."""


class CriterionMutationError(ValueError):
    """A replan attempted to remove or alter an acceptance criterion."""


@dataclass(frozen=True)
class TransitionRecord:
    """Audit-ready description of one aggregate transition."""

    aggregate_id: str
    aggregate_type: str
    from_state: str
    event: str
    guard: str
    to_state: str
    owner_activity: str
    aggregate_version: int
    durable_side_effects: tuple[str, ...]
    audit_event: str
    fsync_before_ack: bool
    timestamp: float

    def to_dict(self) -> dict[str, object]:
        return {
            "aggregate_id": self.aggregate_id,
            "aggregate_type": self.aggregate_type,
            "from_state": self.from_state,
            "event": self.event,
            "guard": self.guard,
            "to_state": self.to_state,
            "owner_activity": self.owner_activity,
            "aggregate_version": self.aggregate_version,
            "durable_side_effects": list(self.durable_side_effects),
            "audit_event": self.audit_event,
            "fsync_before_ack": self.fsync_before_ack,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> TransitionRecord:
        side_effects = data.get("durable_side_effects", ())
        if not isinstance(side_effects, (list, tuple)) or not all(
            isinstance(value, str) for value in side_effects
        ):
            raise ValueError("durable_side_effects must contain strings")
        return cls(
            aggregate_id=strict_str(data["aggregate_id"], "aggregate_id"),
            aggregate_type=strict_str(
                data["aggregate_type"],
                "aggregate_type",
            ),
            from_state=strict_str(data["from_state"], "from_state"),
            event=strict_str(data["event"], "event"),
            guard=strict_str(data["guard"], "guard"),
            to_state=strict_str(data["to_state"], "to_state"),
            owner_activity=strict_str(
                data["owner_activity"],
                "owner_activity",
            ),
            aggregate_version=strict_int(
                data["aggregate_version"],
                "aggregate_version",
                minimum=0,
            ),
            durable_side_effects=tuple(side_effects),
            audit_event=strict_str(data["audit_event"], "audit_event"),
            fsync_before_ack=strict_bool(
                data["fsync_before_ack"],
                "fsync_before_ack",
            ),
            timestamp=strict_float(data["timestamp"], "timestamp"),
        )


@dataclass(frozen=True)
class RunTransitionContext:
    """Journal-projected facts used by guarded Run transitions."""

    run_id: str
    plan_epoch: int
    source_sequence: int
    unresolved_effect_ids: tuple[str, ...] = ()
    undisposed_committed_effect_ids: tuple[str, ...] = ()
    criteria_verified: bool = False
    verification_attestation_ids: tuple[str, ...] = ()
    finalization_record_id: str = ""
    human_acceptance_decision_id: str = ""
    actor_principal_id: str = ""
    decision_nonce_consumed: bool = False
    decision_epoch_valid: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "unresolved_effect_ids",
            tuple(self.unresolved_effect_ids),
        )
        object.__setattr__(
            self,
            "undisposed_committed_effect_ids",
            tuple(self.undisposed_committed_effect_ids),
        )
        object.__setattr__(
            self,
            "verification_attestation_ids",
            tuple(self.verification_attestation_ids),
        )


def _check_version(current: int, expected: int | None) -> None:
    if expected is not None and current != expected:
        raise TransitionRejected(
            f"aggregate version mismatch: expected {expected}, current {current}"
        )


def _record(
    *,
    aggregate_id: str,
    aggregate_type: str,
    from_state: Enum,
    event: Enum,
    guard: str,
    to_state: Enum,
    version: int,
    emitted: tuple[str, ...],
    owner_activity: str,
) -> TransitionRecord:
    return TransitionRecord(
        aggregate_id=aggregate_id,
        aggregate_type=aggregate_type,
        from_state=str(from_state.value),
        event=str(event.value),
        guard=guard,
        to_state=str(to_state.value),
        owner_activity=owner_activity,
        aggregate_version=version,
        durable_side_effects=emitted,
        audit_event=f"{aggregate_type}.{event.value}",
        fsync_before_ack=True,
        timestamp=time.time(),
    )


_RUN_TRANSITIONS: dict[tuple[RunState, RunEvent], tuple[RunState, tuple[str, ...], str]] = {
    (RunState.RECEIVED, RunEvent.CLARIFICATION_REQUIRED): (
        RunState.CLARIFYING,
        ("decision.created",),
        "goal requires clarification",
    ),
    (RunState.RECEIVED, RunEvent.GOAL_DEFINED): (
        RunState.PLAN_READY,
        ("run.plan_requested",),
        "goal definition complete",
    ),
    (RunState.CLARIFYING, RunEvent.GOAL_DEFINED): (
        RunState.PLAN_READY,
        ("run.plan_requested",),
        "clarification resolved",
    ),
    (RunState.PLAN_READY, RunEvent.PLAN_VALIDATED): (
        RunState.PLAN_READY,
        ("run.plan_validated",),
        "plan compiler accepted plan",
    ),
    (RunState.PLAN_READY, RunEvent.APPROVAL_REQUIRED): (
        RunState.APPROVAL_PENDING,
        ("approval.created",),
        "plan exceeds automatic authorization",
    ),
    (RunState.PLAN_READY, RunEvent.ACTIVATED): (
        RunState.SCHEDULED,
        ("run.scheduled",),
        "activation authorization consumed",
    ),
    (RunState.APPROVAL_PENDING, RunEvent.ACTIVATED): (
        RunState.SCHEDULED,
        ("run.scheduled",),
        "approval and activation authorization consumed",
    ),
    (RunState.SCHEDULED, RunEvent.STARTED): (
        RunState.EXECUTING,
        ("run.execution_started",),
        "ready step received legal lease",
    ),
    (RunState.QUEUED, RunEvent.STARTED): (
        RunState.EXECUTING,
        ("run.execution_started",),
        "queued run received legal lease",
    ),
    (RunState.EXECUTING, RunEvent.OUTPUT_SUBMITTED): (
        RunState.VERIFYING,
        ("verification.requested",),
        "worker submitted staged output",
    ),
    (RunState.VERIFYING, RunEvent.VERIFICATION_FAILED): (
        RunState.REPLAN_PENDING,
        ("run.replan_requested",),
        "verification rejected current plan output",
    ),
    (RunState.EXECUTING, RunEvent.REPLAN_REQUESTED): (
        RunState.REPLAN_PENDING,
        ("run.replan_requested",),
        "runtime requested replan",
    ),
    (RunState.EXECUTING, RunEvent.SUPERSEDE_REQUESTED): (
        RunState.SUPERSEDED_PENDING_DRAIN,
        ("run.safe_drain_requested",),
        "new goal or plan version superseded active work",
    ),
    (RunState.SCHEDULED, RunEvent.SUPERSEDE_REQUESTED): (
        RunState.SUPERSEDED_PENDING_DRAIN,
        ("run.safe_drain_requested",),
        "new goal or plan version superseded scheduled work",
    ),
    (RunState.SUPERSEDED_PENDING_DRAIN, RunEvent.SUPERSEDE_DRAINED): (
        RunState.REPLAN_PENDING,
        ("run.replan_requested",),
        "old attempts reached a safe point",
    ),
    (RunState.BLOCKED, RunEvent.REPLAN_REQUESTED): (
        RunState.REPLAN_PENDING,
        ("run.replan_requested",),
        "blocking decision selected replan",
    ),
    (RunState.REPLAN_PENDING, RunEvent.STARTED): (
        RunState.EXECUTING,
        ("run.execution_resumed",),
        "new plan epoch activated",
    ),
    (RunState.EXECUTING, RunEvent.BLOCKED): (
        RunState.BLOCKED,
        ("decision.created",),
        "runtime produced a legal blocker",
    ),
    (RunState.VERIFYING, RunEvent.ACCEPTANCE_REQUIRED): (
        RunState.ACCEPTANCE_PENDING,
        ("decision.human_acceptance_created",),
        "criterion requires Human Oracle",
    ),
    (RunState.EXECUTING, RunEvent.PAUSED): (
        RunState.PAUSED,
        ("run.dispatch_gate_closed",),
        "run control gate drained",
    ),
    (RunState.SCHEDULED, RunEvent.PAUSED): (
        RunState.PAUSED,
        ("run.dispatch_gate_closed",),
        "scheduled run paused",
    ),
    (RunState.PAUSED, RunEvent.RESUMED): (
        RunState.SCHEDULED,
        ("run.resume_requested",),
        "run control epoch advanced",
    ),
}


def transition_run(
    run: Run,
    event: RunEvent,
    *,
    expected_aggregate_version: int | None = None,
    context: RunTransitionContext | None = None,
    owner_activity: str = "run_state_machine",
) -> tuple[Run, TransitionRecord, tuple[str, ...]]:
    """Apply one guarded Run transition and return a new immutable Run."""

    _check_version(run.aggregate_version, expected_aggregate_version)
    source = run.state
    guard = ""
    emitted: tuple[str, ...] = ()
    target: RunState
    target_terminal_state = run.target_terminal_state
    if context is not None and (
        context.run_id != run.run_id
        or context.plan_epoch != run.plan_epoch
        or context.source_sequence < 1
    ):
        raise TransitionRejected(
            "run transition context is not bound to current run and plan"
        )
    unresolved_effect_ids = context.unresolved_effect_ids if context else ()
    undisposed_committed_effect_ids = (
        context.undisposed_committed_effect_ids if context else ()
    )

    if event in {RunEvent.VERIFICATION_PASSED, RunEvent.HUMAN_ACCEPTED}:
        expected_source = (
            RunState.VERIFYING
            if event is RunEvent.VERIFICATION_PASSED
            else RunState.ACCEPTANCE_PENDING
        )
        if source is not expected_source:
            raise TransitionRejected(
                f"illegal run transition: {source.value} + {event.value}"
            )
        if context is None:
            raise TransitionRejected(
                "durable run transition context is required before success"
            )
        if unresolved_effect_ids:
            raise TransitionRejected(
                f"unresolved effects prevent success: {unresolved_effect_ids}"
            )
        if undisposed_committed_effect_ids:
            raise TransitionRejected(
                "committed Effect disposition is required before success"
            )
        if context.criteria_verified is not True:
            raise TransitionRejected("all criteria must be verified before success")
        if not context.finalization_record_id:
            raise TransitionRejected(
                "finalization record is required before success"
            )
        if (
            event is RunEvent.VERIFICATION_PASSED
            and not context.verification_attestation_ids
        ):
            raise TransitionRejected(
                "verification attestation is required before success"
            )
        if event is RunEvent.HUMAN_ACCEPTED and (
            not context.human_acceptance_decision_id
            or not context.actor_principal_id
            or context.decision_nonce_consumed is not True
            or context.decision_epoch_valid is not True
        ):
            raise TransitionRejected(
                "authenticated human acceptance decision is required"
            )
        target = RunState.SUCCEEDED
        guard = (
            "criteria verified and effects finalized"
            if event is RunEvent.VERIFICATION_PASSED
            else "human acceptance recorded and effects finalized"
        )
        emitted = ("run.terminal_report_required",)
        target_terminal_state = None
    elif event is RunEvent.HUMAN_REJECTED:
        if source is not RunState.ACCEPTANCE_PENDING:
            raise TransitionRejected(
                f"illegal run transition: {source.value} + {event.value}"
            )
        target = RunState.REPLAN_PENDING
        guard = "human acceptance rejected current result"
        emitted = ("run.revision_requested",)
    elif event in {
        RunEvent.CANCEL_REQUESTED,
        RunEvent.FAILED,
        RunEvent.EXPIRED,
    }:
        if source in {
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.CANCELED,
            RunState.EXPIRED,
        }:
            raise TransitionRejected("terminal Run cannot be reopened in place")
        target = RunState.CANCELLING
        terminal_by_event = {
            RunEvent.CANCEL_REQUESTED: RunState.CANCELED,
            RunEvent.FAILED: RunState.FAILED,
            RunEvent.EXPIRED: RunState.EXPIRED,
        }
        target_terminal_state = terminal_by_event[event]
        guard = "dispatch gate closed and control epoch advanced"
        emitted = ("run.finalization_requested",)
    elif event is RunEvent.CANCEL_DRAINED:
        if source is not RunState.CANCELLING:
            raise TransitionRejected(
                f"illegal run transition: {source.value} + {event.value}"
            )
        if context is None:
            raise TransitionRejected(
                "durable context is required for cancel drain finalization"
            )
        target_terminal_state = target_terminal_state or RunState.CANCELED
        if unresolved_effect_ids or undisposed_committed_effect_ids:
            target = RunState.RECONCILIATION_PENDING
            guard = "effects require reconciliation before terminal state"
            emitted = ("reconciliation.requested",)
        else:
            if not context.finalization_record_id:
                raise TransitionRejected(
                    "finalization record is required before terminal state"
                )
            target = target_terminal_state
            guard = "dispatch drained and effects finalized"
            emitted = ("run.terminal_report_required",)
            target_terminal_state = None
    elif event is RunEvent.RECONCILIATION_COMPLETED:
        if source is not RunState.RECONCILIATION_PENDING:
            raise TransitionRejected(
                f"illegal run transition: {source.value} + {event.value}"
            )
        if context is None:
            raise TransitionRejected(
                "durable context is required for reconciliation completion"
            )
        if unresolved_effect_ids or undisposed_committed_effect_ids:
            raise TransitionRejected("reconciliation is not complete")
        if run.target_terminal_state is None:
            raise TransitionRejected("reconciliation target terminal state is missing")
        if not context.finalization_record_id:
            raise TransitionRejected(
                "finalization record is required before terminal state"
            )
        target = run.target_terminal_state
        target_terminal_state = None
        guard = "all effects reconciled and disposed"
        emitted = ("run.terminal_report_required",)
    else:
        specification = _RUN_TRANSITIONS.get((source, event))
        if specification is None:
            raise TransitionRejected(
                f"illegal run transition: {source.value} + {event.value}"
            )
        target, emitted, guard = specification

    version = run.aggregate_version + 1
    updated = replace(
        run,
        state=target,
        target_terminal_state=target_terminal_state,
        aggregate_version=version,
    )
    record = _record(
        aggregate_id=run.run_id,
        aggregate_type="run",
        from_state=source,
        event=event,
        guard=guard,
        to_state=target,
        version=version,
        emitted=emitted,
        owner_activity=owner_activity,
    )
    return updated, record, emitted


_PLAN_TRANSITIONS = {
    (PlanState.DRAFT, PlanEvent.COMPILED): PlanState.COMPILED,
    (PlanState.COMPILED, PlanEvent.VALIDATED): PlanState.VALIDATED,
    (PlanState.COMPILED, PlanEvent.INVALIDATED): PlanState.INVALID,
    (PlanState.VALIDATED, PlanEvent.ACTIVATED): PlanState.ACTIVE,
    (PlanState.VALIDATED, PlanEvent.INVALIDATED): PlanState.INVALID,
    (PlanState.ACTIVE, PlanEvent.SUPERSEDE_REQUESTED): (
        PlanState.SUPERSEDED_PENDING_DRAIN
    ),
    (PlanState.SUPERSEDED_PENDING_DRAIN, PlanEvent.DRAINED): PlanState.SUPERSEDED,
    (PlanState.ACTIVE, PlanEvent.INVALIDATED): PlanState.INVALID,
}


def transition_plan(
    plan: Plan,
    event: PlanEvent,
    *,
    expected_aggregate_version: int | None = None,
    owner_activity: str = "plan_state_machine",
) -> tuple[Plan, TransitionRecord, tuple[str, ...]]:
    _check_version(plan.aggregate_version, expected_aggregate_version)
    target = _PLAN_TRANSITIONS.get((plan.state, event))
    if target is None:
        raise TransitionRejected(
            f"illegal plan transition: {plan.state.value} + {event.value}"
        )
    version = plan.aggregate_version + 1
    updated = replace(plan, state=target, aggregate_version=version)
    emitted = (f"plan.{target.value}",)
    return (
        updated,
        _record(
            aggregate_id=plan.plan_id,
            aggregate_type="plan",
            from_state=plan.state,
            event=event,
            guard="plan transition table accepted event",
            to_state=target,
            version=version,
            emitted=emitted,
            owner_activity=owner_activity,
        ),
        emitted,
    )


_STEP_TRANSITIONS = {
    (StepState.PENDING, StepEvent.DEPENDENCIES_SATISFIED): StepState.READY,
    (StepState.READY, StepEvent.LEASE_GRANTED): StepState.LEASED,
    (StepState.LEASED, StepEvent.WORKER_STARTED): StepState.RUNNING,
    (StepState.RUNNING, StepEvent.OUTPUT_STAGED): StepState.OUTPUT_STAGED,
    (StepState.OUTPUT_STAGED, StepEvent.VERIFY_STARTED): StepState.VERIFYING,
    (StepState.VERIFYING, StepEvent.VERIFIED): StepState.SUCCEEDED,
    (StepState.RUNNING, StepEvent.ATTEMPT_FAILED): StepState.RETRY_WAIT,
    (StepState.RETRY_WAIT, StepEvent.ATTEMPTS_EXHAUSTED): StepState.FAILED,
    (StepState.RETRY_WAIT, StepEvent.RETRY_READY): StepState.READY,
    (StepState.REJECTED, StepEvent.RETRY_AUTHORIZED): StepState.RETRY_WAIT,
    (StepState.LEASED, StepEvent.WORKER_LOST): StepState.ORPHANED,
    (StepState.RUNNING, StepEvent.WORKER_LOST): StepState.ORPHANED,
    (StepState.ORPHANED, StepEvent.RECONCILE_STARTED): StepState.RECONCILING,
    (StepState.RECONCILING, StepEvent.RECONCILE_RETRY): StepState.READY,
    (StepState.VERIFYING, StepEvent.REJECTED): StepState.REJECTED,
    (StepState.PENDING, StepEvent.SKIP_AUTHORIZED): StepState.SKIPPED,
    (StepState.READY, StepEvent.SKIP_AUTHORIZED): StepState.SKIPPED,
    (StepState.RUNNING, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.LEASED, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.PENDING, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.READY, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.OUTPUT_STAGED, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.VERIFYING, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.RETRY_WAIT, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.ORPHANED, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.RECONCILING, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.REJECTED, StepEvent.CANCEL_REQUESTED): StepState.CANCELLING,
    (StepState.CANCELLING, StepEvent.CANCELED): StepState.CANCELED,
}


def transition_step(
    step: PlanStep,
    event: StepEvent,
    *,
    expected_aggregate_version: int | None = None,
    owner_activity: str = "step_state_machine",
) -> tuple[PlanStep, TransitionRecord, tuple[str, ...]]:
    _check_version(step.aggregate_version, expected_aggregate_version)
    target = _STEP_TRANSITIONS.get((step.state, event))
    if target is None:
        raise TransitionRejected(
            f"illegal step transition: {step.state.value} + {event.value}"
        )
    version = step.aggregate_version + 1
    updated = replace(step, state=target, aggregate_version=version)
    emitted = (f"step.{target.value}",)
    return (
        updated,
        _record(
            aggregate_id=step.step_id,
            aggregate_type="step",
            from_state=step.state,
            event=event,
            guard="step transition table accepted event",
            to_state=target,
            version=version,
            emitted=emitted,
            owner_activity=owner_activity,
        ),
        emitted,
    )


_EFFECT_TRANSITIONS = {
    (EffectState.PROPOSED, EffectEvent.POLICY_ALLOWED): EffectState.POLICY_ALLOWED,
    (EffectState.PROPOSED, EffectEvent.POLICY_DENIED): EffectState.POLICY_DENIED,
    (EffectState.POLICY_ALLOWED, EffectEvent.PREPARED): EffectState.PREPARED,
    (
        EffectState.POLICY_ALLOWED,
        EffectEvent.PREPARE_FAILED,
    ): EffectState.ABORTED_NO_DISPATCH,
    (
        EffectState.PREPARED,
        EffectEvent.ABORT_BEFORE_DISPATCH,
    ): EffectState.ABORTED_NO_DISPATCH,
    (EffectState.PREPARED, EffectEvent.DISPATCH_STARTED): EffectState.EXECUTING,
    (EffectState.EXECUTING, EffectEvent.DISPATCH_COMMITTED): EffectState.COMMITTED,
    (
        EffectState.EXECUTING,
        EffectEvent.DISPATCH_FAILED_SAFE,
    ): EffectState.FAILED_SAFE,
    (
        EffectState.EXECUTING,
        EffectEvent.DISPATCH_UNKNOWN,
    ): EffectState.UNKNOWN_EFFECT,
    (
        EffectState.UNKNOWN_EFFECT,
        EffectEvent.RECONCILE_STARTED,
    ): EffectState.RECONCILING,
    (
        EffectState.RECONCILING,
        EffectEvent.REMOTE_COMMITTED,
    ): EffectState.COMMITTED,
    (
        EffectState.RECONCILING,
        EffectEvent.REMOTE_NOT_EXECUTED,
    ): EffectState.FAILED_SAFE,
    (
        EffectState.RECONCILING,
        EffectEvent.RETRY_AUTHORIZED,
    ): EffectState.RETRY_AUTHORIZED,
    (
        EffectState.RETRY_AUTHORIZED,
        EffectEvent.PREPARED,
    ): EffectState.PREPARED,
    (
        EffectState.RETRY_AUTHORIZED,
        EffectEvent.ABORT_BEFORE_DISPATCH,
    ): EffectState.ABORTED_NO_DISPATCH,
    (
        EffectState.RECONCILING,
        EffectEvent.MANUAL_REQUIRED,
    ): EffectState.MANUAL_RECONCILIATION,
    (
        EffectState.COMMITTED,
        EffectEvent.COMPENSATE_STARTED,
    ): EffectState.COMPENSATING,
    (
        EffectState.MANUAL_RECONCILIATION,
        EffectEvent.COMPENSATE_STARTED,
    ): EffectState.COMPENSATING,
    (
        EffectState.MANUAL_RECONCILIATION,
        EffectEvent.REMOTE_COMMITTED,
    ): EffectState.COMMITTED,
    (
        EffectState.MANUAL_RECONCILIATION,
        EffectEvent.DISPATCH_FAILED_SAFE,
    ): EffectState.FAILED_SAFE,
    (
        EffectState.COMPENSATING,
        EffectEvent.COMPENSATED,
    ): EffectState.COMPENSATED,
    (
        EffectState.COMPENSATING,
        EffectEvent.COMPENSATION_FAILED,
    ): EffectState.COMPENSATION_FAILED,
    (
        EffectState.COMPENSATION_FAILED,
        EffectEvent.MANUAL_REQUIRED,
    ): EffectState.MANUAL_RECONCILIATION,
    (
        EffectState.MANUAL_RECONCILIATION,
        EffectEvent.ABANDONED_ACCEPTED,
    ): EffectState.ABANDONED_ACCEPTED,
}


def transition_effect(
    effect: Effect,
    event: EffectEvent,
    *,
    expected_aggregate_version: int | None = None,
) -> tuple[Effect, TransitionRecord, tuple[str, ...]]:
    _check_version(effect.aggregate_version, expected_aggregate_version)
    target = _EFFECT_TRANSITIONS.get((effect.state, event))
    if target is None:
        if event is EffectEvent.ABORT_BEFORE_DISPATCH:
            raise TransitionRejected(
                "cannot abort Effect after dispatch might have started"
            )
        raise TransitionRejected(
            f"illegal effect transition: {effect.state.value} + {event.value}"
        )
    active_dispatch = effect.active_dispatch
    if event is EffectEvent.DISPATCH_STARTED:
        active_dispatch = True
    elif event in {
        EffectEvent.DISPATCH_COMMITTED,
        EffectEvent.DISPATCH_FAILED_SAFE,
        EffectEvent.DISPATCH_UNKNOWN,
    }:
        active_dispatch = False
    version = effect.aggregate_version + 1
    updated = replace(
        effect,
        state=target,
        active_dispatch=active_dispatch,
        committed_at=(
            time.time()
            if target is EffectState.COMMITTED
            else effect.committed_at
        ),
        aggregate_version=version,
    )
    emitted = (f"effect.{target.value}",)
    return (
        updated,
        _record(
            aggregate_id=effect.effect_instance_id,
            aggregate_type="effect",
            from_state=effect.state,
            event=event,
            guard="effect transition table and dispatch guard accepted event",
            to_state=target,
            version=version,
            emitted=emitted,
            owner_activity="effect_state_machine",
        ),
        emitted,
    )


def assert_replan_criteria_compatible(
    original: tuple[GoalCriterion, ...],
    replanned: tuple[GoalCriterion, ...],
) -> None:
    """Reject criterion deletion or mutation during ordinary replanning."""

    original_hashes = {
        criterion.criterion_id: criterion.criterion_hash
        for criterion in original
    }
    replanned_hashes = {
        criterion.criterion_id: criterion.criterion_hash
        for criterion in replanned
    }
    missing = sorted(set(original_hashes) - set(replanned_hashes))
    if missing:
        raise CriterionMutationError(
            f"missing criteria in replan: {missing}"
        )
    changed = sorted(
        criterion_id
        for criterion_id, criterion_hash in original_hashes.items()
        if replanned_hashes[criterion_id] != criterion_hash
    )
    if changed:
        raise CriterionMutationError(
            f"criterion content changed during replan: {changed}"
        )
