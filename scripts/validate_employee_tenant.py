#!/usr/bin/env python3
"""Evaluate or explicitly ingest redacted Employee real-tenant evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.autonomous.acceptance.employee_release import (  # noqa: E402
    BundleCheckpoint,
    EmployeeEnvironmentBinding,
    EmployeeEvidenceBundle,
    EmployeeEvidenceStatus,
    EmployeeReleaseManifest,
    EmployeeReleaseStatus,
    evaluate_employee_release,
)

_ENV = {
    "release_id": "GHOSTAP_EMPLOYEE_RELEASE_ID",
    "commit_sha": "GHOSTAP_EMPLOYEE_COMMIT_SHA",
    "service_instance_id": "GHOSTAP_EMPLOYEE_SERVICE_INSTANCE_ID",
    "staging_tenant_hash": "GHOSTAP_EMPLOYEE_STAGING_TENANT_HASH",
    "production_tenant_hash": "GHOSTAP_EMPLOYEE_PRODUCTION_TENANT_HASH",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fail-closed Employee tenant evidence evaluator. The default mode is read-only; "
            "--live only ingests an already redacted real-tenant capture."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "src/autonomous/acceptance/employee_release_manifest.json",
    )
    parser.add_argument("--bundle", type=Path, default=ROOT / "employee-tenant-evidence.jsonl")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--live-results", type=Path)
    parser.add_argument("--checkpoint-out", type=Path)
    parser.add_argument("--release-id")
    parser.add_argument("--commit-sha")
    parser.add_argument("--service-instance-id")
    parser.add_argument("--staging-tenant-hash")
    parser.add_argument("--production-tenant-hash")
    return parser


def _emit(*, status: str, live_mode: bool, **values: Any) -> None:
    print(
        json.dumps(
            {"status": status, "live_mode": live_mode, **values},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _binding(args: argparse.Namespace, profile_id: str) -> tuple[EmployeeEnvironmentBinding | None, list[str]]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for field_name, env_name in _ENV.items():
        value = getattr(args, field_name) or os.environ.get(env_name, "")
        if not value:
            missing.append(env_name)
        values[field_name] = value
    if missing:
        return None, missing
    try:
        return EmployeeEnvironmentBinding(profile_id=profile_id, **values), []
    except ValueError as exc:
        raise ValueError(f"invalid environment binding: {exc}") from exc


def _load_checkpoint(path: Path | None) -> BundleCheckpoint | None:
    if path is None:
        return None
    return BundleCheckpoint.load(path)


def _ingest_live_capture(
    *,
    path: Path,
    manifest: EmployeeReleaseManifest,
    bundle: EmployeeEvidenceBundle,
    binding: EmployeeEnvironmentBinding,
) -> BundleCheckpoint:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("live results must be a non-empty JSON list")
    gates = {gate.gate_id: gate for gate in manifest.gates}
    candidates: list[dict[str, Any]] = []
    for item in raw:
        required = {"gate_id", "status", "details", "captured_at", "environment", "tenant_hash", "attestor"}
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("invalid live result fields")
        gate = gates.get(item["gate_id"])
        if gate is None:
            raise ValueError(f"unknown employee release gate: {item['gate_id']}")
        if item["environment"] != gate.environment:
            raise ValueError(f"wrong environment for {gate.gate_id}")
        if item["tenant_hash"] != binding.tenant_hash_for(gate.environment):
            raise ValueError(f"wrong tenant hash for {gate.gate_id}")
        try:
            status = EmployeeEvidenceStatus(item["status"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid evidence status for {gate.gate_id}") from exc
        candidate = {
            "gate_id": gate.gate_id,
            "environment": gate.environment,
            "tenant_hash": item["tenant_hash"],
            "status": status,
            "details": item["details"],
            "binding": binding,
            "captured_at": item["captured_at"],
            "attestor": item["attestor"],
        }
        bundle.validate_candidate(**candidate)
        candidates.append(candidate)
    checkpoint = BundleCheckpoint.empty()
    for candidate in candidates:
        checkpoint = bundle.append(**candidate)
    return checkpoint


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = EmployeeReleaseManifest.load(args.manifest)
        binding, missing = _binding(args, manifest.profile_id)
        if binding is None:
            _emit(
                status=EmployeeReleaseStatus.PENDING.value,
                live_mode=args.live,
                reason="missing non-secret environment binding",
                missing_environment=missing,
                release_available=False,
            )
            return 2

        bundle = EmployeeEvidenceBundle(args.bundle)
        checkpoint = _load_checkpoint(args.checkpoint)
        if args.live:
            if os.environ.get("GHOSTAP_EMPLOYEE_ACCEPTANCE_LIVE") != "1":
                _emit(
                    status=EmployeeReleaseStatus.FAILED.value,
                    live_mode=True,
                    reason="live mode requires GHOSTAP_EMPLOYEE_ACCEPTANCE_LIVE=1",
                    release_available=False,
                )
                return 1
            if args.live_results is None:
                _emit(
                    status=EmployeeReleaseStatus.FAILED.value,
                    live_mode=True,
                    reason="live mode requires --live-results from the real-tenant selector",
                    release_available=False,
                )
                return 1
            checkpoint = _ingest_live_capture(
                path=args.live_results,
                manifest=manifest,
                bundle=bundle,
                binding=binding,
            )
            if args.checkpoint_out is not None:
                args.checkpoint_out.write_text(
                    json.dumps(checkpoint.to_dict(), sort_keys=True) + "\n",
                    encoding="utf-8",
                )

        evaluation = evaluate_employee_release(
            manifest=manifest,
            bundle=bundle,
            binding=binding,
            now=time.time(),
            checkpoint=checkpoint,
        )
        _emit(
            status=evaluation.status.value,
            live_mode=args.live,
            release_available=evaluation.release_available,
            passed=list(evaluation.passed),
            pending=list(evaluation.pending),
            failed=list(evaluation.failed),
            violations=list(evaluation.violations),
            record_count=evaluation.record_count,
            head_hash=evaluation.head_hash,
        )
        if evaluation.status is EmployeeReleaseStatus.PASSED:
            return 0
        if evaluation.status is EmployeeReleaseStatus.PENDING:
            return 2
        return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _emit(
            status=EmployeeReleaseStatus.FAILED.value,
            live_mode=args.live,
            reason=str(exc),
            release_available=False,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
