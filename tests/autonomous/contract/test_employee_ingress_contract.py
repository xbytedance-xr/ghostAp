from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from src.autonomous.ingress.implementation_evidence import (
    PHASE3_IMPLEMENTATION_MANIFEST_PATH,
    ImplementationEvidenceResult,
    Phase3EvidenceStatus,
    Phase3ImplementationManifest,
)
from src.autonomous.ingress.models import (
    EmployeeAttemptState,
    EmployeeIngressAck,
    EmployeeIngressMetadata,
    EmployeeIngressPayload,
    IngressAcceptance,
    IngressDisposition,
)
from src.autonomous.ingress.sdk_capability import (
    CAPABILITY_PROFILE_ID,
    LOCKED_LARK_CHANNEL_WHEEL_SHA256,
)
from src.config.settings import Settings


def _payload(**overrides: object) -> EmployeeIngressPayload:
    values: dict[str, object] = {
        "schema_version": 1,
        "envelope_id": "ing_" + "1" * 64,
        "normalized_parts": ({"type": "text", "text": "inspect the change"},),
        "attachment_descriptors": (
            {
                "resource_type": "file",
                "resource_id": "file_1",
                "mime_type": "text/plain",
                "size_bytes": 12,
                "sha256": "2" * 64,
            },
        ),
    }
    values.update(overrides)
    return EmployeeIngressPayload(**values)


def _metadata(**overrides: object) -> EmployeeIngressMetadata:
    payload = _payload()
    values: dict[str, object] = {
        "schema_version": 1,
        "envelope_id": payload.envelope_id,
        # Trusted parent/worker binding. These values never come from event JSON.
        "tenant_key": "tenant_1",
        "agent_id": "agt_alpha",
        "bot_principal_id": "bot_alpha",
        "app_id": "cli_alpha",
        "channel_generation": 3,
        "connection_id": "conn_1",
        # Untrusted event coordinates are normalized into separate safe indexes.
        "event_id": "evt_1",
        "message_id": "om_1",
        "event_type": "im.message.receive_v1",
        "action_identity": "",
        "chat_id": "oc_1",
        "thread_root_message_id": "om_root",
        "sender_principal_id": "ou_requester",
        "received_at": "2026-07-13T00:00:00Z",
        "semantic_digest": payload.payload_sha256,
        "payload_sha256": payload.payload_sha256,
        "payload_size_bytes": payload.canonical_size_bytes,
        "attachment_count": 1,
        "attachment_total_bytes": 12,
    }
    values.update(overrides)
    return EmployeeIngressMetadata(**values)


def _acceptance(**overrides: object) -> IngressAcceptance:
    metadata = _metadata()
    values: dict[str, object] = {
        "schema_version": 1,
        "acceptance_id": "acc_" + "3" * 64,
        "envelope_id": metadata.envelope_id,
        "dedup_key": metadata.dedup_key,
        "semantic_digest": metadata.semantic_digest,
        "journal_sequence": 7,
        "journal_frame_hash": "4" * 64,
        "accepted_at": "2026-07-13T00:00:01Z",
    }
    values.update(overrides)
    return IngressAcceptance(**values)


def _platform_result(
    gate_id: str,
    *,
    commit_sha: str = "a" * 40,
    artifact_sha256: str = "b" * 64,
    capability_artifact_sha256: str = "c" * 64,
) -> ImplementationEvidenceResult:
    manifest = Phase3ImplementationManifest.load(PHASE3_IMPLEMENTATION_MANIFEST_PATH)
    gate = manifest.gate(gate_id)
    return ImplementationEvidenceResult.create(
        gate_id=gate.id,
        selector=gate.selector,
        commit_sha=commit_sha,
        artifact_kind=gate.artifact_kind,
        artifact_profile_id=gate.artifact_profile_id,
        artifact_sha256=artifact_sha256,
        sdk_wheel_sha256=LOCKED_LARK_CHANNEL_WHEEL_SHA256,
        sdk_capability_artifact_sha256=capability_artifact_sha256,
        collected_nodeids=(gate.selector,),
        pytest_exit_code=0,
        setup="passed",
        call="passed",
        teardown="passed",
    )


