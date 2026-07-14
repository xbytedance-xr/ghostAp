"""Immutable replay state for the production employee retirement saga."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from ..journal.frame import TransactionFrame


class FireProjectionError(RuntimeError):
    """A durable fire event stream violates the retirement contract."""


class FireEffectState(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    COMMITTED = "committed"
    ACTION_REQUIRED = "action_required"


class FirePhase(StrEnum):
    RETIRING = "retiring"
    ACTION_REQUIRED = "action_required"
    ARCHIVED = "archived"


FIRE_EFFECT_ORDER: tuple[str, ...] = (
    "execution_quiesce",
    "slash_cleanup",
    "channel_stop",
    "membership_cleanup",
    "credential_destroy",
    "archive_move",
)


@dataclass(frozen=True, slots=True)
class DurableFireState:
    intent_id: str
    tenant_key: str
    message_id: str
    chat_id: str
    requester_principal_id: str
    agent_id: str
    employee_name: str
    bot_principal_id: str
    app_id: str
    credential_ref: str
    drain: bool
    phase: FirePhase = FirePhase.RETIRING
    effects: tuple[tuple[str, FireEffectState], ...] = ()
    error_code: str = ""
    last_sequence: int = 0

    def effect_state(self, effect_type: str) -> FireEffectState | None:
        return dict(self.effects).get(effect_type)


def rebuild_fire_projection(
    frames: tuple[TransactionFrame, ...],
) -> Mapping[str, DurableFireState]:
    states: dict[str, DurableFireState] = {}
    for frame in frames:
        for event in frame.events:
            current = states.get(event.aggregate_id)
            if event.event_type == "fire.requested":
                if current is not None:
                    raise FireProjectionError("duplicate fire request")
                payload = event.payload
                required = {
                    "intent_id", "tenant_key", "message_id", "chat_id",
                    "requester_principal_id", "agent_id", "employee_name", "bot_principal_id",
                    "app_id", "credential_ref", "drain",
                }
                if set(payload) != required or payload.get("intent_id") != event.aggregate_id:
                    raise FireProjectionError("invalid fire request")
                strings = {key: payload[key] for key in required - {"drain"}}
                if any(not isinstance(value, str) or not value for value in strings.values()):
                    raise FireProjectionError("invalid fire request")
                if type(payload["drain"]) is not bool:
                    raise FireProjectionError("invalid fire request")
                states[event.aggregate_id] = DurableFireState(
                    **payload,
                    last_sequence=frame.sequence,
                )
                continue
            if not event.event_type.startswith("fire."):
                continue
            if current is None:
                raise FireProjectionError("fire event references unknown request")
            if event.event_type.startswith("fire.effect."):
                effect_type = event.payload.get("effect_type")
                if effect_type not in FIRE_EFFECT_ORDER or set(event.payload) - {
                    "effect_type", "error_code"
                }:
                    raise FireProjectionError("invalid fire effect")
                next_state = FireEffectState(event.event_type.removeprefix("fire.effect."))
                previous = current.effect_state(effect_type)
                allowed = {
                    FireEffectState.PREPARED: {None},
                    FireEffectState.EXECUTING: {FireEffectState.PREPARED},
                    FireEffectState.COMMITTED: {FireEffectState.EXECUTING},
                    FireEffectState.ACTION_REQUIRED: {FireEffectState.EXECUTING},
                }
                if previous not in allowed[next_state]:
                    raise FireProjectionError("invalid fire effect transition")
                effects = dict(current.effects)
                effects[effect_type] = next_state
                error = str(event.payload.get("error_code") or "")
                if next_state is FireEffectState.ACTION_REQUIRED and not error:
                    raise FireProjectionError("action required needs an error code")
                current = replace(
                    current,
                    effects=tuple((name, effects[name]) for name in FIRE_EFFECT_ORDER if name in effects),
                    phase=(FirePhase.ACTION_REQUIRED if next_state is FireEffectState.ACTION_REQUIRED else current.phase),
                    error_code=error,
                    last_sequence=frame.sequence,
                )
            elif event.event_type == "fire.completed":
                if set(event.payload) != {"external_app_disposition"} or event.payload.get(
                    "external_app_disposition"
                ) != "manual_deletion_required":
                    raise FireProjectionError("invalid fire completion")
                if any(current.effect_state(name) is not FireEffectState.COMMITTED for name in FIRE_EFFECT_ORDER):
                    raise FireProjectionError("fire completed before all effects")
                current = replace(current, phase=FirePhase.ARCHIVED, last_sequence=frame.sequence)
            else:
                raise FireProjectionError("unknown fire event")
            states[event.aggregate_id] = current
    return MappingProxyType(states)


__all__ = [
    "DurableFireState",
    "FIRE_EFFECT_ORDER",
    "FireEffectState",
    "FirePhase",
    "FireProjectionError",
    "rebuild_fire_projection",
]
