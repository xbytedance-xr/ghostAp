"""Fail-closed release evidence for real-tenant employee Bot acceptance."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

_SCHEMA_VERSION = 1
_EMPTY_HASH = "0" * 64
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$")
_FORBIDDEN_KEYS = (
    "app_secret",
    "access_token",
    "tenant_access_token",
    "refresh_token",
    "authorization",
    "credential_ref",
    "master_key",
    "vault_key",
)
_FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"(?i)\b(?:app_secret|access_token|tenant_access_token|refresh_token)\b\s*[:=]"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\bvault://"),
)


class EmployeeReleaseStatus(str, Enum):
    """Overall state of the employee-specific release gate."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class EmployeeEvidenceStatus(str, Enum):
    """Operator-observed outcome for one real-tenant gate."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class BundleIntegrityError(ValueError):
    """Raised when an evidence bundle cannot be safely trusted."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleIntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_safe_evidence(value: Any, *, path: str = "details") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise ValueError("evidence keys must be strings")
            key = raw_key.casefold().replace("-", "_")
            if key == "token" or any(forbidden in key for forbidden in _FORBIDDEN_KEYS):
                raise ValueError(f"secret-bearing evidence is forbidden at {path}.{raw_key}")
            _validate_safe_evidence(child, path=f"{path}.{raw_key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_safe_evidence(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and any(pattern.search(value) for pattern in _FORBIDDEN_VALUE_PATTERNS):
        raise ValueError(f"secret-bearing evidence is forbidden at {path}")
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"unsupported evidence value at {path}")


