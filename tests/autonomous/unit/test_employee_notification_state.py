from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.autonomous.journal.frame import JournalEvent
from src.autonomous.provisioning.notification_state import (
    HireNotificationPhase,
    HireNotificationProjectionError,
    hire_notification_message_uuid,
    rebuild_hire_notification_projection,
)


def _frame(sequence: int, event_type: str, payload: dict[str, str]):
    return SimpleNamespace(
        sequence=sequence,
        events=(
            JournalEvent(
                event_type=event_type,
                aggregate_id="hire-notification:hire_1:ready",
                payload=payload,
            ),
        ),
    )


def _payload(**extra: str) -> dict[str, str]:
    return {
        "intent_id": "hire_1",
        "status": "ready",
        "message_uuid": hire_notification_message_uuid("hire_1", "ready"),
        **extra,
    }


def test_notification_retry_requires_explicit_retry_event_and_keeps_uuid():
    frames = (
        _frame(1, "hire.notification.prepared", _payload()),
        _frame(2, "hire.notification.executing", _payload()),
        _frame(3, "hire.notification.action_required", _payload()),
        _frame(4, "hire.notification.retry_requested", _payload()),
        _frame(5, "hire.notification.executing", _payload()),
        _frame(
            6,
            "hire.notification.committed",
            _payload(receipt_ref="om_ready"),
        ),
    )

    state = rebuild_hire_notification_projection(frames)[
        "hire-notification:hire_1:ready"
    ]

    assert state.phase is HireNotificationPhase.COMMITTED
    assert state.attempts == 2
    assert state.message_uuid == hire_notification_message_uuid("hire_1", "ready")
    assert state.receipt_ref == "om_ready"


def test_notification_direct_retry_is_rejected_for_new_event_shape():
    frames = (
        _frame(1, "hire.notification.prepared", _payload()),
        _frame(2, "hire.notification.executing", _payload()),
        _frame(3, "hire.notification.action_required", _payload()),
        _frame(4, "hire.notification.executing", _payload()),
    )

    with pytest.raises(
        HireNotificationProjectionError,
        match="transition is invalid",
    ):
        rebuild_hire_notification_projection(frames)


def test_notification_uuid_is_stable_and_bounded():
    value = hire_notification_message_uuid("hire_1", "ready")

    assert value == hire_notification_message_uuid("hire_1", "ready")
    assert len(value) == 50
    assert value != hire_notification_message_uuid("hire_1", "active")


def test_legacy_committed_notification_replays_without_resending():
    legacy_payload = {"intent_id": "hire_1", "status": "ready"}
    frames = (
        _frame(1, "hire.notification.prepared", legacy_payload),
        _frame(2, "hire.notification.executing", legacy_payload),
        _frame(3, "hire.notification.committed", legacy_payload),
    )

    state = rebuild_hire_notification_projection(frames)[
        "hire-notification:hire_1:ready"
    ]

    assert state.phase is HireNotificationPhase.COMMITTED
    assert state.message_uuid == hire_notification_message_uuid("hire_1", "ready")
    assert state.receipt_ref == "legacy_acknowledged"


def test_notification_aggregate_must_match_payload_coordinates():
    mismatched = SimpleNamespace(
        events=(
            JournalEvent(
                event_type="hire.notification.prepared",
                aggregate_id="hire-notification:hire_wrong:ready",
                payload=_payload(),
            ),
        )
    )

    with pytest.raises(
        HireNotificationProjectionError,
        match="aggregate coordinates are invalid",
    ):
        rebuild_hire_notification_projection((mismatched,))
