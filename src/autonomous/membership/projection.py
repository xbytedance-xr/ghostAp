"""Pure replay projection for durable employee team membership."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace

from ..journal.frame import GENESIS_HASH, JournalEvent, TransactionFrame
from .models import (
    MembershipEffect,
    MembershipEffectState,
    MembershipOperation,
    MembershipState,
)

EFFECT_PREPARED = "employee.membership.effect_prepared"
EFFECT_EXECUTING = "employee.membership.effect_executing"
EFFECT_COMMITTED = "employee.membership.effect_committed"
EFFECT_ACTION_REQUIRED = "employee.membership.effect_action_required"
_MEMBERSHIP_EVENTS = frozenset(
    {
        EFFECT_PREPARED,
        EFFECT_EXECUTING,
        EFFECT_COMMITTED,
        EFFECT_ACTION_REQUIRED,
    }
)


class MembershipProjectionError(RuntimeError):
    """Anchored membership history violates the exact state machine."""


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    tenant_key: str
    chat_id: str
    agent_id: str
    app_id: str
    state: MembershipState
    membership_epoch: int
    current_effect_id: str
    requester_principal_id: str
    error_code: str = ""


@dataclass(slots=True)
class MembershipProjectionState:
    records: dict[tuple[str, str, str], MembershipRecord] = field(default_factory=dict)
    effects: dict[str, MembershipEffect] = field(default_factory=dict)
    effect_keys: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    frame_hashes: dict[int, str] = field(default_factory=dict)
    cursor_sequence: int = 0
    cursor_hash: str = ""

    def clone(self) -> MembershipProjectionState:
        return copy.deepcopy(self)


def is_membership_event(event_type: str) -> bool:
    return event_type in _MEMBERSHIP_EVENTS


def reduce_membership_frame(
    state: MembershipProjectionState,
    frame: TransactionFrame,
) -> None:
    if not isinstance(frame, TransactionFrame) or not frame.committed:
        raise MembershipProjectionError("membership projection requires committed frame")
    known = state.frame_hashes.get(frame.sequence)
    if known is not None:
        if known == frame.frame_hash:
            return
        raise MembershipProjectionError("conflicting membership frame replay")
    if frame.sequence != state.cursor_sequence + 1:
        raise MembershipProjectionError("membership frame sequence is not contiguous")
    if frame.previous_hash != (state.cursor_hash or GENESIS_HASH):
        raise MembershipProjectionError("membership frame hash chain mismatch")

    isolated = state.clone()
    for event in frame.events:
        if event.event_type == EFFECT_PREPARED:
            _prepared(isolated, event)
        elif event.event_type == EFFECT_EXECUTING:
            _executing(isolated, event)
        elif event.event_type == EFFECT_COMMITTED:
            _committed(isolated, event, frame.events)
        elif event.event_type == EFFECT_ACTION_REQUIRED:
            _action_required(isolated, event)

    state.records = isolated.records
    state.effects = isolated.effects
    state.effect_keys = isolated.effect_keys
    state.cursor_sequence = frame.sequence
    state.cursor_hash = frame.frame_hash
    state.frame_hashes[frame.sequence] = frame.frame_hash


def _prepared(state: MembershipProjectionState, event: JournalEvent) -> None:
    if set(event.payload) != {"effect"}:
        raise MembershipProjectionError("prepared membership effect must use exact schema")
    try:
        effect = MembershipEffect.from_dict(event.payload["effect"])
    except (TypeError, ValueError) as exc:
        raise MembershipProjectionError("invalid prepared membership effect") from exc
    if event.aggregate_id != effect.effect_id:
        raise MembershipProjectionError("membership effect aggregate mismatch")
    existing_effect = state.effects.get(effect.effect_id)
    if existing_effect is not None:
        if existing_effect == effect:
            return
        raise MembershipProjectionError("membership effect identity conflict")
    key = (effect.tenant_key, effect.chat_id, effect.agent_id)
    record = state.records.get(key)
    if record is not None:
        current = state.effects.get(record.current_effect_id)
        if current is not None and not current.state.terminal:
            raise MembershipProjectionError("membership already has active effect")
        if effect.membership_epoch <= record.membership_epoch:
            raise MembershipProjectionError("membership epoch must increase")
        if record.app_id != effect.app_id:
            raise MembershipProjectionError("membership app binding changed")
    elif effect.membership_epoch != 1:
        raise MembershipProjectionError("initial membership epoch must be one")
    state.effects[effect.effect_id] = effect
    state.effect_keys[effect.effect_id] = key
    state.records[key] = MembershipRecord(
        tenant_key=effect.tenant_key,
        chat_id=effect.chat_id,
        agent_id=effect.agent_id,
        app_id=effect.app_id,
        state=(
            MembershipState.ADDING
            if effect.operation is MembershipOperation.ADD
            else MembershipState.REMOVING
        ),
        membership_epoch=effect.membership_epoch,
        current_effect_id=effect.effect_id,
        requester_principal_id=effect.requester_principal_id,
    )


def _effect_for_transition(
    state: MembershipProjectionState,
    event: JournalEvent,
    expected: MembershipEffectState,
) -> tuple[MembershipEffect, tuple[str, str, str]]:
    effect_id = event.payload.get("effect_id") if isinstance(event.payload, dict) else None
    if event.aggregate_id != effect_id or not isinstance(effect_id, str):
        raise MembershipProjectionError("membership transition effect mismatch")
    effect = state.effects.get(effect_id)
    key = state.effect_keys.get(effect_id)
    if effect is None or key is None:
        raise MembershipProjectionError("membership effect must be prepared")
    if effect.state is not expected:
        raise MembershipProjectionError(
            f"membership effect requires {expected.value} state"
        )
    return effect, key


def _executing(state: MembershipProjectionState, event: JournalEvent) -> None:
    if set(event.payload) != {"effect_id"}:
        raise MembershipProjectionError("executing membership effect must use exact schema")
    effect, _key = _effect_for_transition(
        state, event, MembershipEffectState.PREPARED
    )
    state.effects[effect.effect_id] = replace(
        effect,
        state=MembershipEffectState.EXECUTING,
    )


def _committed(
    state: MembershipProjectionState,
    event: JournalEvent,
    frame_events: tuple[JournalEvent, ...],
) -> None:
    if set(event.payload) != {"effect_id", "observed_is_member"}:
        raise MembershipProjectionError("committed membership effect must use exact schema")
    effect, key = _effect_for_transition(
        state, event, MembershipEffectState.EXECUTING
    )
    observed = event.payload["observed_is_member"]
    desired = effect.operation is MembershipOperation.ADD
    if type(observed) is not bool or observed is not desired:
        raise MembershipProjectionError("membership observation does not match desired state")
    changes = tuple(
        candidate
        for candidate in frame_events
        if candidate.event_type == "employee.membership_changed"
        and candidate.aggregate_id == effect.agent_id
    )
    if len(changes) != 1:
        raise MembershipProjectionError(
            "membership commit requires one employee.membership_changed event"
        )
    groups = changes[0].payload.get("member_groups")
    if not isinstance(groups, (list, tuple)) or (effect.chat_id in groups) is not desired:
        raise MembershipProjectionError(
            "employee.membership_changed does not match observed membership"
        )
    state.effects[effect.effect_id] = replace(
        effect,
        state=MembershipEffectState.COMMITTED,
    )
    record = state.records[key]
    state.records[key] = replace(
        record,
        state=effect.desired_state,
        error_code="",
    )


def _action_required(state: MembershipProjectionState, event: JournalEvent) -> None:
    if set(event.payload) != {"effect_id", "error_code"}:
        raise MembershipProjectionError(
            "action-required membership effect must use exact schema"
        )
    effect_id = event.payload.get("effect_id") if isinstance(event.payload, dict) else None
    if event.aggregate_id != effect_id or not isinstance(effect_id, str):
        raise MembershipProjectionError("membership transition effect mismatch")
    effect = state.effects.get(effect_id)
    key = state.effect_keys.get(effect_id)
    if (
        effect is None
        or key is None
        or effect.state
        not in {MembershipEffectState.PREPARED, MembershipEffectState.EXECUTING}
    ):
        raise MembershipProjectionError(
            "membership action-required requires prepared or executing state"
        )
    error_code = event.payload["error_code"]
    try:
        updated = replace(
            effect,
            state=MembershipEffectState.ACTION_REQUIRED,
            error_code=error_code,
        )
    except ValueError as exc:
        raise MembershipProjectionError("invalid membership action-required error") from exc
    state.effects[effect.effect_id] = updated
    state.records[key] = replace(
        state.records[key],
        state=MembershipState.DEGRADED,
        error_code=updated.error_code,
    )


__all__ = [
    "EFFECT_ACTION_REQUIRED",
    "EFFECT_COMMITTED",
    "EFFECT_EXECUTING",
    "EFFECT_PREPARED",
    "MembershipProjectionError",
    "MembershipProjectionState",
    "MembershipRecord",
    "is_membership_event",
    "reduce_membership_frame",
]