@dataclass(frozen=True)
class EmployeeEnvironmentBinding:
    """Non-secret identity of one release evaluation environment."""

    profile_id: str
    release_id: str
    commit_sha: str
    service_instance_id: str
    staging_tenant_hash: str
    production_tenant_hash: str

    def __post_init__(self) -> None:
        for field_name in ("profile_id", "release_id", "service_instance_id"):
            value = getattr(self, field_name)
            if not _ID_RE.fullmatch(value):
                raise ValueError(f"invalid {field_name}")
        if not _COMMIT_RE.fullmatch(self.commit_sha):
            raise ValueError("commit_sha must be a lowercase hexadecimal commit digest")
        for field_name in ("staging_tenant_hash", "production_tenant_hash"):
            if not _HASH_RE.fullmatch(getattr(self, field_name)):
                raise ValueError(f"{field_name} must be a lowercase sha256 digest")
        if self.staging_tenant_hash == self.production_tenant_hash:
            raise ValueError("staging and production tenant hashes must be distinct")

    def tenant_hash_for(self, environment: str) -> str:
        """Resolve the expected anonymized tenant binding."""

        if environment == "tenant_staging":
            return self.staging_tenant_hash
        if environment == "tenant_production":
            return self.production_tenant_hash
        raise ValueError(f"unsupported employee evidence environment: {environment}")

    def to_dict(self) -> dict[str, str]:
        return {
            "profile_id": self.profile_id,
            "release_id": self.release_id,
            "commit_sha": self.commit_sha,
            "service_instance_id": self.service_instance_id,
            "staging_tenant_hash": self.staging_tenant_hash,
            "production_tenant_hash": self.production_tenant_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EmployeeEnvironmentBinding:
        required = {
            "profile_id",
            "release_id",
            "commit_sha",
            "service_instance_id",
            "staging_tenant_hash",
            "production_tenant_hash",
        }
        if set(value) != required or not all(isinstance(value[key], str) for key in required):
            raise BundleIntegrityError("invalid environment binding fields")
        return cls(**{key: value[key] for key in required})


@dataclass(frozen=True)
class BundleCheckpoint:
    """Externally retained head required to trust an append-only bundle."""

    record_count: int
    head_hash: str

    def __post_init__(self) -> None:
        if self.record_count < 0 or isinstance(self.record_count, bool):
            raise ValueError("record_count must be non-negative")
        if not _HASH_RE.fullmatch(self.head_hash):
            raise ValueError("head_hash must be a lowercase sha256 digest")

    @classmethod
    def empty(cls) -> BundleCheckpoint:
        return cls(record_count=0, head_hash=_EMPTY_HASH)

    def to_dict(self) -> dict[str, Any]:
        return {"record_count": self.record_count, "head_hash": self.head_hash}

    @classmethod
    def load(cls, path: str | Path) -> BundleCheckpoint:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
        if not isinstance(raw, dict) or set(raw) != {"record_count", "head_hash"}:
            raise BundleIntegrityError("invalid checkpoint fields")
        return cls(record_count=raw["record_count"], head_hash=raw["head_hash"])


@dataclass(frozen=True)
class EmployeeReleaseAttestation:
    """Independent-QA signature over one bound evidence checkpoint."""

    checkpoint: BundleCheckpoint
    binding: EmployeeEnvironmentBinding
    issued_at: float
    key_id: str
    signature: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "checkpoint": self.checkpoint.to_dict(),
            "binding": self.binding.to_dict(),
            "issued_at": self.issued_at,
            "key_id": self.key_id,
        }

    def signing_payload(self) -> bytes:
        return _canonical_json(self.unsigned_dict())

    def verify(
        self,
        *,
        public_key: bytes,
        expected_key_id: str,
        expected_binding: EmployeeEnvironmentBinding,
        now: float,
        max_age_seconds: int = 86400,
    ) -> bool:
        if (
            self.key_id != expected_key_id
            or self.binding != expected_binding
            or isinstance(now, bool)
            or not isinstance(now, (int, float))
            or self.issued_at > now + 300
            or now - self.issued_at > max_age_seconds
        ):
            return False
        try:
            signature = base64.b64decode(self.signature, validate=True)
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature,
                self.signing_payload(),
            )
        except (binascii.Error, ValueError, InvalidSignature):
            return False
        return True

    @classmethod
    def load(cls, path: str | Path) -> EmployeeReleaseAttestation:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
        required = {
            "schema_version",
            "checkpoint",
            "binding",
            "issued_at",
            "key_id",
            "signature",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) != required
            or raw["schema_version"] != _SCHEMA_VERSION
            or not isinstance(raw["checkpoint"], dict)
            or set(raw["checkpoint"]) != {"record_count", "head_hash"}
            or not isinstance(raw["binding"], dict)
            or isinstance(raw["issued_at"], bool)
            or not isinstance(raw["issued_at"], (int, float))
            or not isinstance(raw["key_id"], str)
            or not raw["key_id"]
            or not isinstance(raw["signature"], str)
            or not raw["signature"]
        ):
            raise BundleIntegrityError("invalid release attestation fields")
        return cls(
            checkpoint=BundleCheckpoint(
                record_count=raw["checkpoint"]["record_count"],
                head_hash=raw["checkpoint"]["head_hash"],
            ),
            binding=EmployeeEnvironmentBinding.from_dict(raw["binding"]),
            issued_at=float(raw["issued_at"]),
            key_id=raw["key_id"],
            signature=raw["signature"],
        )


@dataclass(frozen=True)
class EmployeeReleaseGate:
    """One fixed requirement in the employee release profile."""

    gate_id: str
    category: str
    environment: str
    max_age_seconds: int
    required_assertions: tuple[str, ...]
    minimum_bot_count: int = 0
    minimum_duration_seconds: int = 0
    required_zero_metrics: tuple[str, ...] = ()
    maximum_metrics: tuple[tuple[str, float, bool], ...] = ()


