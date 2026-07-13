from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.autonomous.acceptance.employee_release import (
    BundleCheckpoint,
    EmployeeEnvironmentBinding,
    EmployeeEvidenceBundle,
    EmployeeEvidenceStatus,
    EmployeeReleaseAttestation,
    EmployeeReleaseManifest,
    EmployeeReleaseStatus,
    evaluate_employee_release,
)
from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime
from src.config.settings import Settings

MANIFEST_PATH = Path("tests/autonomous/acceptance/employee_release_manifest.json")
PRODUCTION_MANIFEST_PATH = Path("src/autonomous/acceptance/employee_release_manifest.json")
EXPECTED_GATE_IDS = {
    "EMP-STAGING-PROVISION",
    "EMP-PRODUCTION-PROVISION",
    "EMP-IDENTITY-ISOLATION",
    "EMP-SLASH-CRUD-REBUILD",
    "EMP-RESTART-RECOVERY",
    "EMP-RECONNECT-RECOVERY",
    "EMP-MEDIA-TEXT",
    "EMP-MEDIA-POST",
    "EMP-MEDIA-IMAGE",
    "EMP-MEDIA-FILE",
    "EMP-MEDIA-REPLY-THREAD",
    "EMP-CONTEXT-PAGINATION",
    "EMP-CONTEXT-THREAD-BINDING",
    "EMP-CONTEXT-REVISION",
    "EMP-CONTEXT-TRIMMING",
    "EMP-CONTEXT-ZERO-DISPATCH",
    "EMP-CONTEXT-IDENTITY-ISOLATION",
    "EMP-MEDIA-CARD-ACTION",
    "EMP-SECRET-SCAN-STAGING",
    "EMP-SECRET-SCAN-PRODUCTION",
    "EMP-SOAK-1",
    "EMP-SOAK-10",
    "EMP-SOAK-50",
}


def test_runtime_rejects_config_only_release_authority() -> None:
    assert EmployeeDepartmentRuntime._release_evidence_ready(object()) is False


@pytest.fixture
def manifest() -> EmployeeReleaseManifest:
    return EmployeeReleaseManifest.load(MANIFEST_PATH)


@pytest.fixture
def binding() -> EmployeeEnvironmentBinding:
    return EmployeeEnvironmentBinding(
        profile_id="employee-release-v1",
        release_id="release-2026-07-13-001",
        commit_sha="a" * 40,
        service_instance_id="ghostap-prod-a",
        staging_tenant_hash="b" * 64,
        production_tenant_hash="c" * 64,
    )


