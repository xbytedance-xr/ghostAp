"""Validated Journal projection for employee hire status notifications."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from ..journal.frame import TransactionFrame


class HireNotificationProjectionError(RuntimeError):
    """A notification event stream violates the durable retry contract."""


class HireNotificationPhase(StrEnum):
    PREPARED = "prepared"
    EXECUTING = "executing"
    ACTION_REQUIRED = "action_required"
    RETRY_REQUESTED = "retry_requested"
    COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class DurableHireNotification:
    aggregate_id: str
    intent_id: str
    status: str
    message_uuid: str
    phase: HireNotificationPhase
    attempts: int = 0
    receipt_ref: str = ""
    legacy: bool = False


def hire_notification_message_uuid(intent_id: str, status: str) -> str:
    return hashlib.sha256(
        f"employee-hire-status:{intent_id}:{status}".encode()
    ).hexdigest()[:50]


def rebuild_hire_notification_projection(
    frames: tuple[TransactionFrame, ...],
) -> Mapping[str, DurableHireNotification]:
    states: dict[str, DurableHireNotification] = {}
    for frame in frames:
        for event in frame.events:
            if not event.event_type.startswith("hire.notification."):
                continue
            if not event.aggregate_id.startswith("hire-notification:"):
                raise HireNotificationProjectionError(
                    "notification aggregate id is invalid"
                )
            payload = event.payload
            base_keys = {"intent_id", "status"}
            legacy = set(payload) == base_keys
            expected_keys = base_keys | {"message_uuid"}
            if event.event_type == "hire.notification.committed":
                expected_keys |= {"receipt_ref"}
            if not legacy and set(payload) != expected_keys:
                raise HireNotificationProjectionError(
                    "notification payload is invalid"
                )
            intent_id = payload.get("intent_id")
            status = payload.get("status")
            if (
                not isinstance(intent_id, str)
                or not intent_id
                or status not in {"ready", "active", "action_required"}
            ):
                raise HireNotificationProjectionError(
                    "notification coordinates are invalid"
                )
            if event.aggregate_id != f"hire-notification:{intent_id}:{status}":
                raise HireNotificationProjectionError(
                    "notification aggregate coordinates are invalid"
                )
            message_uuid = payload.get("message_uuid") or (
                hire_notification_message_uuid(intent_id, status)
            )
            if (
                not isinstance(message_uuid, str)
                or message_uuid != hire_notification_message_uuid(intent_id, status)
            ):
                raise HireNotificationProjectionError(
                    "notification message uuid is invalid"
                )
            current = states.get(event.aggregate_id)
            event_name = event.event_type.removeprefix("hire.notification.")
            try:
                next_phase = HireNotificationPhase(event_name)
            except ValueError as exc:
                raise HireNotificationProjectionError(
                    "notification event is unknown"
                ) from exc
            if current is None:
                if next_phase is not HireNotificationPhase.PREPARED:
                    raise HireNotificationProjectionError(
                        "notification must begin prepared"
                    )
                states[event.aggregate_id] = DurableHireNotification(
                    aggregate_id=event.aggregate_id,
                    intent_id=intent_id,
                    status=status,
                    message_uuid=message_uuid,
                    phase=next_phase,
                    legacy=legacy,
                )
                continue
            if (
                current.intent_id != intent_id
                or current.status != status
                or current.message_uuid != message_uuid
            ):
                raise HireNotificationProjectionError(
                    "notification coordinates changed"
                )
            allowed = {
                HireNotificationPhase.EXECUTING: {
                    HireNotificationPhase.PREPARED,
                    HireNotificationPhase.RETRY_REQUESTED,
                },
                HireNotificationPhase.ACTION_REQUIRED: {
                    HireNotificationPhase.EXECUTING,
                },
                HireNotificationPhase.RETRY_REQUESTED: {
                    HireNotificationPhase.EXECUTING,
                    HireNotificationPhase.ACTION_REQUIRED,
                },
                HireNotificationPhase.COMMITTED: {
                    HireNotificationPhase.EXECUTING,
                },
            }
            legacy_direct_retry = (
                current.legacy
                and legacy
                and next_phase is HireNotificationPhase.EXECUTING
                and current.phase is HireNotificationPhase.ACTION_REQUIRED
            )
            if (
                next_phase is HireNotificationPhase.PREPARED
                or (
                    not legacy_direct_retry
                    and current.phase not in allowed.get(next_phase, set())
                )
            ):
                raise HireNotificationProjectionError(
                    "notification transition is invalid"
                )
            receipt_ref = current.receipt_ref
            if next_phase is HireNotificationPhase.COMMITTED:
                receipt_ref = (
                    "legacy_acknowledged"
                    if legacy
                    else payload.get("receipt_ref") or ""
                )
                if not isinstance(receipt_ref, str) or not receipt_ref:
                    raise HireNotificationProjectionError(
                        "notification receipt is invalid"
                    )
            states[event.aggregate_id] = replace(
                current,
                phase=next_phase,
                attempts=(
                    current.attempts + 1
                    if next_phase is HireNotificationPhase.EXECUTING
                    else current.attempts
                ),
                receipt_ref=receipt_ref,
                legacy=current.legacy and legacy,
            )
    return MappingProxyType(states)


__all__ = [
    "DurableHireNotification",
    "HireNotificationPhase",
    "HireNotificationProjectionError",
    "hire_notification_message_uuid",
    "rebuild_hire_notification_projection",
]
