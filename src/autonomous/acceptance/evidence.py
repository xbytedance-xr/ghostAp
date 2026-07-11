"""Immutable evidence artifacts for acceptance gates."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class EvidenceType(str, Enum):
    TEST_RESULT = "test_result"
    METRIC_SNAPSHOT = "metric_snapshot"
    CHAOS_REPORT = "chaos_report"
    SECURITY_SCAN = "security_scan"
    SOAK_REPORT = "soak_report"
    MANUAL_ATTESTATION = "manual_attestation"


@dataclass(frozen=True)
class EvidenceArtifact:
    evidence_id: str
    gate_id: str
    evidence_type: EvidenceType
    data: dict[str, Any]
    content_hash: str
    created_at: float
    attestor: str = ""
    environment: str = ""

    @classmethod
    def create(
        cls,
        *,
        gate_id: str,
        evidence_type: EvidenceType,
        data: dict[str, Any],
        attestor: str = "",
        environment: str = "",
    ) -> EvidenceArtifact:
        content = json.dumps(data, sort_keys=True, ensure_ascii=False)
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        evidence_id = f"ev_{content_hash[:16]}"

        return cls(
            evidence_id=evidence_id,
            gate_id=gate_id,
            evidence_type=evidence_type,
            data=data,
            content_hash=content_hash,
            created_at=time.time(),
            attestor=attestor,
            environment=environment,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "gate_id": self.gate_id,
            "evidence_type": self.evidence_type.value,
            "data": self.data,
            "content_hash": self.content_hash,
            "created_at": self.created_at,
            "attestor": self.attestor,
            "environment": self.environment,
        }


class EvidenceStore:
    """Append-only store for gate evidence artifacts."""

    def __init__(self) -> None:
        self._artifacts: dict[str, EvidenceArtifact] = {}
        self._by_gate: dict[str, list[str]] = {}

    def record(self, artifact: EvidenceArtifact) -> None:
        self._artifacts[artifact.evidence_id] = artifact
        self._by_gate.setdefault(artifact.gate_id, []).append(artifact.evidence_id)

    def get(self, evidence_id: str) -> EvidenceArtifact | None:
        return self._artifacts.get(evidence_id)

    def list_for_gate(self, gate_id: str) -> list[EvidenceArtifact]:
        ids = self._by_gate.get(gate_id, [])
        return [self._artifacts[eid] for eid in ids if eid in self._artifacts]

    def has_evidence(self, gate_id: str) -> bool:
        return gate_id in self._by_gate and len(self._by_gate[gate_id]) > 0

    def verify_integrity(self, evidence_id: str) -> bool:
        artifact = self._artifacts.get(evidence_id)
        if artifact is None:
            return False
        content = json.dumps(artifact.data, sort_keys=True, ensure_ascii=False)
        computed = hashlib.sha256(content.encode()).hexdigest()
        return computed == artifact.content_hash
