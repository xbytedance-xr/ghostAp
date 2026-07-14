from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from src.autonomous.outbox.models import (
    DeliveryEffectKind,
    DeliveryEffectState,
    EmployeeCardState,
    EmployeeOutboxBinding,
    EmployeeOutboxSnapshot,
    OutboxDeliveryEffect,
    advance_snapshot,
    employee_outbox_effect_id,
    employee_outbox_id,
    employee_outbox_uuid,
)


def _snapshot(**overrides: object) -> EmployeeOutboxSnapshot:
    outbox_id = employee_outbox_id("tenant-a", "agt_alpha", "attempt-a")
    values: dict[str, object] = {
        "schema_version": 1,
        "outbox_id": outbox_id,
        "tenant_key": "tenant-a",
        "agent_id": "agt_alpha",
        "attempt_id": "attempt-a",
        "chat_id": "oc_team",
        "thread_root_message_id": "om_root",
        "version": 1,
        "state": EmployeeCardState.QUEUED,
        "title": "修复登录回调",
        "summary": "任务已进入员工队列",
        "progress_percent": 0,
        "card_json": {"schema": "2.0", "body": {"elements": []}},
        "created_at": "2026-07-14T00:00:00Z",
        "terminal_version": 0,
    }
    values.update(overrides)
    return EmployeeOutboxSnapshot(**values)


def test_outbox_id_and_create_uuid_are_stable_and_coordinate_bound() -> None:
    first = employee_outbox_id("tenant-a", "agt_alpha", "attempt-a")
    replay = employee_outbox_id("tenant-a", "agt_alpha", "attempt-a")
    other = employee_outbox_id("tenant-a", "agt_alpha", "attempt-b")

    assert first == replay
    assert first.startswith("out_")
    assert first != other
    assert employee_outbox_uuid(first) == employee_outbox_uuid(replay)
    assert employee_outbox_uuid(first) != employee_outbox_uuid(other)


def test_snapshot_is_frozen_exact_schema_and_rejects_secret_aliases() -> None:
    snapshot = _snapshot()

    assert EmployeeOutboxSnapshot.from_dict(snapshot.to_dict()) == snapshot
    with pytest.raises(FrozenInstanceError):
        snapshot.version = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="unknown"):
        EmployeeOutboxSnapshot.from_dict({**snapshot.to_dict(), "extra": True})
    with pytest.raises(ValueError, match="secret-bearing"):
        _snapshot(
            card_json={
                "schema": "2.0",
                "body": {"elements": [{"App-Secret": "must-not-persist"}]},
            }
        )
    with pytest.raises(ValueError, match="secret-bearing"):
        _snapshot(card_json={"body": {"token": "must-not-persist"}})


def test_snapshot_versions_and_progress_are_monotonic_until_terminal() -> None:
    queued = _snapshot()
    running = replace(
        queued,
        version=2,
        state=EmployeeCardState.RUNNING,
        progress_percent=40,
        summary="正在运行测试",
    )
    completed = replace(
        running,
        version=3,
        state=EmployeeCardState.COMPLETED,
        progress_percent=100,
        terminal_version=3,
        summary="完成",
    )

    assert advance_snapshot(queued, running) == running
    assert advance_snapshot(running, completed) == completed
    with pytest.raises(ValueError, match="terminal"):
        advance_snapshot(
            completed,
            replace(completed, version=4, summary="late progress", terminal_version=4),
        )
    with pytest.raises(ValueError, match="progress"):
        advance_snapshot(running, replace(running, version=3, progress_percent=39))
    with pytest.raises(ValueError, match="exactly one"):
        advance_snapshot(queued, replace(running, version=3))
    with pytest.raises(ValueError, match="created_at"):
        advance_snapshot(
            queued,
            replace(running, created_at="2026-07-14T00:00:01Z"),
        )


def test_snapshot_rejects_coordinate_drift_and_invalid_terminal_version() -> None:
    queued = _snapshot()

    with pytest.raises(ValueError, match="coordinates"):
        advance_snapshot(queued, replace(queued, version=2, chat_id="oc_other"))
    with pytest.raises(ValueError, match="terminal_version"):
        replace(
            queued,
            state=EmployeeCardState.FAILED,
            version=2,
            terminal_version=0,
        )


def test_binding_and_effect_are_frozen_exact_and_bound_to_snapshot() -> None:
    snapshot = _snapshot()
    binding = EmployeeOutboxBinding(
        schema_version=1,
        outbox_id=snapshot.outbox_id,
        stable_uuid=employee_outbox_uuid(snapshot.outbox_id),
        app_id="cli_employee",
        generation=3,
        connection_id="conn_employee",
        message_id="om_employee_card",
        bound_snapshot_version=1,
    )
    effect = OutboxDeliveryEffect(
        schema_version=1,
        effect_id=employee_outbox_effect_id(
            snapshot.outbox_id,
            DeliveryEffectKind.CREATE,
            snapshot.version,
            1,
        ),
        outbox_id=snapshot.outbox_id,
        kind=DeliveryEffectKind.CREATE,
        state=DeliveryEffectState.PREPARED,
        snapshot_version=1,
        snapshot_sha256=snapshot.payload_sha256,
        attempt=1,
        error_code="",
    )

    assert EmployeeOutboxBinding.from_dict(binding.to_dict()) == binding
    assert OutboxDeliveryEffect.from_dict(effect.to_dict()) == effect
    with pytest.raises(ValueError, match="stable_uuid"):
        replace(binding, stable_uuid="00000000-0000-0000-0000-000000000000")
    with pytest.raises(ValueError, match="effect_id"):
        replace(effect, effect_id="bad")
    with pytest.raises(ValueError, match="effect_id"):
        replace(effect, effect_id="outeff_" + "a" * 64)
