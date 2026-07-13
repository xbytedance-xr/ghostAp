"""Strict identity and evidence contracts for employee Channel capability."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from src.autonomous.ingress.sdk_capability import (
    CAPABILITY_NODEIDS,
    LOCKED_LARK_CHANNEL_VERSION,
    LOCKED_LARK_CHANNEL_WHEEL_SHA256,
    CapabilityDecision,
    CapabilityRunEvidence,
    CapabilityTestOutcome,
    SDKDistributionIdentity,
    collect_sdk_distribution_identity,
    prepare_controlled_sdk_import_cache,
)


def _passing_outcomes() -> tuple[CapabilityTestOutcome, ...]:
    return tuple(
        CapabilityTestOutcome(
            nodeid=nodeid,
            setup="passed",
            call="passed",
            teardown="passed",
        )
        for nodeid in CAPABILITY_NODEIDS
    )


def test_installed_sdk_record_and_runtime_payload_are_verified() -> None:
    identity = collect_sdk_distribution_identity()

    assert identity.distribution_name == "lark-channel-sdk"
    assert identity.version == LOCKED_LARK_CHANNEL_VERSION
    assert identity.lock_wheel_sha256 == LOCKED_LARK_CHANNEL_WHEEL_SHA256
    assert identity.observed_wheel_archive_sha256 is None
    assert identity.record_verified is True
    assert identity.installed_identity_algorithm == "record-sha256-triples-v1"
    assert identity.runtime_identity_algorithm == "package-sha256-triples-v1"
    assert identity.path_basis == "site-packages-relative-posix"
    assert len(identity.project_lock_sha256) == 64
    assert identity.installed_record_sha256 == ("832add283a7ba9800978e6e94b37bab223aa266ddc0d6163ff93d564fc06ee27")
    assert identity.runtime_payload_sha256 == ("845e6c04019aefd54ec56c37a435d45a1f5e1cff8a81b7eb5382049ad3e05c88")


def test_controlled_import_cache_is_empty_and_not_source_adjacent(tmp_path: Path) -> None:
    import importlib.util
    import os
    import py_compile
    import sys

    module_name = "employee_sdk_bytecode_probe"
    source = tmp_path / f"{module_name}.py"
    source.write_text("VALUE = 'evil'\n", encoding="utf-8")
    fixed_time = 1_700_000_000
    os.utime(source, (fixed_time, fixed_time))
    source_adjacent_pyc = Path(py_compile.compile(str(source), doraise=True))
    source.write_text("VALUE = 'safe'\n", encoding="utf-8")
    os.utime(source, (fixed_time, fixed_time))
    assert source_adjacent_pyc.is_file()

    previous_prefix = sys.pycache_prefix
    previous_write = sys.dont_write_bytecode
    sys.path.insert(0, str(tmp_path))
    try:
        controlled = prepare_controlled_sdk_import_cache(tmp_path / "controlled-cache")
        cached = Path(importlib.util.cache_from_source(__file__)).resolve()
        assert cached.is_relative_to(controlled)
        assert not list(controlled.rglob("*.pyc"))
        assert sys.dont_write_bytecode is True
        module = __import__(module_name)
        assert module.VALUE == "safe"
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(tmp_path))
        sys.pycache_prefix = previous_prefix
        sys.dont_write_bytecode = previous_write


def test_controlled_collector_rejects_unprepared_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setattr(sys, "pycache_prefix", None)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    with pytest.raises(ValueError, match="controlled SDK import cache"):
        collect_sdk_distribution_identity(require_controlled_import_cache=True)


def test_same_version_repacked_sdk_identity_is_rejected() -> None:
    value = collect_sdk_distribution_identity().to_dict()
    value["runtime_payload_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="not trusted"):
        SDKDistributionIdentity.from_dict(value)


def test_capability_evidence_is_frozen_canonical_and_non_promotable_when_dirty() -> None:
    evidence = CapabilityRunEvidence.create(
        commit_sha="a" * 40,
        worktree_clean=False,
        sdk=collect_sdk_distribution_identity(),
        requested_nodeids=CAPABILITY_NODEIDS,
        collected_nodeids=CAPABILITY_NODEIDS,
        outcomes=_passing_outcomes(),
        pytest_exit_code=0,
        created_at="2026-07-13T00:00:00Z",
    )

    assert evidence.decision is CapabilityDecision.CAPABLE_PINNED_ADAPTER
    assert evidence.promotable is False
    assert evidence.requires_parent_payload_gate is True
    assert evidence.artifact_sha256 == evidence.compute_artifact_sha256()
    assert dataclasses.is_dataclass(evidence)
    with pytest.raises(dataclasses.FrozenInstanceError):
        evidence.promotable = True  # type: ignore[misc]

    encoded = evidence.to_json_bytes()
    assert (
        encoded
        == json.dumps(
            evidence.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    )
    assert CapabilityRunEvidence.from_json_bytes(encoded) == evidence


@pytest.mark.parametrize(
    ("collected", "outcomes", "exit_code"),
    [
        (CAPABILITY_NODEIDS[:-1], _passing_outcomes(), 0),
        (
            CAPABILITY_NODEIDS,
            (
                CapabilityTestOutcome(
                    nodeid=CAPABILITY_NODEIDS[0],
                    setup="passed",
                    call="failed",
                    teardown="passed",
                ),
                *_passing_outcomes()[1:],
            ),
            1,
        ),
    ],
)
def test_collection_mismatch_or_failure_is_capability_red(
    collected: tuple[str, ...],
    outcomes: tuple[CapabilityTestOutcome, ...],
    exit_code: int,
) -> None:
    evidence = CapabilityRunEvidence.create(
        commit_sha="b" * 40,
        worktree_clean=True,
        sdk=collect_sdk_distribution_identity(),
        requested_nodeids=CAPABILITY_NODEIDS,
        collected_nodeids=collected,
        outcomes=outcomes,
        pytest_exit_code=exit_code,
        created_at="2026-07-13T00:00:00Z",
    )

    assert evidence.decision is CapabilityDecision.CAPABILITY_RED
    assert evidence.promotable is False


def test_evidence_parser_rejects_unknown_duplicate_and_tampered_fields() -> None:
    evidence = CapabilityRunEvidence.create(
        commit_sha="c" * 40,
        worktree_clean=True,
        sdk=collect_sdk_distribution_identity(),
        requested_nodeids=CAPABILITY_NODEIDS,
        collected_nodeids=CAPABILITY_NODEIDS,
        outcomes=_passing_outcomes(),
        pytest_exit_code=0,
        created_at="2026-07-13T00:00:00Z",
    )
    value = evidence.to_dict()
    value["unknown"] = True
    with pytest.raises(ValueError, match="fields"):
        CapabilityRunEvidence.from_json_bytes(json.dumps(value, separators=(",", ":")).encode())

    duplicate = evidence.to_json_bytes()[:-1] + b',"schema_version":1}'
    with pytest.raises(ValueError, match="duplicate"):
        CapabilityRunEvidence.from_json_bytes(duplicate)

    tampered = evidence.to_dict()
    tampered["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact"):
        CapabilityRunEvidence.from_json_bytes(json.dumps(tampered, separators=(",", ":")).encode())

    invalid_time = evidence.to_dict()
    invalid_time["created_at"] = "not-a-timestamp"
    invalid_time["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="timestamp"):
        CapabilityRunEvidence.from_json_bytes(
            json.dumps(invalid_time, separators=(",", ":")).encode()
        )

    invalid_schema = evidence.to_dict()
    invalid_schema["schema_version"] = True
    invalid_schema["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="schema"):
        CapabilityRunEvidence.from_json_bytes(
            json.dumps(invalid_schema, separators=(",", ":")).encode()
        )

    invalid_runtime = evidence.to_dict()
    invalid_runtime["runtime"]["python_version"] = 313
    invalid_runtime["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="runtime identity"):
        CapabilityRunEvidence.from_json_bytes(
            json.dumps(invalid_runtime, separators=(",", ":")).encode()
        )


def test_atomic_evidence_write_never_targets_repository_by_default(
    tmp_path: Path,
) -> None:
    evidence = CapabilityRunEvidence.create(
        commit_sha="d" * 40,
        worktree_clean=True,
        sdk=collect_sdk_distribution_identity(),
        requested_nodeids=CAPABILITY_NODEIDS,
        collected_nodeids=CAPABILITY_NODEIDS,
        outcomes=_passing_outcomes(),
        pytest_exit_code=0,
        created_at="2026-07-13T00:00:00Z",
    )
    target = tmp_path / "evidence.json"

    evidence.write_atomic(target)

    assert target.read_bytes() == evidence.to_json_bytes() + b"\n"
    assert not list(tmp_path.glob("*.tmp"))