@pytest.mark.parametrize(
    "factory",
    [_payload, _metadata, _acceptance],
)
def test_ingress_models_are_frozen_exact_schema_round_trips(factory) -> None:
    model = factory()

    assert type(model).from_dict(model.to_dict()) == model
    with pytest.raises(FrozenInstanceError):
        model.schema_version = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="unknown"):
        type(model).from_dict({**model.to_dict(), "unexpected": True})


def test_encrypted_payload_is_deeply_immutable_canonical_and_secret_field_free() -> None:
    first = _payload(
        normalized_parts=(
            {"text": "inspect the change", "type": "text"},
            {"value": {"b": 2, "a": 1}, "type": "post"},
        )
    )
    second = _payload(
        normalized_parts=(
            {"type": "text", "text": "inspect the change"},
            {"type": "post", "value": {"a": 1, "b": 2}},
        )
    )

    assert first.payload_sha256 == second.payload_sha256
    assert first.canonical_bytes == second.canonical_bytes
    with pytest.raises(TypeError):
        first.normalized_parts[0]["text"] = "changed"  # type: ignore[index]
    with pytest.raises(ValueError, match="secret"):
        EmployeeIngressPayload.from_dict(
            {
                **first.to_dict(),
                "normalized_parts": ({"type": "text", "app_secret": "nope"},),
            }
        )
    assert "app_secret" not in first.to_dict()


def test_payload_rejects_oversize_content_and_attachment_metadata() -> None:
    with pytest.raises(ValueError, match="payload size"):
        _payload(normalized_parts=({"type": "text", "text": "x" * (256 * 1024)},))
    with pytest.raises(ValueError, match="attachment"):
        _payload(
            attachment_descriptors=(
                {
                    "resource_type": "file",
                    "resource_id": "file_1",
                    "mime_type": "text/plain",
                    "size_bytes": True,
                    "sha256": "2" * 64,
                },
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_id", "employee_alpha"),
        ("app_id", "app_alpha"),
        ("message_id", "message_1"),
        ("channel_generation", True),
        ("channel_generation", 0),
        ("received_at", "2026-07-13T08:00:00+08:00"),
        ("received_at", "not-a-time"),
        ("semantic_digest", "not-a-digest"),
        ("payload_size_bytes", -1),
    ],
)
def test_metadata_rejects_invalid_identifiers_generation_size_and_time(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match=field):
        _metadata(**{field: value})


def test_canonical_dedup_prefers_event_and_ignores_connection_generation() -> None:
    first = _metadata()
    replay = _metadata(channel_generation=99, connection_id="conn_replay")
    different_employee = _metadata(agent_id="agt_beta")

    assert first.dedup_key == replay.dedup_key
    assert first.dedup_key != different_employee.dedup_key
    assert first.canonical_dedup_material == (
        "tenant_1",
        "agt_alpha",
        "event",
        "evt_1",
    )


def test_fallback_dedup_requires_trusted_action_correlation() -> None:
    with pytest.raises(ValueError, match="action_identity"):
        _metadata(event_id="", action_identity="")

    first = _metadata(event_id="", action_identity="corr_1")
    replay = _metadata(
        event_id="",
        action_identity="corr_1",
        channel_generation=4,
        connection_id="conn_2",
    )
    assert first.dedup_key == replay.dedup_key
    assert first.canonical_dedup_material == (
        "tenant_1",
        "agt_alpha",
        "fallback",
        "om_1",
        "im.message.receive_v1",
        "corr_1",
    )


def test_transport_ack_is_new_per_delivery_and_references_canonical_acceptance() -> None:
    acceptance = _acceptance()
    first = EmployeeIngressAck(
        schema_version=1,
        request_id="req_1",
        acceptance=acceptance,
        agent_id="agt_alpha",
        app_id="cli_alpha",
        channel_generation=3,
        connection_id="conn_1",
        semantic_digest=acceptance.semantic_digest,
        duplicate=False,
        acknowledged_at="2026-07-13T00:00:02Z",
    )
    replay = replace(
        first,
        request_id="req_2",
        channel_generation=4,
        connection_id="conn_2",
        duplicate=True,
    )

    assert first.acceptance == replay.acceptance
    assert first.request_id != replay.request_id
    assert EmployeeIngressAck.from_dict(first.to_dict()) == first
    with pytest.raises(ValueError, match="unknown"):
        EmployeeIngressAck.from_dict({**first.to_dict(), "unexpected": True})
    with pytest.raises(ValueError, match="semantic_digest"):
        replace(first, semantic_digest="5" * 64)


