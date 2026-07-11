"""Pure replay projections that materialize domain state from journal events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from ..domain.effects import Effect
from ..domain.enums import (
    EffectState,
    GoalState,
    GoalType,
    PlanState,
    RunState,
    StepState,
)
from ..domain.goals import GoalDefinition, GoalSpec, Run
from ..domain.plans import Plan, PlanStep
from .frame import JournalEvent, TransactionFrame


class ProjectionError(RuntimeError):
    """A projection reducer encountered an unrecoverable inconsistency."""


@dataclass
class InboxRecord:
    """Materialized inbox event from journal replay."""

    event_id: str
    dedup_key: str
    source_type: str
    payload: dict[str, Any]
    received_at: float
    processed: bool = False
    goal_id: str | None = None
    run_id: str | None = None
    tombstone: bool = False


@dataclass
class ProjectionState:
    """Complete materialized state from journal replay."""

    goals: dict[str, GoalDefinition] = field(default_factory=dict)
    runs: dict[str, Run] = field(default_factory=dict)
    plans: dict[str, Plan] = field(default_factory=dict)
    steps: dict[str, PlanStep] = field(default_factory=dict)
    effects: dict[str, Effect] = field(default_factory=dict)
    inbox: dict[str, InboxRecord] = field(default_factory=dict)
    dedup_keys: set[str] = field(default_factory=set)
    occurrence_keys: set[str] = field(default_factory=set)
    cursor_sequence: int = 0
    cursor_hash: str = ""

    def get_goal(self, goal_id: str) -> GoalDefinition | None:
        return self.goals.get(goal_id)

    def get_run(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def get_plan(self, plan_id: str) -> Plan | None:
        return self.plans.get(plan_id)

    def get_step(self, step_id: str) -> PlanStep | None:
        return self.steps.get(step_id)

    def get_effect(self, effect_id: str) -> Effect | None:
        return self.effects.get(effect_id)

    def get_inbox_event(self, event_id: str) -> InboxRecord | None:
        return self.inbox.get(event_id)

    def pending_inbox(self) -> list[InboxRecord]:
        return [
            record
            for record in self.inbox.values()
            if not record.processed and not record.tombstone
        ]


# ---------------------------------------------------------------------------
# Reducer dispatch table - pure functions keyed by event_type
# ---------------------------------------------------------------------------

_REDUCERS: dict[str, Any] = {}


def _reducer(event_type: str):
    """Decorator to register a pure reducer for a specific event_type."""

    def decorator(func):
        _REDUCERS[event_type] = func
        return func

    return decorator


@_reducer("goal.created")
def _reduce_goal_created(state: ProjectionState, event: JournalEvent) -> None:
    payload = dict(event.payload)
    goal_id = payload.get("goal_id", event.aggregate_id)
    payload.setdefault("goal_id", goal_id)
    goal = GoalDefinition.from_dict(payload)
    state.goals[goal_id] = goal


@_reducer("goal.state_changed")
def _reduce_goal_state_changed(state: ProjectionState, event: JournalEvent) -> None:
    goal_id = event.aggregate_id
    existing = state.goals.get(goal_id)
    if existing is None:
        raise ProjectionError(f"goal.state_changed for unknown goal {goal_id}")
    new_state = GoalState(event.payload["state"])
    updated = GoalDefinition(
        goal_id=existing.goal_id,
        tenant_key=existing.tenant_key,
        owner_principal_id=existing.owner_principal_id,
        owner_id=existing.owner_id,
        goal_type=existing.goal_type,
        state=new_state,
        spec=existing.spec,
        epochs=existing.epochs,
        autonomy_mode=existing.autonomy_mode,
        created_at=existing.created_at,
        updated_at=event.timestamp,
        standing_order=existing.standing_order,
        aggregate_version=existing.aggregate_version + 1,
    )
    state.goals[goal_id] = updated


@_reducer("goal.activated")
def _reduce_goal_activated(state: ProjectionState, event: JournalEvent) -> None:
    goal_id = event.aggregate_id
    existing = state.goals.get(goal_id)
    if existing is None:
        raise ProjectionError(f"goal.activated for unknown goal {goal_id}")
    updated = GoalDefinition(
        goal_id=existing.goal_id,
        tenant_key=existing.tenant_key,
        owner_principal_id=existing.owner_principal_id,
        owner_id=existing.owner_id,
        goal_type=existing.goal_type,
        state=GoalState.ACTIVE,
        spec=existing.spec,
        epochs=existing.epochs,
        autonomy_mode=existing.autonomy_mode,
        created_at=existing.created_at,
        updated_at=event.timestamp,
        standing_order=existing.standing_order,
        aggregate_version=existing.aggregate_version + 1,
    )
    state.goals[goal_id] = updated


@_reducer("run.created")
def _reduce_run_created(state: ProjectionState, event: JournalEvent) -> None:
    payload = dict(event.payload)
    run_id = payload.get("run_id", event.aggregate_id)
    payload.setdefault("run_id", run_id)
    run = Run.from_dict(payload)
    state.runs[run_id] = run


@_reducer("run.state_changed")
def _reduce_run_state_changed(state: ProjectionState, event: JournalEvent) -> None:
    run_id = event.aggregate_id
    existing = state.runs.get(run_id)
    if existing is None:
        raise ProjectionError(f"run.state_changed for unknown run {run_id}")
    new_state = RunState(event.payload["state"])
    updated = Run(
        run_id=existing.run_id,
        tenant_key=existing.tenant_key,
        goal_id=existing.goal_id,
        goal_version=existing.goal_version,
        run_definition_version=existing.run_definition_version,
        root_run_lineage=existing.root_run_lineage,
        occurrence_key=existing.occurrence_key,
        trigger_event_id=existing.trigger_event_id,
        state=new_state,
        plan_epoch=existing.plan_epoch,
        budget_ledger_id=existing.budget_ledger_id,
        created_at=existing.created_at,
        deadline=existing.deadline,
        supersedes_run_id=existing.supersedes_run_id,
        retry_of_run_id=existing.retry_of_run_id,
        revision_of_run_id=existing.revision_of_run_id,
        authorization_snapshot_id=existing.authorization_snapshot_id,
        run_control_epoch=existing.run_control_epoch,
        revocation_epoch=existing.revocation_epoch,
        target_terminal_state=existing.target_terminal_state,
        aggregate_version=existing.aggregate_version + 1,
    )
    state.runs[run_id] = updated


@_reducer("plan.created")
def _reduce_plan_created(state: ProjectionState, event: JournalEvent) -> None:
    payload = dict(event.payload)
    plan_id = payload.get("plan_id", event.aggregate_id)
    payload.setdefault("plan_id", plan_id)
    plan = Plan.from_dict(payload)
    state.plans[plan_id] = plan
    for step in plan.steps:
        state.steps[step.step_id] = step


@_reducer("plan.state_changed")
def _reduce_plan_state_changed(state: ProjectionState, event: JournalEvent) -> None:
    plan_id = event.aggregate_id
    existing = state.plans.get(plan_id)
    if existing is None:
        raise ProjectionError(f"plan.state_changed for unknown plan {plan_id}")
    new_state = PlanState(event.payload["state"])
    updated = Plan(
        plan_id=existing.plan_id,
        run_id=existing.run_id,
        tenant_key=existing.tenant_key,
        state=new_state,
        epoch=existing.epoch,
        steps=existing.steps,
        criteria_coverage=existing.criteria_coverage,
        budget_estimate=existing.budget_estimate,
        authorization_id=existing.authorization_id,
        parent_authorization_id=existing.parent_authorization_id,
        created_at=existing.created_at,
        aggregate_version=existing.aggregate_version + 1,
    )
    state.plans[plan_id] = updated


@_reducer("step.state_changed")
def _reduce_step_state_changed(state: ProjectionState, event: JournalEvent) -> None:
    step_id = event.aggregate_id
    existing = state.steps.get(step_id)
    if existing is None:
        raise ProjectionError(f"step.state_changed for unknown step {step_id}")
    new_state = StepState(event.payload["state"])
    updated = PlanStep(
        step_id=existing.step_id,
        name=existing.name,
        description=existing.description,
        state=new_state,
        depends_on=existing.depends_on,
        capability=existing.capability,
        capability_version=existing.capability_version,
        arguments_schema=existing.arguments_schema,
        principal_policy=existing.principal_policy,
        resource_key=existing.resource_key,
        verifier_oracle=existing.verifier_oracle,
        compensation=existing.compensation,
        assigned_employee=existing.assigned_employee,
        max_attempts=existing.max_attempts,
        timeout_seconds=existing.timeout_seconds,
        criterion_ids=existing.criterion_ids,
        aggregate_version=existing.aggregate_version + 1,
    )
    state.steps[step_id] = updated


@_reducer("effect.created")
def _reduce_effect_created(state: ProjectionState, event: JournalEvent) -> None:
    payload = dict(event.payload)
    effect_id = payload.get("effect_id", event.aggregate_id)
    payload.setdefault("effect_id", effect_id)
    effect = Effect.from_dict(payload)
    state.effects[effect_id] = effect


@_reducer("effect.state_changed")
def _reduce_effect_state_changed(state: ProjectionState, event: JournalEvent) -> None:
    effect_id = event.aggregate_id
    existing = state.effects.get(effect_id)
    if existing is None:
        raise ProjectionError(f"effect.state_changed for unknown effect {effect_id}")
    new_state = EffectState(event.payload["state"])
    active_dispatch = new_state is EffectState.EXECUTING
    updated = Effect(
        effect_id=existing.effect_id,
        effect_instance_id=existing.effect_instance_id,
        effect_lineage_id=existing.effect_lineage_id,
        action_intent_id=existing.action_intent_id,
        execution_seq=existing.execution_seq,
        state=new_state,
        capability=existing.capability,
        capability_version=existing.capability_version,
        resource_id=existing.resource_id,
        resource_key=existing.resource_key,
        semantic_action_key=existing.semantic_action_key,
        risk_level=existing.risk_level,
        attempt_id=existing.attempt_id,
        run_id=existing.run_id,
        tenant_key=existing.tenant_key,
        created_at=existing.created_at,
        committed_at=event.payload.get("committed_at", existing.committed_at),
        evidence_hash=existing.evidence_hash,
        cleanup_grant_id=existing.cleanup_grant_id,
        provider_idempotency_key=existing.provider_idempotency_key,
        adapter_hash=existing.adapter_hash,
        schema_hash=existing.schema_hash,
        canonicalization_version=existing.canonicalization_version,
        active_dispatch=active_dispatch,
        parent_effect_instance_id=existing.parent_effect_instance_id,
        aggregate_version=existing.aggregate_version + 1,
    )
    state.effects[effect_id] = updated


@_reducer("inbox.event_received")
def _reduce_inbox_event_received(
    state: ProjectionState, event: JournalEvent
) -> None:
    payload = dict(event.payload)
    event_id = payload["event_id"]
    dedup_key = payload["dedup_key"]
    state.dedup_keys.add(dedup_key)
    state.inbox[event_id] = InboxRecord(
        event_id=event_id,
        dedup_key=dedup_key,
        source_type=payload.get("source_type", ""),
        payload=payload.get("payload", {}),
        received_at=event.timestamp,
    )


@_reducer("inbox.event_processed")
def _reduce_inbox_event_processed(
    state: ProjectionState, event: JournalEvent
) -> None:
    event_id = event.payload["event_id"]
    record = state.inbox.get(event_id)
    if record is not None:
        record.processed = True
        if "goal_id" in event.payload:
            record.goal_id = event.payload["goal_id"]
        if "run_id" in event.payload:
            record.run_id = event.payload["run_id"]


@_reducer("inbox.tombstone")
def _reduce_inbox_tombstone(state: ProjectionState, event: JournalEvent) -> None:
    event_id = event.payload["event_id"]
    record = state.inbox.get(event_id)
    if record is not None:
        record.tombstone = True


@_reducer("occurrence.consumed")
def _reduce_occurrence_consumed(
    state: ProjectionState, event: JournalEvent
) -> None:
    occurrence_key = event.payload["occurrence_key"]
    state.occurrence_keys.add(occurrence_key)


@_reducer("cursor.advanced")
def _reduce_cursor_advanced(state: ProjectionState, event: JournalEvent) -> None:
    state.cursor_sequence = event.payload["sequence"]
    state.cursor_hash = event.payload.get("hash", "")


# Legacy event type compatibility (underscore-separated from JournalEntry)
@_reducer("inbox.event")
def _reduce_legacy_inbox_event(state: ProjectionState, event: JournalEvent) -> None:
    """Handle legacy inbox_event format from JournalEntry migration."""
    payload = dict(event.payload)
    event_id = payload.get("event_id", event.aggregate_id)
    dedup_key = payload.get("dedup_key", "")
    if dedup_key:
        state.dedup_keys.add(dedup_key)
    state.inbox[event_id] = InboxRecord(
        event_id=event_id,
        dedup_key=dedup_key,
        source_type=payload.get("source_type", payload.get("event_type", "")),
        payload=payload.get("payload", {}),
        received_at=payload.get("received_at", event.timestamp),
    )


@_reducer("goal.state.changed")
def _reduce_legacy_goal_state(state: ProjectionState, event: JournalEvent) -> None:
    """Handle legacy goal_state_changed from JournalEntry."""
    _reduce_goal_state_changed(state, event)


@_reducer("run.state.changed")
def _reduce_legacy_run_state(state: ProjectionState, event: JournalEvent) -> None:
    """Handle legacy run_state_changed from JournalEntry."""
    _reduce_run_state_changed(state, event)


@_reducer("occurrence.consumed")
def _reduce_legacy_occurrence(state: ProjectionState, event: JournalEvent) -> None:
    """Handle occurrence_consumed from JournalEntry."""
    occurrence_key = event.payload.get("occurrence_key", "")
    if occurrence_key:
        state.occurrence_keys.add(occurrence_key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_event(state: ProjectionState, event: JournalEvent) -> None:
    """Apply a single event to the projection state using the reducer table.

    Unknown event types are silently skipped (forward-compatible).
    """
    reducer = _REDUCERS.get(event.event_type)
    if reducer is not None:
        reducer(state, event)


def apply_frame(state: ProjectionState, frame: TransactionFrame) -> None:
    """Apply all events in a transaction frame to the projection state."""
    for event in frame.events:
        apply_event(state, event)
    state.cursor_sequence = frame.sequence
    state.cursor_hash = frame.frame_hash


class ProjectionRepository:
    """Materializes domain state by replaying journal frames."""

    def __init__(self) -> None:
        self._state = ProjectionState()

    @property
    def state(self) -> ProjectionState:
        return self._state

    def rebuild(self, frames: Iterator[TransactionFrame]) -> ProjectionState:
        """Replay all frames and return the materialized state."""
        self._state = ProjectionState()
        for frame in frames:
            apply_frame(self._state, frame)
        return self._state

    def apply(self, frame: TransactionFrame) -> None:
        """Incrementally apply a single frame."""
        apply_frame(self._state, frame)

    # Typed query methods
    def goal(self, goal_id: str) -> GoalDefinition | None:
        return self._state.get_goal(goal_id)

    def run(self, run_id: str) -> Run | None:
        return self._state.get_run(run_id)

    def plan(self, plan_id: str) -> Plan | None:
        return self._state.get_plan(plan_id)

    def step(self, step_id: str) -> PlanStep | None:
        return self._state.get_step(step_id)

    def effect(self, effect_id: str) -> Effect | None:
        return self._state.get_effect(effect_id)

    def inbox(self, event_id: str) -> InboxRecord | None:
        return self._state.get_inbox_event(event_id)
