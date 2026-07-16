"""Durable state for in-place employee App manifest reauthorization."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Iterable

from ..journal.frame import JournalEvent, TransactionFrame


class ManifestReauthorizationError(RuntimeError):
    """A persisted reauthorization stream violates its contract."""


class ManifestReauthorizationPhase(str, Enum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    COMMITTED = "committed"
    ACTION_REQUIRED = "action_required"


@dataclass(frozen=True)
class ManifestReauthorizationState:
    operation_id: str
    tenant_key: str
    agent_id: str
    bot_principal_id: str
    app_id: str
    desired_manifest_hash: str
    message_id: str
    employee_name: str
    phase: ManifestReauthorizationPhase
    error_code: str = ""
    evidence_source: str = ""

    @property
    def intent_id(self) -> str:
        """Compatibility identity used by the existing registration notifier."""

        return self.operation_id


_EVENT_PREFIX = "manifest.reauthorization."
_BASE_FIELDS = {
    "tenant_key",
    "agent_id",
    "bot_principal_id",
    "app_id",
    "desired_manifest_hash",
    "message_id",
    "employee_name",
}


def is_manifest_reauthorization_event(event_type: str) -> bool:
    return event_type.startswith(_EVENT_PREFIX)


def _text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestReauthorizationError(f"{key} is required")
    return value


def apply_manifest_reauthorization_event(
    current: ManifestReauthorizationState | None,
    event: JournalEvent,
) -> ManifestReauthorizationState:
    payload = dict(event.payload)
    if event.event_type == "manifest.reauthorization.prepared":
        if current is not None or set(payload) != _BASE_FIELDS:
            raise ManifestReauthorizationError("invalid prepared operation")
        return ManifestReauthorizationState(
            operation_id=event.aggregate_id,
            tenant_key=_text(payload, "tenant_key"),
            agent_id=_text(payload, "agent_id"),
            bot_principal_id=_text(payload, "bot_principal_id"),
            app_id=_text(payload, "app_id"),
            desired_manifest_hash=_text(payload, "desired_manifest_hash"),
            message_id=_text(payload, "message_id"),
            employee_name=_text(payload, "employee_name"),
            phase=ManifestReauthorizationPhase.PREPARED,
        )
    if current is None:
        raise ManifestReauthorizationError("operation must be prepared first")
    if event.event_type == "manifest.reauthorization.executing":
        if set(payload) or current.phase is not ManifestReauthorizationPhase.PREPARED:
            raise ManifestReauthorizationError("invalid executing transition")
        return replace(current, phase=ManifestReauthorizationPhase.EXECUTING)
    if event.event_type == "manifest.reauthorization.committed":
        if (
            set(payload) != {"observed_manifest_hash", "evidence_source"}
            or current.phase is not ManifestReauthorizationPhase.EXECUTING
            or _text(payload, "observed_manifest_hash")
            != current.desired_manifest_hash
        ):
            raise ManifestReauthorizationError("invalid committed transition")
        return replace(
            current,
            phase=ManifestReauthorizationPhase.COMMITTED,
            evidence_source=_text(payload, "evidence_source"),
        )
    if event.event_type == "manifest.reauthorization.action_required":
        if (
            set(payload) != {"error_code"}
            or current.phase
            not in {
                ManifestReauthorizationPhase.PREPARED,
                ManifestReauthorizationPhase.EXECUTING,
            }
        ):
            raise ManifestReauthorizationError("invalid action-required transition")
        return replace(
            current,
            phase=ManifestReauthorizationPhase.ACTION_REQUIRED,
            error_code=_text(payload, "error_code"),
        )
    raise ManifestReauthorizationError("unknown reauthorization event")


def rebuild_manifest_reauthorizations(
    frames: Iterable[TransactionFrame],
) -> dict[str, ManifestReauthorizationState]:
    states: dict[str, ManifestReauthorizationState] = {}
    for frame in frames:
        for event in frame.events:
            if not is_manifest_reauthorization_event(event.event_type):
                continue
            states[event.aggregate_id] = apply_manifest_reauthorization_event(
                states.get(event.aggregate_id),
                event,
            )
    return states


__all__ = [
    "ManifestReauthorizationError",
    "ManifestReauthorizationPhase",
    "ManifestReauthorizationState",
    "apply_manifest_reauthorization_event",
    "is_manifest_reauthorization_event",
    "rebuild_manifest_reauthorizations",
]