def test_disposition_and_attempt_state_are_strict_frozen_and_generation_bound() -> None:
    acceptance = _acceptance()
    disposition = IngressDisposition(
        schema_version=1,
        disposition_id="dsp_" + "5" * 64,
        acceptance_id=acceptance.acceptance_id,
        state="queued",
        reason_code="authorized",
        journal_sequence=8,
        journal_frame_hash="6" * 64,
        recorded_at="2026-07-13T00:00:03Z",
    )
    attempt = EmployeeAttemptState(
        schema_version=1,
        attempt_id="atm_" + "7" * 64,
        acceptance_id=acceptance.acceptance_id,
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        app_id="cli_alpha",
        channel_generation=3,
        state="dispatch_committed",
        terminal_epoch=0,
        journal_sequence=9,
        journal_frame_hash="8" * 64,
        updated_at="2026-07-13T00:00:04Z",
    )

    assert IngressDisposition.from_dict(disposition.to_dict()) == disposition
    assert EmployeeAttemptState.from_dict(attempt.to_dict()) == attempt
    with pytest.raises(ValueError, match="unknown"):
        IngressDisposition.from_dict({**disposition.to_dict(), "unexpected": True})
    with pytest.raises(ValueError, match="unknown"):
        EmployeeAttemptState.from_dict({**attempt.to_dict(), "unexpected": True})
    with pytest.raises(ValueError, match="terminal_epoch"):
        replace(attempt, state="completed", terminal_epoch=0)
    with pytest.raises(ValueError, match="channel_generation"):
        replace(attempt, channel_generation=True)


def test_ingress_settings_defaults_are_bounded_and_visible_release_stays_closed() -> None:
    settings = Settings(_env_file=None)

    assert settings.autonomous_employee_ingress_ack_timeout_seconds == 1.5
    assert 0 < settings.autonomous_employee_ingress_ack_timeout_seconds < 3.0
    assert settings.autonomous_employee_ingress_max_payload_bytes == 256 * 1024
    assert settings.autonomous_employee_ingress_max_attachment_count == 10
    assert settings.autonomous_employee_ingress_max_attachment_bytes == 20 * 1024 * 1024
    assert settings.autonomous_employee_ingress_max_total_attachment_bytes == 50 * 1024 * 1024
    assert settings.autonomous_employee_queue_per_employee_limit == 8
    assert settings.autonomous_employee_queue_per_team_limit == 32
    assert settings.autonomous_employee_queue_global_limit == 128
    assert settings.autonomous_visible_employee_limit == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("autonomous_employee_ingress_ack_timeout_seconds", 0),
        ("autonomous_employee_ingress_ack_timeout_seconds", 3),
        ("autonomous_employee_ingress_ack_timeout_seconds", float("nan")),
        ("autonomous_employee_ingress_ack_timeout_seconds", True),
        ("autonomous_employee_ingress_max_payload_bytes", 0),
        ("autonomous_employee_ingress_max_payload_bytes", 256 * 1024 + 1),
        ("autonomous_employee_ingress_max_payload_bytes", True),
        ("autonomous_employee_ingress_max_attachment_count", 0),
        ("autonomous_employee_ingress_max_attachment_count", 11),
        ("autonomous_employee_ingress_max_attachment_bytes", 0),
        ("autonomous_employee_ingress_max_attachment_bytes", 20 * 1024 * 1024 + 1),
        ("autonomous_employee_ingress_max_total_attachment_bytes", float("nan")),
        (
            "autonomous_employee_ingress_max_total_attachment_bytes",
            50 * 1024 * 1024 + 1,
        ),
        ("autonomous_employee_queue_per_employee_limit", True),
        ("autonomous_employee_queue_per_team_limit", 0),
        ("autonomous_employee_queue_global_limit", float("nan")),
    ],
)
def test_ingress_numeric_settings_reject_bounds_booleans_and_nan(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})