@dataclass(frozen=True)
class EmployeeReleaseManifest:
    """Employee-specific profile, intentionally separate from the frozen 77 gates."""

    profile_id: str
    gates: tuple[EmployeeReleaseGate, ...]

    @classmethod
    def load(cls, path: str | Path) -> EmployeeReleaseManifest:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_object,
        )
        if not isinstance(raw, dict) or set(raw) != {"schema_version", "profile_id", "gates"}:
            raise ValueError("invalid employee release manifest")
        if raw["schema_version"] != _SCHEMA_VERSION or not _ID_RE.fullmatch(raw["profile_id"]):
            raise ValueError("unsupported employee release profile")
        if not isinstance(raw["gates"], list) or not raw["gates"]:
            raise ValueError("employee release manifest requires gates")
        gates: list[EmployeeReleaseGate] = []
        seen: set[str] = set()
        required = {
            "gate_id",
            "category",
            "environment",
            "max_age_seconds",
            "required_assertions",
        }
        optional = {
            "minimum_bot_count",
            "minimum_duration_seconds",
            "required_zero_metrics",
            "maximum_metrics",
        }
        for item in raw["gates"]:
            if not isinstance(item, dict) or not required <= set(item) <= required | optional:
                raise ValueError("invalid employee release gate fields")
            gate_id = item["gate_id"]
            if not isinstance(gate_id, str) or not _ID_RE.fullmatch(gate_id) or gate_id in seen:
                raise ValueError(f"invalid or duplicate employee release gate: {gate_id}")
            seen.add(gate_id)
            environment = item["environment"]
            if environment not in {"tenant_staging", "tenant_production"}:
                raise ValueError(f"invalid environment for {gate_id}")
            assertions = item["required_assertions"]
            zero_metrics = item.get("required_zero_metrics", [])
            maximum_metrics = item.get("maximum_metrics", {})
            parsed_maximums: list[tuple[str, float, bool]] = []
            invalid_maximum = False
            if isinstance(maximum_metrics, dict):
                for key, raw_maximum in maximum_metrics.items():
                    exclusive = False
                    maximum = raw_maximum
                    if isinstance(raw_maximum, dict):
                        if set(raw_maximum) != {"value", "exclusive"} or raw_maximum.get("exclusive") is not True:
                            invalid_maximum = True
                            break
                        maximum = raw_maximum["value"]
                        exclusive = True
                    if (
                        not isinstance(key, str)
                        or not key
                        or isinstance(maximum, bool)
                        or not isinstance(maximum, (int, float))
                    ):
                        invalid_maximum = True
                        break
                    parsed_maximums.append((key, float(maximum), exclusive))
            else:
                invalid_maximum = True
            if (
                not isinstance(assertions, list)
                or not assertions
                or any(not isinstance(value, str) or not value for value in assertions)
                or not isinstance(zero_metrics, list)
                or any(not isinstance(value, str) or not value for value in zero_metrics)
                or invalid_maximum
            ):
                raise ValueError(f"invalid evidence criteria for {gate_id}")
            max_age = item["max_age_seconds"]
            if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age <= 0:
                raise ValueError(f"invalid evidence freshness for {gate_id}")
            category = item["category"]
            if not isinstance(category, str) or not _ID_RE.fullmatch(category):
                raise ValueError(f"invalid category for {gate_id}")
            minimum_bot_count = item.get("minimum_bot_count", 0)
            minimum_duration = item.get("minimum_duration_seconds", 0)
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (minimum_bot_count, minimum_duration)
            ):
                raise ValueError(f"invalid minimum criteria for {gate_id}")
            gates.append(
                EmployeeReleaseGate(
                    gate_id=gate_id,
                    category=category,
                    environment=environment,
                    max_age_seconds=max_age,
                    required_assertions=tuple(assertions),
                    minimum_bot_count=minimum_bot_count,
                    minimum_duration_seconds=minimum_duration,
                    required_zero_metrics=tuple(zero_metrics),
                    maximum_metrics=tuple(sorted(parsed_maximums)),
                )
            )
        return cls(profile_id=raw["profile_id"], gates=tuple(gates))


