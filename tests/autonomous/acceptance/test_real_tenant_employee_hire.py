"""Opt-in selector for the Employee `/hire` real-tenant release profile."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest

from src.autonomous.acceptance.employee_release import (
    EmployeeEnvironmentBinding,
    EmployeeEvidenceBundle,
    EmployeeReleaseAttestation,
    EmployeeReleaseManifest,
    EmployeeReleaseStatus,
    evaluate_employee_release,
)

_REQUIRED_ENV = (
    "GHOSTAP_EMPLOYEE_ACCEPTANCE_LIVE",
    "GHOSTAP_EMPLOYEE_RELEASE_ID",
    "GHOSTAP_EMPLOYEE_COMMIT_SHA",
    "GHOSTAP_EMPLOYEE_SERVICE_INSTANCE_ID",
    "GHOSTAP_EMPLOYEE_STAGING_TENANT_HASH",
    "GHOSTAP_EMPLOYEE_PRODUCTION_TENANT_HASH",
    "GHOSTAP_EMPLOYEE_EVIDENCE_BUNDLE",
    "GHOSTAP_EMPLOYEE_RELEASE_ATTESTATION",
    "GHOSTAP_EMPLOYEE_RELEASE_PUBLIC_KEY",
    "GHOSTAP_EMPLOYEE_RELEASE_KEY_ID",
)


def test_real_tenant_employee_hire_release_profile() -> None:
    """Release only when an explicitly selected, anchored real run is complete."""

    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if os.environ.get("GHOSTAP_EMPLOYEE_ACCEPTANCE_LIVE") != "1":
        missing = sorted({*missing, "GHOSTAP_EMPLOYEE_ACCEPTANCE_LIVE=1"})
    if missing:
        pytest.skip("real Employee tenant acceptance is opt-in; missing environment: " + ", ".join(missing))

    manifest = EmployeeReleaseManifest.load(Path(__file__).with_name("employee_release_manifest.json"))
    binding = EmployeeEnvironmentBinding(
        profile_id=manifest.profile_id,
        release_id=os.environ["GHOSTAP_EMPLOYEE_RELEASE_ID"],
        commit_sha=os.environ["GHOSTAP_EMPLOYEE_COMMIT_SHA"],
        service_instance_id=os.environ["GHOSTAP_EMPLOYEE_SERVICE_INSTANCE_ID"],
        staging_tenant_hash=os.environ["GHOSTAP_EMPLOYEE_STAGING_TENANT_HASH"],
        production_tenant_hash=os.environ["GHOSTAP_EMPLOYEE_PRODUCTION_TENANT_HASH"],
    )
    attestation = EmployeeReleaseAttestation.load(
        Path(os.environ["GHOSTAP_EMPLOYEE_RELEASE_ATTESTATION"])
    )
    public_key = base64.b64decode(
        os.environ["GHOSTAP_EMPLOYEE_RELEASE_PUBLIC_KEY"],
        validate=True,
    )
    assert attestation.verify(
        public_key=public_key,
        expected_key_id=os.environ["GHOSTAP_EMPLOYEE_RELEASE_KEY_ID"],
        expected_binding=binding,
        now=time.time(),
    ), "real Employee tenant release attestation is invalid, stale, or rebound"
    evaluation = evaluate_employee_release(
        manifest=manifest,
        bundle=EmployeeEvidenceBundle(Path(os.environ["GHOSTAP_EMPLOYEE_EVIDENCE_BUNDLE"])),
        binding=binding,
        now=time.time(),
        checkpoint=attestation.checkpoint,
    )

    assert evaluation.status is EmployeeReleaseStatus.PASSED, (
        "real Employee tenant release remains unavailable: "
        f"pending={evaluation.pending}, failed={evaluation.failed}, "
        f"violations={evaluation.violations}"
    )