def test_runtime_checkpoint_requires_independent_ed25519_attestation(
    binding: EmployeeEnvironmentBinding,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    unsigned = EmployeeReleaseAttestation(
        checkpoint=BundleCheckpoint(17, "d" * 64),
        binding=binding,
        issued_at=1_000_000.0,
        key_id="independent-qa-2026",
        signature="pending",
    )
    signed = replace(
        unsigned,
        signature=base64.b64encode(private_key.sign(unsigned.signing_payload())).decode("ascii"),
    )

    assert signed.verify(
        public_key=public_key,
        expected_key_id="independent-qa-2026",
        expected_binding=binding,
        now=1_000_001.0,
    )
    assert (
        replace(signed, key_id="forged").verify(
            public_key=public_key,
            expected_key_id="independent-qa-2026",
            expected_binding=binding,
            now=1_000_001.0,
        )
        is False
    )


def _passing_details(gate_id: str) -> dict[str, object]:
    gate = next(item for item in EmployeeReleaseManifest.load(MANIFEST_PATH).gates if item.gate_id == gate_id)
    details: dict[str, object] = {
        "assertions": {name: True for name in gate.required_assertions},
    }
    if gate_id.startswith("EMP-SOAK-"):
        bot_count = int(gate_id.rsplit("-", 1)[1])
        details.update(
            {
                "bot_count": bot_count,
                "duration_seconds": {1: 300, 10: 1800, 50: 7200}[bot_count],
                "identity_crossovers": 0,
                "duplicate_logical_tasks": 0,
                "uncaught_exceptions": 0,
                "terminal_overwrites": 0,
                "abnormal_reconnects_per_bot_hour": 0.0,
            }
        )
        if bot_count == 50:
            details["fleet_recovery_seconds"] = 120
    return details


def _append_all_passes(
    bundle: EmployeeEvidenceBundle,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
    *,
    captured_at: float,
) -> BundleCheckpoint:
    checkpoint = BundleCheckpoint.empty()
    for gate in manifest.gates:
        checkpoint = bundle.append(
            gate_id=gate.gate_id,
            environment=gate.environment,
            tenant_hash=binding.tenant_hash_for(gate.environment),
            status=EmployeeEvidenceStatus.PASSED,
            details=_passing_details(gate.gate_id),
            binding=binding,
            captured_at=captured_at,
            attestor="tenant-qa@example.invalid",
        )
    return checkpoint


def test_employee_manifest_is_independent_and_covers_the_real_tenant_matrix(
    manifest: EmployeeReleaseManifest,
) -> None:
    assert manifest.profile_id == "employee-release-v1"
    assert {gate.gate_id for gate in manifest.gates} == EXPECTED_GATE_IDS
    assert {gate.environment for gate in manifest.gates} == {
        "tenant_staging",
        "tenant_production",
    }
    assert len(manifest.gates) == len(EXPECTED_GATE_IDS)


def test_operator_and_runtime_use_the_same_employee_release_profile() -> None:
    assert json.loads(PRODUCTION_MANIFEST_PATH.read_text()) == json.loads(MANIFEST_PATH.read_text())


def test_employee_manifest_requires_specific_real_tenant_observations(
    manifest: EmployeeReleaseManifest,
) -> None:
    assertions = {gate.gate_id: set(gate.required_assertions) for gate in manifest.gates}

    assert {
        "preset_applied",
        "addons_applied",
        "create_only_used",
        "employee_status_round_trip",
    } <= assertions["EMP-STAGING-PROVISION"]
    assert {
        "two_employee_bots_same_chat",
        "distinct_app_ids",
        "distinct_reply_identities",
        "main_bot_send_count_zero",
    } <= assertions["EMP-IDENTITY-ISOLATION"]
    assert {
        "slash_create",
        "slash_list",
        "slash_update",
        "slash_delete",
        "slash_rebuild",
        "desktop_propagation",
        "mobile_propagation",
    } <= assertions["EMP-SLASH-CRUD-REBUILD"]
    assert {"host_restarted", "rpo_zero", "fleet_rto_met"} <= assertions["EMP-RESTART-RECOVERY"]
    assert {"network_disconnected", "channel_reconnected", "reconnect_rto_met"} <= assertions["EMP-RECONNECT-RECOVERY"]
    assert {
        "logs_clean",
        "journal_clean",
        "identity_clean",
        "ipc_clean",
        "archive_clean",
    } <= assertions["EMP-SECRET-SCAN-STAGING"]
    assert {
        "full_thread_pagination",
        "page_token_progress",
        "configured_cap_fail_closed",
    } <= assertions["EMP-CONTEXT-PAGINATION"]
    assert {
        "root_thread_current_binding",
        "cross_chat_rejected",
        "cross_tenant_rejected",
    } <= assertions["EMP-CONTEXT-THREAD-BINDING"]
    assert {
        "revision_edit_observed",
        "revision_delete_observed",
        "snapshot_instability_rejected",
    } <= assertions["EMP-CONTEXT-REVISION"]
    assert {
        "deterministic_trimming_observed",
        "protected_current_retained",
        "budget_overflow_rejected",
    } <= assertions["EMP-CONTEXT-TRIMMING"]
    assert {
        "context_failure_zero_dispatch",
        "acp_call_count_zero",
        "task_commit_count_zero",
    } <= assertions["EMP-CONTEXT-ZERO-DISPATCH"]
    assert {
        "employee_app_context_reads",
        "manager_bot_client_calls_zero",
        "manager_bot_api_calls_zero",
        "main_bot_send_count_zero",
    } <= assertions["EMP-CONTEXT-IDENTITY-ISOLATION"]


def test_missing_evidence_is_pending_and_does_not_change_the_visible_limit(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
) -> None:
    evaluation = evaluate_employee_release(
        manifest=manifest,
        bundle=EmployeeEvidenceBundle(tmp_path / "evidence.jsonl"),
        binding=binding,
        now=1_000_000,
        checkpoint=None,
    )

    assert evaluation.status is EmployeeReleaseStatus.PENDING
    assert evaluation.passed == ()
    assert set(evaluation.pending) == EXPECTED_GATE_IDS
    assert Settings(_env_file=None).autonomous_visible_employee_limit == 0


def test_complete_fresh_evidence_requires_a_matching_external_checkpoint(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
) -> None:
    bundle = EmployeeEvidenceBundle(tmp_path / "evidence.jsonl")
    checkpoint = _append_all_passes(
        bundle,
        manifest,
        binding,
        captured_at=1_000_000,
    )

    unanchored = evaluate_employee_release(
        manifest=manifest,
        bundle=bundle,
        binding=binding,
        now=1_000_001,
        checkpoint=None,
    )
    anchored = evaluate_employee_release(
        manifest=manifest,
        bundle=bundle,
        binding=binding,
        now=1_000_001,
        checkpoint=checkpoint,
    )

    assert unanchored.status is EmployeeReleaseStatus.PENDING
    assert "missing trusted bundle checkpoint" in unanchored.violations
    assert anchored.status is EmployeeReleaseStatus.PASSED
    assert set(anchored.passed) == EXPECTED_GATE_IDS


@pytest.mark.parametrize(
    ("mutation", "expected_status"),
    [
        ("partial", EmployeeReleaseStatus.PENDING),
        ("skipped", EmployeeReleaseStatus.PENDING),
        ("stale", EmployeeReleaseStatus.PENDING),
        ("wrong_environment", EmployeeReleaseStatus.FAILED),
        ("wrong_binding", EmployeeReleaseStatus.FAILED),
        ("insufficient_soak", EmployeeReleaseStatus.PENDING),
    ],
)
def test_partial_stale_or_wrongly_bound_evidence_never_passes(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
    mutation: str,
    expected_status: EmployeeReleaseStatus,
) -> None:
    bundle = EmployeeEvidenceBundle(tmp_path / "evidence.jsonl")
    now = 1_000_000.0
    target = manifest.gates[0]
    status = EmployeeEvidenceStatus.PASSED
    captured_at = now
    environment = target.environment
    tenant_hash = binding.tenant_hash_for(environment)
    record_binding = binding
    details = _passing_details(target.gate_id)
    if mutation == "partial":
        status = EmployeeEvidenceStatus.PARTIAL
    elif mutation == "skipped":
        status = EmployeeEvidenceStatus.SKIPPED
    elif mutation == "stale":
        captured_at = now - target.max_age_seconds - 1
    elif mutation == "wrong_environment":
        environment = "tenant_production" if environment == "tenant_staging" else "tenant_staging"
        tenant_hash = binding.tenant_hash_for(environment)
    elif mutation == "wrong_binding":
        record_binding = replace(binding, release_id="release-other")
    elif mutation == "insufficient_soak":
        target = next(gate for gate in manifest.gates if gate.gate_id == "EMP-SOAK-50")
        environment = target.environment
        tenant_hash = binding.tenant_hash_for(environment)
        details = _passing_details(target.gate_id)
        details["duration_seconds"] = 7199

    checkpoint = bundle.append(
        gate_id=target.gate_id,
        environment=environment,
        tenant_hash=tenant_hash,
        status=status,
        details=details,
        binding=record_binding,
        captured_at=captured_at,
        attestor="tenant-qa@example.invalid",
    )

    evaluation = evaluate_employee_release(
        manifest=manifest,
        bundle=bundle,
        binding=binding,
        now=now,
        checkpoint=checkpoint,
    )

    assert evaluation.status is expected_status
    assert evaluation.status is not EmployeeReleaseStatus.PASSED


@pytest.mark.parametrize("mutation", ["missing", "false"])
def test_thread_context_gate_requires_each_explicit_real_tenant_assertion(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
    mutation: str,
) -> None:
    gate = next(
        item
        for item in manifest.gates
        if item.gate_id == "EMP-CONTEXT-IDENTITY-ISOLATION"
    )
    details = _passing_details(gate.gate_id)
    assertions = details["assertions"]
    assert isinstance(assertions, dict)
    if mutation == "missing":
        assertions.pop("manager_bot_api_calls_zero")
    else:
        assertions["manager_bot_api_calls_zero"] = False
    bundle = EmployeeEvidenceBundle(tmp_path / "evidence.jsonl")
    checkpoint = bundle.append(
        gate_id=gate.gate_id,
        environment=gate.environment,
        tenant_hash=binding.tenant_hash_for(gate.environment),
        status=EmployeeEvidenceStatus.PASSED,
        details=details,
        binding=binding,
        captured_at=1_000_000,
        attestor="tenant-qa@example.invalid",
    )

    evaluation = evaluate_employee_release(
        manifest=manifest,
        bundle=bundle,
        binding=binding,
        now=1_000_001,
        checkpoint=checkpoint,
    )

    assert gate.gate_id not in evaluation.passed
    assert evaluation.status is not EmployeeReleaseStatus.PASSED


def test_hash_chain_tampering_is_failed_not_pending(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
) -> None:
    path = tmp_path / "evidence.jsonl"
    bundle = EmployeeEvidenceBundle(path)
    gate = manifest.gates[0]
    checkpoint = bundle.append(
        gate_id=gate.gate_id,
        environment=gate.environment,
        tenant_hash=binding.tenant_hash_for(gate.environment),
        status=EmployeeEvidenceStatus.PASSED,
        details=_passing_details(gate.gate_id),
        binding=binding,
        captured_at=1_000_000,
        attestor="tenant-qa@example.invalid",
    )
    raw = json.loads(path.read_text())
    raw["details"]["assertions"]["observed_on_real_tenant"] = False
    path.write_text(json.dumps(raw) + "\n")

    evaluation = evaluate_employee_release(
        manifest=manifest,
        bundle=bundle,
        binding=binding,
        now=1_000_001,
        checkpoint=checkpoint,
    )

    assert evaluation.status is EmployeeReleaseStatus.FAILED
    assert any("hash" in violation for violation in evaluation.violations)


def test_malformed_or_truncated_bundle_is_failed_not_pending(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
) -> None:
    path = tmp_path / "evidence.jsonl"
    path.write_text('{"schema_version":1', encoding="utf-8")

    evaluation = evaluate_employee_release(
        manifest=manifest,
        bundle=EmployeeEvidenceBundle(path),
        binding=binding,
        now=1_000_001,
        checkpoint=BundleCheckpoint.empty(),
    )

    assert evaluation.status is EmployeeReleaseStatus.FAILED
    assert any("partial final record" in item for item in evaluation.violations)


@pytest.mark.parametrize(
    "details",
    [
        {"app_secret": "do-not-store"},
        {"nested": {"access_token": "do-not-store"}},
        {"note": "Authorization: Bearer do-not-store"},
        {"credential_ref": "vault://employee/secret"},
    ],
)
def test_evidence_bundle_rejects_secret_bearing_artifacts(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
    details: dict[str, object],
) -> None:
    gate = manifest.gates[0]
    with pytest.raises(ValueError, match="secret-bearing evidence is forbidden"):
        EmployeeEvidenceBundle(tmp_path / "evidence.jsonl").append(
            gate_id=gate.gate_id,
            environment=gate.environment,
            tenant_hash=binding.tenant_hash_for(gate.environment),
            status=EmployeeEvidenceStatus.PASSED,
            details=details,
            binding=binding,
            captured_at=1_000_000,
            attestor="tenant-qa@example.invalid",
        )


def test_cli_default_is_read_only_and_reports_pending(tmp_path: Path) -> None:
    bundle = tmp_path / "absent.jsonl"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_employee_tenant.py",
            "--manifest",
            str(MANIFEST_PATH),
            "--bundle",
            str(bundle),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 2
    assert payload["status"] == "pending"
    assert payload["live_mode"] is False
    assert not bundle.exists()


def test_cli_live_mode_requires_explicit_opt_in_and_does_not_create_bundle(
    tmp_path: Path,
    binding: EmployeeEnvironmentBinding,
) -> None:
    bundle = tmp_path / "evidence.jsonl"
    capture = tmp_path / "capture.json"
    capture.write_text("[]", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "scripts/validate_employee_tenant.py",
            "--manifest",
            str(MANIFEST_PATH),
            "--bundle",
            str(bundle),
            "--live",
            "--live-results",
            str(capture),
            "--release-id",
            binding.release_id,
            "--commit-sha",
            binding.commit_sha,
            "--service-instance-id",
            binding.service_instance_id,
            "--staging-tenant-hash",
            binding.staging_tenant_hash,
            "--production-tenant-hash",
            binding.production_tenant_hash,
        ],
        check=False,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "GHOSTAP_EMPLOYEE_ACCEPTANCE_LIVE"},
    )

    payload = json.loads(result.stdout)
    assert result.returncode == 1
    assert payload["status"] == "failed"
    assert "requires GHOSTAP_EMPLOYEE_ACCEPTANCE_LIVE=1" in payload["reason"]
    assert not bundle.exists()


