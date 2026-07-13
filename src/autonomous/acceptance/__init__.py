"""Acceptance manifest contracts for autonomous deployment gates."""

from .employee_release import (
    BundleCheckpoint,
    BundleIntegrityError,
    EmployeeEnvironmentBinding,
    EmployeeEvidenceBundle,
    EmployeeEvidenceRecord,
    EmployeeEvidenceStatus,
    EmployeeReleaseAttestation,
    EmployeeReleaseEvaluation,
    EmployeeReleaseGate,
    EmployeeReleaseManifest,
    EmployeeReleaseStatus,
    evaluate_employee_release,
)
from .manifest import (
    AcceptanceGate,
    AcceptanceManifest,
    EvidenceLevel,
    GateStatus,
    ManifestEvaluation,
)

__all__ = [
    "AcceptanceGate",
    "AcceptanceManifest",
    "EvidenceLevel",
    "GateStatus",
    "ManifestEvaluation",
    "BundleCheckpoint",
    "BundleIntegrityError",
    "EmployeeEnvironmentBinding",
    "EmployeeEvidenceBundle",
    "EmployeeEvidenceRecord",
    "EmployeeEvidenceStatus",
    "EmployeeReleaseEvaluation",
    "EmployeeReleaseAttestation",
    "EmployeeReleaseGate",
    "EmployeeReleaseManifest",
    "EmployeeReleaseStatus",
    "evaluate_employee_release",
]
