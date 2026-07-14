import json
from pathlib import Path

import pytest

from src.autonomous.acceptance import AcceptanceManifest, GateStatus
from src.autonomous.ingress.implementation_evidence import (
    PHASE3_IMPLEMENTATION_MANIFEST_PATH,
    ImplementationEvidenceResult,
    Phase3ImplementationManifest,
)

MANIFEST_PATH = Path("tests/autonomous/acceptance/manifest.json")
EXPECTED_IDS = {
    *(f"DS-{index:02d}" for index in range(1, 9)),
    *(f"FM-{index:02d}" for index in range(1, 22)),
    *(f"FI-{index:02d}" for index in range(1, 33)),
    *(f"MD-{index:02d}" for index in range(1, 8)),
    *(f"AR-{index:02d}" for index in range(1, 6)),
    *(f"MG-{index:02d}" for index in range(1, 5)),
}
REQUIRED_FIELDS = {
    "id",
    "phase",
    "source_lines",
    "owner",
    "selector",
    "threshold",
    "evidence_level",
    "environment",
    "status",
}


def test_manifest_freezes_exactly_77_atomic_gate_ids() -> None:
    records = json.loads(MANIFEST_PATH.read_text())
    ids = [record["id"] for record in records]

    assert len(records) == 77
    assert len(ids) == len(set(ids))
    assert set(ids) == EXPECTED_IDS


def test_each_manifest_record_has_the_frozen_contract() -> None:
    records = json.loads(MANIFEST_PATH.read_text())

    for record in records:
        assert set(record) == REQUIRED_FIELDS
        assert record["phase"] in {"phase_0", "phase_1", "phase_2", "phase_3", "phase_4", "release"}
        assert record["source_lines"]
        assert all(isinstance(line, int) and line > 0 for line in record["source_lines"])
        assert record["owner"]
        assert record["selector"]
        assert record["threshold"]
        assert record["evidence_level"] in {
            "unit_contract",
            "integration",
            "chaos_security",
            "tenant_e2e",
            "soak_statistical",
        }
        assert record["environment"]
        assert record["status"] == "pending"


def test_manifest_load_rejects_duplicate_ids(tmp_path: Path) -> None:
    duplicate_manifest = tmp_path / "manifest.json"
    record = {
        "id": "DS-01",
        "phase": "release",
        "source_lines": [1737],
        "owner": "acceptance",
        "selector": "tests/autonomous/acceptance/test_dataset.py",
        "threshold": "sample_size >= 100",
        "evidence_level": "soak_statistical",
        "environment": "frozen_acceptance_dataset",
        "status": "pending",
    }
    duplicate_manifest.write_text(json.dumps([record, record]))

    with pytest.raises(ValueError, match="duplicate gate id: DS-01"):
        AcceptanceManifest.load(duplicate_manifest)


@pytest.mark.parametrize("field", ["owner", "selector", "threshold", "environment"])
@pytest.mark.parametrize("value", [None, "", "TBD", "TODO"])
def test_manifest_rejects_empty_or_placeholder_metadata(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    invalid_manifest = tmp_path / "manifest.json"
    record = {
        "id": "DS-01",
        "phase": "release",
        "source_lines": [1737],
        "owner": "acceptance",
        "selector": "tests/autonomous/acceptance/test_dataset.py",
        "threshold": "sample_size >= 100",
        "evidence_level": "soak_statistical",
        "environment": "frozen_acceptance_dataset",
        "status": "pending",
    }
    record[field] = value
    invalid_manifest.write_text(json.dumps([record]))

    with pytest.raises(ValueError, match=f"invalid {field}"):
        AcceptanceManifest.load(invalid_manifest)


def test_manifest_evaluation_is_fail_closed_for_missing_evidence() -> None:
    manifest = AcceptanceManifest.load(MANIFEST_PATH)

    evaluation = manifest.evaluate(
        {
            "DS-01": {
                "passed": True,
                "evidence_level": "soak_statistical",
                "environment": "frozen_acceptance_dataset",
            }
        }
    )

    assert evaluation.total == 77
    assert evaluation.passed == ("DS-01",)
    assert len(evaluation.pending) == 76
    assert evaluation.failed == ()
    assert evaluation.status is GateStatus.PENDING


def test_manifest_evaluation_rejects_insufficient_evidence() -> None:
    manifest = AcceptanceManifest.load(MANIFEST_PATH)

    evaluation = manifest.evaluate(
        {
            "DS-01": {
                "passed": True,
                "evidence_level": "unit_contract",
                "environment": "local",
            },
            "FM-01": {
                "passed": False,
                "evidence_level": "tenant_e2e",
                "environment": "tenant_staging",
            },
        }
    )

    assert "DS-01" in evaluation.pending
    assert evaluation.failed == ("FM-01",)
    assert evaluation.status is GateStatus.FAILED


def test_manifest_does_not_substitute_a_different_evidence_type() -> None:
    manifest = AcceptanceManifest.load(MANIFEST_PATH)

    evaluation = manifest.evaluate(
        {
            "AR-05": {
                "passed": True,
                "evidence_level": "soak_statistical",
                "environment": "tenant_staging",
            }
        }
    )

    assert "AR-05" in evaluation.pending
    assert "AR-05" not in evaluation.passed


def test_fi29_is_bound_to_exact_phase3_ipc_result() -> None:
    manifest = AcceptanceManifest.load(MANIFEST_PATH)
    fi29 = next(gate for gate in manifest.gates if gate.id == "FI-29")
    phase3 = Phase3ImplementationManifest.load(PHASE3_IMPLEMENTATION_MANIFEST_PATH)
    ipc = phase3.gate("EI-IPC-01")
    result = ImplementationEvidenceResult.create(
        gate_id=ipc.id,
        selector=ipc.selector,
        commit_sha="a" * 40,
        artifact_kind=ipc.artifact_kind,
        artifact_profile_id=ipc.artifact_profile_id,
        artifact_sha256="b" * 64,
        sdk_wheel_sha256=None,
        sdk_capability_artifact_sha256=None,
        collected_nodeids=(ipc.selector,),
        pytest_exit_code=0,
        setup="passed",
        call="passed",
        teardown="passed",
    )
    evidence = {
        "FI-29": {
            "passed": True,
            "evidence_level": fi29.evidence_level.value,
            "environment": fi29.environment,
            "phase3_gate_id": "EI-IPC-01",
            "phase3_result": result.to_dict(),
        }
    }

    arbitrary = manifest.evaluate(
        {
            "FI-29": {
                "passed": True,
                "evidence_level": fi29.evidence_level.value,
                "environment": fi29.environment,
            }
        }
    )
    bridged = manifest.evaluate(
        evidence,
        expected_phase3_commit_sha="a" * 40,
        expected_phase3_artifact_sha256="b" * 64,
        expected_phase3_sdk_capability_artifact_sha256="c" * 64,
    )
    wrong_commit = manifest.evaluate(
        evidence,
        expected_phase3_commit_sha="d" * 40,
        expected_phase3_artifact_sha256="b" * 64,
        expected_phase3_sdk_capability_artifact_sha256="c" * 64,
    )

    assert fi29.selector == ipc.selector
    assert "FI-29" in arbitrary.pending
    assert "FI-29" in bridged.passed
    assert "FI-29" in wrong_commit.pending
