"""Machine-readable acceptance gate manifest and fail-closed evaluator."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class GateStatus(str, Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class EvidenceLevel(str, Enum):
    UNIT_CONTRACT = "unit_contract"
    INTEGRATION = "integration"
    CHAOS_SECURITY = "chaos_security"
    TENANT_E2E = "tenant_e2e"
    SOAK_STATISTICAL = "soak_statistical"


_REQUIRED_FIELDS = {
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
_METADATA_FIELDS = ("owner", "selector", "threshold", "environment")
_PLACEHOLDER_VALUES = frozenset({"tbd", "todo"})


@dataclass(frozen=True)
class AcceptanceGate:
    id: str
    phase: str
    source_lines: tuple[int, ...]
    owner: str
    selector: str
    threshold: str
    evidence_level: EvidenceLevel
    environment: str
    status: GateStatus

    @classmethod
    def from_dict(cls, record: Mapping[str, Any]) -> AcceptanceGate:
        fields = set(record)
        if fields != _REQUIRED_FIELDS:
            missing = sorted(_REQUIRED_FIELDS - fields)
            unexpected = sorted(fields - _REQUIRED_FIELDS)
            raise ValueError(
                f"invalid gate fields; missing={missing}, unexpected={unexpected}"
            )
        source_lines = tuple(record["source_lines"])
        if not source_lines or any(
            not isinstance(line, int) or isinstance(line, bool) or line <= 0
            for line in source_lines
        ):
            raise ValueError(f"invalid source_lines for gate {record['id']}")
        metadata: dict[str, str] = {}
        for field_name in _METADATA_FIELDS:
            raw_value = record[field_name]
            if not isinstance(raw_value, str):
                raise ValueError(f"invalid {field_name} for gate {record['id']}")
            value = raw_value.strip()
            if not value or value.casefold() in _PLACEHOLDER_VALUES:
                raise ValueError(f"invalid {field_name} for gate {record['id']}")
            metadata[field_name] = value
        return cls(
            id=str(record["id"]),
            phase=str(record["phase"]),
            source_lines=source_lines,
            owner=metadata["owner"],
            selector=metadata["selector"],
            threshold=metadata["threshold"],
            evidence_level=EvidenceLevel(record["evidence_level"]),
            environment=metadata["environment"],
            status=GateStatus(record["status"]),
        )


@dataclass(frozen=True)
class ManifestEvaluation:
    total: int
    passed: tuple[str, ...]
    pending: tuple[str, ...]
    failed: tuple[str, ...]
    status: GateStatus


@dataclass(frozen=True)
class AcceptanceManifest:
    gates: tuple[AcceptanceGate, ...]

    @classmethod
    def load(cls, path: str | Path) -> AcceptanceManifest:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("acceptance manifest must be a JSON list")

        gates: list[AcceptanceGate] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("acceptance manifest records must be JSON objects")
            gate = AcceptanceGate.from_dict(item)
            if gate.id in seen:
                raise ValueError(f"duplicate gate id: {gate.id}")
            seen.add(gate.id)
            gates.append(gate)
        return cls(tuple(gates))

    def evaluate(
        self,
        evidence: Mapping[str, Mapping[str, Any]],
    ) -> ManifestEvaluation:
        gate_ids = {gate.id for gate in self.gates}
        unknown_ids = sorted(set(evidence) - gate_ids)
        if unknown_ids:
            raise ValueError(f"unknown evidence gate ids: {unknown_ids}")

        passed: list[str] = []
        pending: list[str] = []
        failed: list[str] = []
        for gate in self.gates:
            artifact = evidence.get(gate.id)
            if artifact is None:
                pending.append(gate.id)
                continue
            if artifact.get("passed") is False:
                failed.append(gate.id)
                continue
            if artifact.get("passed") is not True:
                pending.append(gate.id)
                continue
            if not self._meets_evidence_contract(gate, artifact):
                pending.append(gate.id)
                continue
            passed.append(gate.id)

        if failed:
            status = GateStatus.FAILED
        elif pending:
            status = GateStatus.PENDING
        else:
            status = GateStatus.PASSED
        return ManifestEvaluation(
            total=len(self.gates),
            passed=tuple(passed),
            pending=tuple(pending),
            failed=tuple(failed),
            status=status,
        )

    @staticmethod
    def _meets_evidence_contract(
        gate: AcceptanceGate,
        artifact: Mapping[str, Any],
    ) -> bool:
        try:
            actual_level = EvidenceLevel(artifact["evidence_level"])
        except (KeyError, ValueError):
            return False
        if actual_level is not gate.evidence_level:
            return False
        return artifact.get("environment") == gate.environment