@dataclass(frozen=True)
class EmployeeEvidenceRecord:
    """One immutable member of the employee evidence hash chain."""

    sequence: int
    gate_id: str
    environment: str
    tenant_hash: str
    status: EmployeeEvidenceStatus
    details: dict[str, Any]
    binding: EmployeeEnvironmentBinding
    captured_at: float
    attestor: str
    previous_hash: str
    record_hash: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "sequence": self.sequence,
            "gate_id": self.gate_id,
            "environment": self.environment,
            "tenant_hash": self.tenant_hash,
            "status": self.status.value,
            "details": self.details,
            "binding": self.binding.to_dict(),
            "captured_at": self.captured_at,
            "attestor": self.attestor,
            "previous_hash": self.previous_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "record_hash": self.record_hash}


def _record_from_dict(raw: Mapping[str, Any]) -> EmployeeEvidenceRecord:
    required = {
        "schema_version",
        "sequence",
        "gate_id",
        "environment",
        "tenant_hash",
        "status",
        "details",
        "binding",
        "captured_at",
        "attestor",
        "previous_hash",
        "record_hash",
    }
    if set(raw) != required or raw["schema_version"] != _SCHEMA_VERSION:
        raise BundleIntegrityError("invalid evidence record fields")
    if isinstance(raw["sequence"], bool) or not isinstance(raw["sequence"], int):
        raise BundleIntegrityError("invalid evidence sequence")
    if not isinstance(raw["details"], dict) or not isinstance(raw["binding"], dict):
        raise BundleIntegrityError("invalid evidence payload")
    for field_name in ("gate_id", "environment", "tenant_hash", "attestor", "previous_hash", "record_hash"):
        if not isinstance(raw[field_name], str):
            raise BundleIntegrityError(f"invalid evidence {field_name}")
    if isinstance(raw["captured_at"], bool) or not isinstance(raw["captured_at"], (int, float)):
        raise BundleIntegrityError("invalid evidence captured_at")
    try:
        status = EmployeeEvidenceStatus(raw["status"])
    except (TypeError, ValueError) as exc:
        raise BundleIntegrityError("invalid evidence status") from exc
    try:
        binding = EmployeeEnvironmentBinding.from_dict(raw["binding"])
        _validate_safe_evidence(raw["details"])
    except ValueError as exc:
        raise BundleIntegrityError(str(exc)) from exc
    return EmployeeEvidenceRecord(
        sequence=raw["sequence"],
        gate_id=raw["gate_id"],
        environment=raw["environment"],
        tenant_hash=raw["tenant_hash"],
        status=status,
        details=raw["details"],
        binding=binding,
        captured_at=float(raw["captured_at"]),
        attestor=raw["attestor"],
        previous_hash=raw["previous_hash"],
        record_hash=raw["record_hash"],
    )