def test_env_example_documents_ingress_settings() -> None:
    env_example = Path(".env.example").read_text(encoding="utf-8")
    for name in (
        "AUTONOMOUS_EMPLOYEE_INGRESS_ACK_TIMEOUT_SECONDS",
        "AUTONOMOUS_EMPLOYEE_INGRESS_MAX_PAYLOAD_BYTES",
        "AUTONOMOUS_EMPLOYEE_INGRESS_MAX_ATTACHMENT_COUNT",
        "AUTONOMOUS_EMPLOYEE_INGRESS_MAX_ATTACHMENT_BYTES",
        "AUTONOMOUS_EMPLOYEE_INGRESS_MAX_TOTAL_ATTACHMENT_BYTES",
        "AUTONOMOUS_EMPLOYEE_QUEUE_PER_EMPLOYEE_LIMIT",
        "AUTONOMOUS_EMPLOYEE_QUEUE_PER_TEAM_LIMIT",
        "AUTONOMOUS_EMPLOYEE_QUEUE_GLOBAL_LIMIT",
    ):
        assert f"{name}=" in env_example


def test_phase3_manifest_is_local_strict_and_freezes_pending_ipc_selector() -> None:
    manifest = Phase3ImplementationManifest.load(PHASE3_IMPLEMENTATION_MANIFEST_PATH)

    assert manifest.profile_id == "employee-durable-ingress-phase3-v1"
    assert tuple(gate.id for gate in manifest.gates) == (
        "EI-PLATFORM-MESSAGE-01",
        "EI-PLATFORM-CARD-01",
        "EI-IPC-01",
    )
    assert all(gate.evidence_level == "chaos_security" for gate in manifest.gates)
    assert manifest.gate("EI-PLATFORM-MESSAGE-01").selector.endswith(
        "::test_message_wire_response_waits_for_parent_anchor"
    )
    assert manifest.gate("EI-PLATFORM-CARD-01").selector.endswith(
        "::test_card_action_wire_response_waits_for_parent_anchor"
    )
    ipc = manifest.gate("EI-IPC-01")
    assert ipc.selector == (
        "tests/autonomous/chaos/test_employee_ingress_recovery.py::"
        "test_ipc_ack_only_after_anchored_acceptance"
    )
    assert ipc.selector_state == "pending"
    assert ipc.artifact_kind == "employee_ingress_ipc_harness"
    assert ipc.artifact_profile_id != CAPABILITY_PROFILE_ID
    assert not Path(ipc.selector.split("::", 1)[0]).exists()