def test_live_capture_is_validated_as_a_batch_before_any_append(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
) -> None:
    script_path = Path("scripts/validate_employee_tenant.py")
    spec = importlib.util.spec_from_file_location("validate_employee_tenant", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    first_gate, second_gate = manifest.gates[:2]
    capture = tmp_path / "capture.json"
    capture.write_text(
        json.dumps(
            [
                {
                    "gate_id": first_gate.gate_id,
                    "status": "passed",
                    "details": _passing_details(first_gate.gate_id),
                    "captured_at": 1_000_000,
                    "environment": first_gate.environment,
                    "tenant_hash": binding.tenant_hash_for(first_gate.environment),
                    "attestor": "tenant-qa@example.invalid",
                },
                {
                    "gate_id": second_gate.gate_id,
                    "status": "passed",
                    "details": {"app_secret": "must-not-be-appended"},
                    "captured_at": 1_000_000,
                    "environment": second_gate.environment,
                    "tenant_hash": binding.tenant_hash_for(second_gate.environment),
                    "attestor": "tenant-qa@example.invalid",
                },
            ]
        ),
        encoding="utf-8",
    )
    bundle = EmployeeEvidenceBundle(tmp_path / "evidence.jsonl")

    with pytest.raises(ValueError, match="secret-bearing evidence is forbidden"):
        module._ingest_live_capture(
            path=capture,
            manifest=manifest,
            bundle=bundle,
            binding=binding,
        )

    assert bundle.load_verified() == ()


def test_soak_reconnect_threshold_is_strictly_less_than_one(
    tmp_path: Path,
    manifest: EmployeeReleaseManifest,
    binding: EmployeeEnvironmentBinding,
) -> None:
    gate = next(item for item in manifest.gates if item.gate_id == "EMP-SOAK-50")
    bundle = EmployeeEvidenceBundle(tmp_path / "evidence.jsonl")
    details = _passing_details(gate.gate_id)
    details["abnormal_reconnects_per_bot_hour"] = 0.9999995
    checkpoint = bundle.append(
        gate_id=gate.gate_id,
        environment=gate.environment,
        tenant_hash=binding.tenant_hash_for(gate.environment),
        status=EmployeeEvidenceStatus.PASSED,
        details=details,
        binding=binding,
        captured_at=1_000_000,
        attestor="tenant-qa@example.invalid",
    )

    evaluation = evaluate_employee_release(
        manifest=manifest,
        bundle=bundle,
        binding=binding,
        now=1_000_001,
        checkpoint=checkpoint,
    )

    assert gate.gate_id in evaluation.passed