class EmployeeEvidenceBundle:
    """Filesystem JSONL bundle that only exposes verified append operations."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _parse(payload: bytes) -> tuple[EmployeeEvidenceRecord, ...]:
        records: list[EmployeeEvidenceRecord] = []
        expected_previous = _EMPTY_HASH
        if not payload:
            return ()
        if not payload.endswith(b"\n"):
            raise BundleIntegrityError("evidence bundle has a partial final record")
        for expected_sequence, line in enumerate(payload.splitlines(), start=1):
            try:
                raw = json.loads(line, object_pairs_hook=_strict_object)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BundleIntegrityError("malformed evidence JSONL") from exc
            if not isinstance(raw, dict):
                raise BundleIntegrityError("evidence record must be an object")
            record = _record_from_dict(raw)
            if record.sequence != expected_sequence:
                raise BundleIntegrityError("evidence sequence is not append-only")
            if record.previous_hash != expected_previous:
                raise BundleIntegrityError("evidence previous hash mismatch")
            computed = hashlib.sha256(_canonical_json(record.unsigned_dict())).hexdigest()
            if record.record_hash != computed:
                raise BundleIntegrityError("evidence record hash mismatch")
            records.append(record)
            expected_previous = record.record_hash
        return tuple(records)

    def load_verified(self) -> tuple[EmployeeEvidenceRecord, ...]:
        """Read and verify every record without creating a missing bundle."""

        try:
            payload = self.path.read_bytes()
        except FileNotFoundError:
            return ()
        return self._parse(payload)

    def append(
        self,
        *,
        gate_id: str,
        environment: str,
        tenant_hash: str,
        status: EmployeeEvidenceStatus,
        details: dict[str, Any],
        binding: EmployeeEnvironmentBinding,
        captured_at: float,
        attestor: str,
    ) -> BundleCheckpoint:
        """Append one fsynced record after validating the existing hash chain."""

        self.validate_candidate(
            gate_id=gate_id,
            environment=environment,
            tenant_hash=tenant_hash,
            status=status,
            details=details,
            binding=binding,
            captured_at=captured_at,
            attestor=attestor,
        )
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            chunks: list[bytes] = []
            while chunk := os.read(fd, 64 * 1024):
                chunks.append(chunk)
            existing = self._parse(b"".join(chunks))
            previous_hash = existing[-1].record_hash if existing else _EMPTY_HASH
            unsigned = {
                "schema_version": _SCHEMA_VERSION,
                "sequence": len(existing) + 1,
                "gate_id": gate_id,
                "environment": environment,
                "tenant_hash": tenant_hash,
                "status": status.value,
                "details": details,
                "binding": binding.to_dict(),
                "captured_at": float(captured_at),
                "attestor": attestor.strip(),
                "previous_hash": previous_hash,
            }
            record_hash = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
            payload = _canonical_json({**unsigned, "record_hash": record_hash}) + b"\n"
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
            os.fchmod(fd, 0o600)
            return BundleCheckpoint(record_count=len(existing) + 1, head_hash=record_hash)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def validate_candidate(
        *,
        gate_id: str,
        environment: str,
        tenant_hash: str,
        status: EmployeeEvidenceStatus,
        details: dict[str, Any],
        binding: EmployeeEnvironmentBinding,
        captured_at: float,
        attestor: str,
    ) -> None:
        """Validate one live capture without touching the append-only file."""

        if not _ID_RE.fullmatch(gate_id):
            raise ValueError("invalid gate_id")
        if environment not in {"tenant_staging", "tenant_production"}:
            raise ValueError("invalid environment")
        if not _HASH_RE.fullmatch(tenant_hash):
            raise ValueError("tenant_hash must be a lowercase sha256 digest")
        if isinstance(captured_at, bool) or not isinstance(captured_at, (int, float)) or captured_at <= 0:
            raise ValueError("captured_at must be a positive timestamp")
        if not isinstance(attestor, str) or not attestor.strip() or len(attestor) > 256:
            raise ValueError("attestor is required")
        if not isinstance(status, EmployeeEvidenceStatus):
            raise ValueError("invalid evidence status")
        if not isinstance(binding, EmployeeEnvironmentBinding):
            raise ValueError("invalid environment binding")
        if not isinstance(details, dict):
            raise ValueError("details must be an object")
        _validate_safe_evidence(details)


@dataclass(frozen=True)
class EmployeeReleaseEvaluation:
    """Structured, fail-closed result consumed by operators and composition."""

    status: EmployeeReleaseStatus
    passed: tuple[str, ...]
    pending: tuple[str, ...]
    failed: tuple[str, ...]
    violations: tuple[str, ...]
    record_count: int
    head_hash: str

    @property
    def release_available(self) -> bool:
        return self.status is EmployeeReleaseStatus.PASSED


def _number_at_least(details: Mapping[str, Any], key: str, minimum: float) -> bool:
    value = details.get(key)
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value >= minimum


def _gate_contract_passes(gate: EmployeeReleaseGate, record: EmployeeEvidenceRecord) -> bool:
    assertions = record.details.get("assertions")
    if not isinstance(assertions, dict) or any(assertions.get(name) is not True for name in gate.required_assertions):
        return False
    if gate.minimum_bot_count and not _number_at_least(record.details, "bot_count", gate.minimum_bot_count):
        return False
    if gate.minimum_duration_seconds and not _number_at_least(
        record.details, "duration_seconds", gate.minimum_duration_seconds
    ):
        return False
    for metric in gate.required_zero_metrics:
        if record.details.get(metric) != 0:
            return False
    for metric, maximum, exclusive in gate.maximum_metrics:
        value = record.details.get(metric)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value > maximum
            or (exclusive and value >= maximum)
        ):
            return False
    return True


def evaluate_employee_release(
    *,
    manifest: EmployeeReleaseManifest,
    bundle: EmployeeEvidenceBundle,
    binding: EmployeeEnvironmentBinding,
    now: float,
    checkpoint: BundleCheckpoint | None,
) -> EmployeeReleaseEvaluation:
    """Evaluate evidence without ever mutating runtime visibility configuration."""

    gate_ids = {gate.gate_id for gate in manifest.gates}
    if manifest.profile_id != binding.profile_id:
        return EmployeeReleaseEvaluation(
            status=EmployeeReleaseStatus.FAILED,
            passed=(),
            pending=tuple(sorted(gate_ids)),
            failed=(),
            violations=("release profile binding mismatch",),
            record_count=0,
            head_hash=_EMPTY_HASH,
        )
    try:
        records = bundle.load_verified()
    except (BundleIntegrityError, OSError, ValueError) as exc:
        return EmployeeReleaseEvaluation(
            status=EmployeeReleaseStatus.FAILED,
            passed=(),
            pending=tuple(sorted(gate_ids)),
            failed=(),
            violations=(f"bundle integrity failure: {exc}",),
            record_count=0,
            head_hash=_EMPTY_HASH,
        )
    head_hash = records[-1].record_hash if records else _EMPTY_HASH
    violations: list[str] = []
    fatal = False
    if checkpoint is None:
        violations.append("missing trusted bundle checkpoint")
    elif checkpoint.record_count != len(records) or checkpoint.head_hash != head_hash:
        violations.append("trusted bundle checkpoint mismatch")
        fatal = True
    latest: dict[str, EmployeeEvidenceRecord] = {}
    for record in records:
        if record.gate_id not in gate_ids:
            violations.append(f"unknown employee evidence gate: {record.gate_id}")
            fatal = True
            continue
        gate = next(item for item in manifest.gates if item.gate_id == record.gate_id)
        if record.environment != gate.environment:
            violations.append(f"environment mismatch for {record.gate_id}")
            fatal = True
            continue
        if record.binding != binding:
            violations.append(f"release binding mismatch for {record.gate_id}")
            fatal = True
            continue
        if record.tenant_hash != binding.tenant_hash_for(record.environment):
            violations.append(f"tenant binding mismatch for {record.gate_id}")
            fatal = True
            continue
        latest[record.gate_id] = record

    passed: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    for gate in manifest.gates:
        record = latest.get(gate.gate_id)
        if record is None:
            pending.append(gate.gate_id)
            continue
        if record.captured_at > now + 300:
            violations.append(f"future evidence timestamp for {gate.gate_id}")
            failed.append(gate.gate_id)
        elif now - record.captured_at > gate.max_age_seconds:
            pending.append(gate.gate_id)
        elif record.status is EmployeeEvidenceStatus.FAILED:
            failed.append(gate.gate_id)
        elif record.status is not EmployeeEvidenceStatus.PASSED:
            pending.append(gate.gate_id)
        elif not _gate_contract_passes(gate, record):
            pending.append(gate.gate_id)
        else:
            passed.append(gate.gate_id)

    if fatal or failed:
        status = EmployeeReleaseStatus.FAILED
    elif pending or checkpoint is None:
        status = EmployeeReleaseStatus.PENDING
    else:
        status = EmployeeReleaseStatus.PASSED
    return EmployeeReleaseEvaluation(
        status=status,
        passed=tuple(passed),
        pending=tuple(pending),
        failed=tuple(failed),
        violations=tuple(violations),
        record_count=len(records),
        head_hash=head_hash,
    )