def test_phase3_manifest_rejects_duplicate_ids(tmp_path: Path) -> None:
    manifest = Phase3ImplementationManifest.load(PHASE3_IMPLEMENTATION_MANIFEST_PATH)
    duplicate = tmp_path / "manifest.json"
    record = manifest.to_dict()["gates"][0]
    duplicate.write_text(
        Phase3ImplementationManifest.canonical_json_bytes(
            {
                "schema_version": 1,
                "profile_id": manifest.profile_id,
                "gates": [record, record],
            }
        ).decode(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        Phase3ImplementationManifest.load(duplicate)


def test_phase3_evidence_binds_exact_selector_commit_artifacts_and_summary() -> None:
    manifest = Phase3ImplementationManifest.load(PHASE3_IMPLEMENTATION_MANIFEST_PATH)
    results = (
        _platform_result("EI-PLATFORM-MESSAGE-01"),
        _platform_result("EI-PLATFORM-CARD-01"),
    )

    evaluation = manifest.evaluate(
        results,
        expected_commit_sha="a" * 40,
        expected_artifact_sha256="b" * 64,
        expected_sdk_capability_artifact_sha256="c" * 64,
    )

    assert evaluation.status is Phase3EvidenceStatus.PENDING
    assert evaluation.passed == (
        "EI-PLATFORM-MESSAGE-01",
        "EI-PLATFORM-CARD-01",
    )
    assert evaluation.pending == ("EI-IPC-01",)

    with pytest.raises(ValueError, match="commit"):
        manifest.evaluate(
            (replace(results[0], commit_sha="d" * 40),),
            expected_commit_sha="a" * 40,
            expected_artifact_sha256="b" * 64,
            expected_sdk_capability_artifact_sha256="c" * 64,
        )
    with pytest.raises(ValueError, match="SDK capability artifact"):
        manifest.evaluate(
            (replace(results[0], sdk_capability_artifact_sha256="d" * 64),),
            expected_commit_sha="a" * 40,
            expected_artifact_sha256="b" * 64,
            expected_sdk_capability_artifact_sha256="c" * 64,
        )


def test_phase3_evidence_rejects_duplicate_and_missing_from_run_selectors() -> None:
    manifest = Phase3ImplementationManifest.load(PHASE3_IMPLEMENTATION_MANIFEST_PATH)
    result = _platform_result("EI-PLATFORM-MESSAGE-01")

    with pytest.raises(ValueError, match="duplicate"):
        manifest.evaluate(
            (result, result),
            expected_commit_sha="a" * 40,
            expected_artifact_sha256="b" * 64,
            expected_sdk_capability_artifact_sha256="c" * 64,
        )

    missing = result.to_dict()
    missing["collected_nodeids"] = []
    with pytest.raises(ValueError, match="collected"):
        manifest.evaluate(
            (missing,),
            expected_commit_sha="a" * 40,
            expected_artifact_sha256="b" * 64,
            expected_sdk_capability_artifact_sha256="c" * 64,
        )


def test_phase3_evidence_summary_must_bind_exact_pytest_nodeid() -> None:
    manifest = Phase3ImplementationManifest.load(PHASE3_IMPLEMENTATION_MANIFEST_PATH)
    result = _platform_result("EI-PLATFORM-MESSAGE-01").to_dict()
    result["result_summary"]["nodeid"] = "tests/wrong.py::test_wrong"
    result["result_summary_sha256"] = hashlib.sha256(
        Phase3ImplementationManifest.canonical_json_bytes(result["result_summary"])
    ).hexdigest()

    with pytest.raises(ValueError, match="summary nodeid"):
        manifest.evaluate(
            (result,),
            expected_commit_sha="a" * 40,
            expected_artifact_sha256="b" * 64,
            expected_sdk_capability_artifact_sha256="c" * 64,
        )


def test_phase3_evidence_rejects_task2_pending_selector_and_sdk_substitution() -> None:
    manifest = Phase3ImplementationManifest.load(PHASE3_IMPLEMENTATION_MANIFEST_PATH)
    ipc = manifest.gate("EI-IPC-01")
    fake_ipc = ImplementationEvidenceResult.create(
        gate_id=ipc.id,
        selector=ipc.selector,
        commit_sha="a" * 40,
        artifact_kind="employee_channel_sdk_capability",
        artifact_profile_id=CAPABILITY_PROFILE_ID,
        artifact_sha256="b" * 64,
        sdk_wheel_sha256=LOCKED_LARK_CHANNEL_WHEEL_SHA256,
        sdk_capability_artifact_sha256="c" * 64,
        collected_nodeids=(ipc.selector,),
        pytest_exit_code=0,
        setup="passed",
        call="passed",
        teardown="passed",
    )

    with pytest.raises(ValueError, match="pending selector"):
        manifest.evaluate(
            (fake_ipc,),
            expected_commit_sha="a" * 40,
            expected_artifact_sha256="b" * 64,
            expected_sdk_capability_artifact_sha256="c" * 64,
        )


def test_ingress_contracts_and_evidence_have_no_secret_fields() -> None:
    models = (
        _payload(),
        _metadata(),
        _acceptance(),
        _platform_result("EI-PLATFORM-MESSAGE-01"),
    )
    for model in models:
        serialized = repr(model.to_dict()).casefold()
        assert "app_secret" not in serialized
        assert "access_token" not in serialized
