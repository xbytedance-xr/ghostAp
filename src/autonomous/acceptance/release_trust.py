"""External deployment authority for employee release admission.

The application deliberately does not own the QA trust root or the monotonic
attestation ledger.  A root-owned local broker verifies those properties for the
running workload and returns a short-lived, nonce-bound capability.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..journal.anchor import AnchorState
from .employee_release import (
    BundleCheckpoint,
    EmployeeEnvironmentBinding,
    EmployeeEvidenceBundle,
    EmployeeReleaseAttestation,
    EmployeeReleaseManifest,
    EmployeeReleaseStatus,
    evaluate_employee_release,
)

_PROTOCOL_VERSION = 1
_MAX_RESPONSE_BYTES = 64 * 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,511}$")


class ReleaseTrustError(RuntimeError):
    """The external release authority could not prove a safe capability."""


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseTrustError(f"duplicate release broker key: {key}")
        result[key] = value
    return result


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReleaseTrustError(f"invalid {field}")
    result = float(value)
    if not math.isfinite(result):
        raise ReleaseTrustError(f"invalid {field}")
    return result


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseTrustError(f"invalid {field}")
    return value


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ReleaseTrustError(f"invalid {field}")
    return value


def _activation_fence_targets(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ReleaseTrustError("invalid main Bot activation fence target set")
    targets = tuple(value)
    if (
        not targets
        or len(targets) > 64
        or targets != tuple(sorted(set(targets)))
        or any(not isinstance(item, str) or not _HASH_RE.fullmatch(item) for item in targets)
    ):
        raise ReleaseTrustError("invalid main Bot activation fence target set")
    return targets


def _attestation_dict(value: EmployeeReleaseAttestation) -> dict[str, Any]:
    return {**value.unsigned_dict(), "signature": value.signature}


@dataclass(frozen=True)
class ReleaseTrustLease:
    """Short-lived deployment authorization returned by the external broker."""

    binding: EmployeeEnvironmentBinding
    checkpoint: BundleCheckpoint
    lease_id: str
    workload_identity: str
    workload_digest: str
    ledger_sequence: int
    consumption_id: str
    witness_id: str
    witness_sequence: int
    issued_at: float
    expires_at: float
    recovery_expires_at: float

    def __post_init__(self) -> None:
        _identifier(self.lease_id, "lease_id")
        _identifier(self.workload_identity, "workload_identity")
        if not _HASH_RE.fullmatch(self.workload_digest):
            raise ReleaseTrustError("invalid workload digest")
        _positive_int(self.ledger_sequence, "ledger_sequence")
        _identifier(self.consumption_id, "consumption_id")
        _identifier(self.witness_id, "witness_id")
        _positive_int(self.witness_sequence, "witness_sequence")
        issued_at = _finite_number(self.issued_at, "issued_at")
        expires_at = _finite_number(self.expires_at, "expires_at")
        recovery_expires_at = _finite_number(
            self.recovery_expires_at,
            "recovery_expires_at",
        )
        if expires_at <= issued_at or recovery_expires_at <= expires_at:
            raise ReleaseTrustError("invalid release capability lifetime")

    def valid_at(self, now: float) -> bool:
        return (
            not isinstance(now, bool)
            and isinstance(now, (int, float))
            and math.isfinite(float(now))
            and self.issued_at <= float(now) + 5
            and float(now) < self.expires_at
            and float(now) < self.recovery_expires_at
        )


class ReleaseTrustProvider(Protocol):
    """External, deployment-owned release authority."""

    def consume(self, attestation: EmployeeReleaseAttestation) -> ReleaseTrustLease: ...

    def renew(self, lease: ReleaseTrustLease) -> ReleaseTrustLease: ...

    def close(self) -> None: ...


class RuntimeReleaseTrustSession:
    """Own one externally consumed attestation and its renewable capability."""

    def __init__(
        self,
        provider: ReleaseTrustProvider,
        lease: ReleaseTrustLease,
    ) -> None:
        self._provider = provider
        self._lease = lease
        self._anchor_witness_sequence = lease.witness_sequence
        self._main_bot_audit_sequence = 0
        self._main_bot_activation_fences: dict[
            str,
            tuple[str, tuple[str, ...]],
        ] = {}
        self._lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
        self._closed = False

    @property
    def lease(self) -> ReleaseTrustLease:
        with self._lock:
            return self._lease

    def valid(self, now: float) -> bool:
        with self._lock:
            return not self._closed and self._lease.valid_at(now)

    def renew_if_needed(
        self,
        now: float,
        *,
        renewal_window_seconds: float = 120.0,
    ) -> bool:
        current_time = _finite_number(now, "renewal time")
        if (
            isinstance(renewal_window_seconds, bool)
            or not isinstance(renewal_window_seconds, (int, float))
            or not math.isfinite(float(renewal_window_seconds))
            or renewal_window_seconds <= 0
        ):
            raise ValueError("renewal window must be positive")
        with self._lock:
            if self._closed or not self._lease.valid_at(current_time):
                return False
            if self._lease.expires_at - current_time > float(renewal_window_seconds):
                return True
            previous = self._lease
            renewed = self._provider.renew(previous)
            if not isinstance(renewed, ReleaseTrustLease) or (
                renewed.binding != previous.binding
                or renewed.checkpoint != previous.checkpoint
                or renewed.lease_id != previous.lease_id
                or renewed.workload_identity != previous.workload_identity
                or renewed.workload_digest != previous.workload_digest
                or renewed.ledger_sequence != previous.ledger_sequence
                or renewed.consumption_id != previous.consumption_id
                or renewed.witness_id != previous.witness_id
                or renewed.witness_sequence <= previous.witness_sequence
                or renewed.expires_at <= previous.expires_at
                or renewed.recovery_expires_at <= previous.recovery_expires_at
                or not renewed.valid_at(current_time)
            ):
                raise ReleaseTrustError("release capability renewal lineage mismatch")
            self._lease = renewed
            self._anchor_witness_sequence = max(
                self._anchor_witness_sequence,
                renewed.witness_sequence,
            )
            return True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._provider.close()

    def journal_anchor(self, scope: str) -> ExternalWitnessAnchor:
        if not isinstance(scope, str) or not _ID_RE.fullmatch(scope):
            raise ReleaseTrustError("invalid external anchor scope")
        if not callable(getattr(self._provider, "read_anchor", None)) or not callable(
            getattr(self._provider, "compare_and_swap_anchor", None)
        ):
            raise ReleaseTrustError("external release provider has no witness anchor")
        return ExternalWitnessAnchor(self, scope)

    def _read_anchor(self, scope: str) -> tuple[AnchorState, int]:
        with self._lock:
            if self._closed or not self._lease.valid_at(time.time()):
                raise ReleaseTrustError("release capability is expired")
            state, witness = self._provider.read_anchor(  # type: ignore[attr-defined]
                self._lease,
                scope,
            )
            self._accept_anchor_witness(witness)
            return state, witness

    def _compare_and_swap_anchor(
        self,
        scope: str,
        expected: AnchorState,
        new: AnchorState,
    ) -> tuple[bool, AnchorState, int]:
        with self._lock:
            if self._closed or not self._lease.valid_at(time.time()):
                raise ReleaseTrustError("release capability is expired")
            swapped, observed, witness = self._provider.compare_and_swap_anchor(  # type: ignore[attr-defined]
                self._lease,
                scope,
                expected,
                new,
            )
            self._accept_anchor_witness(witness)
            return swapped, observed, witness

    def _accept_anchor_witness(self, witness: int) -> None:
        if (
            isinstance(witness, bool)
            or not isinstance(witness, int)
            or witness <= self._anchor_witness_sequence
        ):
            raise ReleaseTrustError("external anchor witness did not advance")
        self._anchor_witness_sequence = witness

    def record_main_bot_send_attempt(
        self,
        *,
        attempt_id: str,
        tenant_hash: str,
        operation: str,
        target_hash: str,
        attempted_at: float,
    ) -> None:
        _identifier(attempt_id, "main Bot audit attempt_id")
        if tenant_hash and not _HASH_RE.fullmatch(tenant_hash):
            raise ReleaseTrustError("invalid main Bot audit tenant hash")
        if operation not in {"create", "reply", "patch"}:
            raise ReleaseTrustError("invalid main Bot audit operation")
        if not _HASH_RE.fullmatch(target_hash):
            raise ReleaseTrustError("invalid main Bot audit target hash")
        timestamp = _finite_number(attempted_at, "main Bot audit timestamp")
        with self._lock:
            if self._closed or not self._lease.valid_at(time.time()):
                raise ReleaseTrustError("release capability is expired")
            method = getattr(self._provider, "record_main_bot_send_attempt", None)
            if not callable(method):
                raise ReleaseTrustError("external main Bot audit is unavailable")
            audit_sequence, witness = method(
                self._lease,
                attempt_id=attempt_id,
                tenant_hash=tenant_hash,
                operation=operation,
                target_hash=target_hash,
                attempted_at=timestamp,
            )
            if (
                isinstance(audit_sequence, bool)
                or not isinstance(audit_sequence, int)
                or audit_sequence <= self._main_bot_audit_sequence
            ):
                raise ReleaseTrustError("external main Bot audit did not advance")
            self._accept_anchor_witness(witness)
            self._main_bot_audit_sequence = audit_sequence

    def count_main_bot_send_attempts(
        self,
        tenant_key: str,
        start: float,
        end: float,
    ) -> int:
        if not isinstance(tenant_key, str) or not tenant_key:
            raise ReleaseTrustError("main Bot audit tenant is required")
        started_at = _finite_number(start, "main Bot audit start")
        ended_at = _finite_number(end, "main Bot audit end")
        if started_at > ended_at:
            raise ReleaseTrustError("invalid main Bot audit window")
        tenant_hash = hashlib.sha256(tenant_key.encode("utf-8")).hexdigest()
        with self._lock:
            if self._closed or not self._lease.valid_at(time.time()):
                raise ReleaseTrustError("release capability is expired")
            method = getattr(self._provider, "count_main_bot_send_attempts", None)
            if not callable(method):
                raise ReleaseTrustError("external main Bot audit is unavailable")
            count, audit_sequence, witness = method(
                self._lease,
                tenant_hash=tenant_hash,
                start=started_at,
                end=ended_at,
            )
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or isinstance(audit_sequence, bool)
                or not isinstance(audit_sequence, int)
                or audit_sequence < self._main_bot_audit_sequence
            ):
                raise ReleaseTrustError("external main Bot audit query is invalid")
            self._accept_anchor_witness(witness)
            self._main_bot_audit_sequence = audit_sequence
            return count

    def count_main_bot_target_send_attempts(
        self,
        tenant_key: str,
        target_hash: str,
        start: float,
        end: float,
    ) -> int:
        if not isinstance(tenant_key, str) or not tenant_key:
            raise ReleaseTrustError("main Bot audit tenant is required")
        if not _HASH_RE.fullmatch(target_hash):
            raise ReleaseTrustError("invalid main Bot audit target hash")
        started_at = _finite_number(start, "main Bot audit start")
        ended_at = _finite_number(end, "main Bot audit end")
        if started_at > ended_at:
            raise ReleaseTrustError("invalid main Bot audit window")
        tenant_hash = hashlib.sha256(tenant_key.encode("utf-8")).hexdigest()
        with self._lock:
            if self._closed or not self._lease.valid_at(time.time()):
                raise ReleaseTrustError("release capability is expired")
            method = getattr(
                self._provider,
                "count_main_bot_target_send_attempts",
                None,
            )
            if not callable(method):
                raise ReleaseTrustError("external main Bot target audit is unavailable")
            count, audit_sequence, witness = method(
                self._lease,
                tenant_hash=tenant_hash,
                target_hash=target_hash,
                start=started_at,
                end=ended_at,
            )
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or isinstance(audit_sequence, bool)
                or not isinstance(audit_sequence, int)
                or audit_sequence < self._main_bot_audit_sequence
            ):
                raise ReleaseTrustError("external main Bot target audit query is invalid")
            self._accept_anchor_witness(witness)
            self._main_bot_audit_sequence = audit_sequence
            return count

    @property
    def main_bot_activation_fence_ready(self) -> bool:
        with self._lock:
            return (
                not self._closed
                and self._lease.valid_at(time.time())
                and callable(
                    getattr(
                        self._provider,
                        "acquire_main_bot_activation_fence",
                        None,
                    )
                )
                and callable(
                    getattr(
                        self._provider,
                        "release_main_bot_activation_fence",
                        None,
                    )
                )
            )

    def acquire_main_bot_activation_fence(
        self,
        tenant_key: str,
        target_hashes: tuple[str, ...],
    ) -> str:
        if not isinstance(tenant_key, str) or not tenant_key:
            raise ReleaseTrustError("main Bot activation fence tenant is required")
        targets = _activation_fence_targets(target_hashes)
        tenant_hash = hashlib.sha256(tenant_key.encode("utf-8")).hexdigest()
        with self._lock:
            if self._closed or not self._lease.valid_at(time.time()):
                raise ReleaseTrustError("release capability is expired")
            method = getattr(
                self._provider,
                "acquire_main_bot_activation_fence",
                None,
            )
            if not callable(method):
                raise ReleaseTrustError(
                    "external main Bot activation fence is unavailable"
                )
            witness_floor = self._anchor_witness_sequence
            result = method(
                self._lease,
                tenant_hash=tenant_hash,
                target_hashes=targets,
                witness_sequence=witness_floor,
            )
            if not isinstance(result, tuple) or len(result) != 3:
                raise ReleaseTrustError(
                    "external main Bot activation fence response is invalid"
                )
            fence_id, audit_sequence, witness = result
            fence_id = _identifier(fence_id, "main Bot activation fence_id")
            if (
                fence_id in self._main_bot_activation_fences
                or isinstance(audit_sequence, bool)
                or not isinstance(audit_sequence, int)
                or audit_sequence < self._main_bot_audit_sequence
                or isinstance(witness, bool)
                or not isinstance(witness, int)
                or witness <= witness_floor
            ):
                raise ReleaseTrustError(
                    "external main Bot activation fence witness is invalid"
                )
            self._accept_anchor_witness(witness)
            self._main_bot_audit_sequence = audit_sequence
            self._main_bot_activation_fences[fence_id] = (tenant_hash, targets)
            return fence_id

    def release_main_bot_activation_fence(
        self,
        tenant_key: str,
        target_hashes: tuple[str, ...],
        *,
        fence_id: str,
    ) -> None:
        if not isinstance(tenant_key, str) or not tenant_key:
            raise ReleaseTrustError("main Bot activation fence tenant is required")
        targets = _activation_fence_targets(target_hashes)
        tenant_hash = hashlib.sha256(tenant_key.encode("utf-8")).hexdigest()
        fence_id = _identifier(fence_id, "main Bot activation fence_id")
        with self._lock:
            if self._closed or not self._lease.valid_at(time.time()):
                raise ReleaseTrustError("release capability is expired")
            if self._main_bot_activation_fences.get(fence_id) != (
                tenant_hash,
                targets,
            ):
                raise ReleaseTrustError(
                    "external main Bot activation fence binding mismatch"
                )
            method = getattr(
                self._provider,
                "release_main_bot_activation_fence",
                None,
            )
            if not callable(method):
                raise ReleaseTrustError(
                    "external main Bot activation fence is unavailable"
                )
            witness_floor = self._anchor_witness_sequence
            result = method(
                self._lease,
                tenant_hash=tenant_hash,
                target_hashes=targets,
                fence_id=fence_id,
                witness_sequence=witness_floor,
            )
            if not isinstance(result, tuple) or len(result) != 2:
                raise ReleaseTrustError(
                    "external main Bot activation fence response is invalid"
                )
            audit_sequence, witness = result
            if (
                isinstance(audit_sequence, bool)
                or not isinstance(audit_sequence, int)
                or audit_sequence < self._main_bot_audit_sequence
                or isinstance(witness, bool)
                or not isinstance(witness, int)
                or witness <= witness_floor
            ):
                raise ReleaseTrustError(
                    "external main Bot activation fence witness is invalid"
                )
            self._accept_anchor_witness(witness)
            self._main_bot_audit_sequence = audit_sequence
            self._main_bot_activation_fences.pop(fence_id)


class ExternalWitnessAnchor:
    """Journal AnchorProvider backed by the deployment trust broker."""

    production_safe = True

    def __init__(self, session: RuntimeReleaseTrustSession, scope: str) -> None:
        self._session = session
        self._scope = scope
        self._witness_sequence = session.lease.witness_sequence
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    def read(self) -> AnchorState:
        with self._lock:
            state, witness_sequence = self._session._read_anchor(self._scope)
            self._advance_witness(witness_sequence)
            return state

    def compare_and_swap(
        self,
        expected_sequence: int,
        expected_hash: str,
        new_sequence: int,
        new_hash: str,
    ) -> bool:
        try:
            expected = AnchorState(expected_sequence, expected_hash)
            new = AnchorState(new_sequence, new_hash)
        except (TypeError, ValueError):
            return False
        if (
            isinstance(expected_sequence, bool)
            or isinstance(new_sequence, bool)
            or expected_sequence < 0
            or new_sequence != expected_sequence + 1
            or not _HASH_RE.fullmatch(expected_hash)
            or not _HASH_RE.fullmatch(new_hash)
        ):
            return False
        with self._lock:
            swapped, observed, witness_sequence = self._session._compare_and_swap_anchor(
                self._scope,
                expected,
                new,
            )
            self._advance_witness(witness_sequence)
            if swapped and observed != new:
                raise ReleaseTrustError("external anchor CAS result mismatch")
            if not swapped and observed == expected:
                raise ReleaseTrustError("external anchor rejected an unchanged head")
            return swapped

    def _advance_witness(self, value: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= self._witness_sequence
        ):
            raise ReleaseTrustError("external anchor witness did not advance")
        self._witness_sequence = value


def _runtime_binding(settings: Any) -> EmployeeEnvironmentBinding:
    return EmployeeEnvironmentBinding(
        profile_id="employee-release-v1",
        release_id=getattr(settings, "autonomous_employee_release_id", ""),
        commit_sha=getattr(settings, "autonomous_employee_commit_sha", ""),
        service_instance_id=getattr(
            settings,
            "autonomous_employee_service_instance_id",
            "",
        ),
        staging_tenant_hash=getattr(
            settings,
            "autonomous_employee_staging_tenant_hash",
            "",
        ),
        production_tenant_hash=getattr(
            settings,
            "autonomous_employee_production_tenant_hash",
            "",
        ),
    )


def authorize_runtime_employee_release(
    settings: Any,
    provider: ReleaseTrustProvider,
    *,
    now: float | None = None,
) -> RuntimeReleaseTrustSession:
    """Validate local claims, then consume them through the external authority."""

    if provider is None:
        raise ReleaseTrustError("external release trust provider is required")
    evaluated_at = time.time() if now is None else _finite_number(now, "evaluation time")
    try:
        binding = _runtime_binding(settings)
        manifest = EmployeeReleaseManifest.load(
            Path(__file__).with_name("employee_release_manifest.json")
        )
        attestation = EmployeeReleaseAttestation.load(
            Path(
                getattr(
                    settings,
                    "autonomous_employee_release_checkpoint",
                    "",
                )
            ).expanduser()
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ReleaseTrustError("employee release claims are invalid") from exc
    if attestation.binding != binding:
        raise ReleaseTrustError("employee release attestation binding mismatch")
    evaluation = evaluate_employee_release(
        manifest=manifest,
        bundle=EmployeeEvidenceBundle(
            Path(
                getattr(
                    settings,
                    "autonomous_employee_release_evidence_bundle",
                    "",
                )
            ).expanduser()
        ),
        binding=binding,
        now=evaluated_at,
        checkpoint=attestation.checkpoint,
    )
    if evaluation.status is not EmployeeReleaseStatus.PASSED:
        raise ReleaseTrustError("employee release evidence is not complete and fresh")
    try:
        lease = provider.consume(attestation)
    except ReleaseTrustError:
        raise
    except Exception as exc:
        raise ReleaseTrustError("external release authority failed") from exc
    if (
        not isinstance(lease, ReleaseTrustLease)
        or lease.binding != binding
        or lease.checkpoint != attestation.checkpoint
        or not lease.valid_at(evaluated_at)
    ):
        raise ReleaseTrustError("external release authority returned an invalid lease")
    return RuntimeReleaseTrustSession(provider, lease)


class RootOwnedUnixReleaseTrustBroker:
    """Authenticate a release broker through Unix socket ownership and peer UID."""

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 2.0,
        expected_peer_uid: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path).expanduser()
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
            or timeout_seconds > 30
        ):
            raise ValueError("release broker timeout must be in (0, 30]")
        if (
            isinstance(expected_peer_uid, bool)
            or not isinstance(expected_peer_uid, int)
            or expected_peer_uid < 0
        ):
            raise ValueError("expected peer uid must be non-negative")
        self.timeout_seconds = float(timeout_seconds)
        self.expected_peer_uid = expected_peer_uid
        self._clock = clock

    def consume(self, attestation: EmployeeReleaseAttestation) -> ReleaseTrustLease:
        if not isinstance(attestation, EmployeeReleaseAttestation):
            raise TypeError("attestation must be EmployeeReleaseAttestation")
        nonce = secrets.token_hex(32)
        response = self._exchange(
            {
                "protocol_version": _PROTOCOL_VERSION,
                "operation": "consume_release_attestation",
                "nonce": nonce,
                "pid": os.getpid(),
                "attestation": _attestation_dict(attestation),
            }
        )
        return self._parse_lease(
            response,
            nonce=nonce,
            expected_binding=attestation.binding,
            expected_checkpoint=attestation.checkpoint,
            previous=None,
        )

    def renew(self, lease: ReleaseTrustLease) -> ReleaseTrustLease:
        if not isinstance(lease, ReleaseTrustLease):
            raise TypeError("lease must be ReleaseTrustLease")
        nonce = secrets.token_hex(32)
        response = self._exchange(
            {
                "protocol_version": _PROTOCOL_VERSION,
                "operation": "renew_recovery_capability",
                "nonce": nonce,
                "pid": os.getpid(),
                "lease_id": lease.lease_id,
                "binding": lease.binding.to_dict(),
                "checkpoint": lease.checkpoint.to_dict(),
                "ledger_sequence": lease.ledger_sequence,
                "consumption_id": lease.consumption_id,
                "witness_sequence": lease.witness_sequence,
            }
        )
        return self._parse_lease(
            response,
            nonce=nonce,
            expected_binding=lease.binding,
            expected_checkpoint=lease.checkpoint,
            previous=lease,
        )

    def close(self) -> None:
        """The client keeps no persistent socket or secret material."""

    def read_anchor(
        self,
        lease: ReleaseTrustLease,
        scope: str,
    ) -> tuple[AnchorState, int]:
        nonce = secrets.token_hex(32)
        response = self._exchange(
            self._anchor_request(
                operation="read_journal_anchor",
                nonce=nonce,
                lease=lease,
                scope=scope,
            )
        )
        _swapped, state, witness = self._parse_anchor_response(
            response,
            nonce=nonce,
            lease=lease,
            scope=scope,
            expect_swapped=False,
        )
        return state, witness

    def compare_and_swap_anchor(
        self,
        lease: ReleaseTrustLease,
        scope: str,
        expected: AnchorState,
        new: AnchorState,
    ) -> tuple[bool, AnchorState, int]:
        nonce = secrets.token_hex(32)
        request = self._anchor_request(
            operation="compare_and_swap_journal_anchor",
            nonce=nonce,
            lease=lease,
            scope=scope,
        )
        request.update(
            {
                "expected": {
                    "sequence": expected.sequence,
                    "frame_hash": expected.frame_hash,
                },
                "new": {
                    "sequence": new.sequence,
                    "frame_hash": new.frame_hash,
                },
            }
        )
        response = self._exchange(request)
        return self._parse_anchor_response(
            response,
            nonce=nonce,
            lease=lease,
            scope=scope,
            expect_swapped=True,
        )

    def record_main_bot_send_attempt(
        self,
        lease: ReleaseTrustLease,
        *,
        attempt_id: str,
        tenant_hash: str,
        operation: str,
        target_hash: str,
        attempted_at: float,
    ) -> tuple[int, int]:
        nonce = secrets.token_hex(32)
        response = self._exchange(
            {
                **self._lease_request(
                    operation="record_main_bot_send_attempt",
                    nonce=nonce,
                    lease=lease,
                ),
                "attempt_id": attempt_id,
                "tenant_hash": tenant_hash,
                "message_operation": operation,
                "target_hash": target_hash,
                "attempted_at": attempted_at,
            }
        )
        return self._parse_main_bot_audit_response(
            response,
            nonce=nonce,
            lease=lease,
            include_count=False,
        )[1:]

    def count_main_bot_send_attempts(
        self,
        lease: ReleaseTrustLease,
        *,
        tenant_hash: str,
        start: float,
        end: float,
    ) -> tuple[int, int, int]:
        nonce = secrets.token_hex(32)
        response = self._exchange(
            {
                **self._lease_request(
                    operation="count_main_bot_send_attempts",
                    nonce=nonce,
                    lease=lease,
                ),
                "tenant_hash": tenant_hash,
                "start": start,
                "end": end,
            }
        )
        return self._parse_main_bot_audit_response(
            response,
            nonce=nonce,
            lease=lease,
            include_count=True,
        )

    def count_main_bot_target_send_attempts(
        self,
        lease: ReleaseTrustLease,
        *,
        tenant_hash: str,
        target_hash: str,
        start: float,
        end: float,
    ) -> tuple[int, int, int]:
        nonce = secrets.token_hex(32)
        response = self._exchange(
            {
                **self._lease_request(
                    operation="count_main_bot_target_send_attempts",
                    nonce=nonce,
                    lease=lease,
                ),
                "tenant_hash": tenant_hash,
                "target_hash": target_hash,
                "start": start,
                "end": end,
            }
        )
        return self._parse_main_bot_audit_response(
            response,
            nonce=nonce,
            lease=lease,
            include_count=True,
        )

    def acquire_main_bot_activation_fence(
        self,
        lease: ReleaseTrustLease,
        *,
        tenant_hash: str,
        target_hashes: tuple[str, ...],
        witness_sequence: int | None = None,
    ) -> tuple[str, int, int]:
        targets = _activation_fence_targets(target_hashes)
        if not _HASH_RE.fullmatch(tenant_hash):
            raise ReleaseTrustError("invalid main Bot activation fence tenant hash")
        witness_floor = (
            lease.witness_sequence
            if witness_sequence is None
            else _positive_int(witness_sequence, "witness_sequence")
        )
        if witness_floor < lease.witness_sequence:
            raise ReleaseTrustError("invalid main Bot activation fence witness")
        nonce = secrets.token_hex(32)
        response = self._exchange(
            {
                **self._lease_request(
                    operation="acquire_main_bot_activation_fence",
                    nonce=nonce,
                    lease=lease,
                ),
                "tenant_hash": tenant_hash,
                "target_hashes": list(targets),
                "witness_sequence": witness_floor,
            }
        )
        fence_id, audit_sequence, witness = self._parse_activation_fence_response(
            response,
            nonce=nonce,
            lease=lease,
            tenant_hash=tenant_hash,
            target_hashes=targets,
            fence_id=None,
            witness_floor=witness_floor,
        )
        return fence_id, audit_sequence, witness

    def release_main_bot_activation_fence(
        self,
        lease: ReleaseTrustLease,
        *,
        tenant_hash: str,
        target_hashes: tuple[str, ...],
        fence_id: str,
        witness_sequence: int | None = None,
    ) -> tuple[int, int]:
        targets = _activation_fence_targets(target_hashes)
        if not _HASH_RE.fullmatch(tenant_hash):
            raise ReleaseTrustError("invalid main Bot activation fence tenant hash")
        fence_id = _identifier(fence_id, "main Bot activation fence_id")
        witness_floor = (
            lease.witness_sequence
            if witness_sequence is None
            else _positive_int(witness_sequence, "witness_sequence")
        )
        if witness_floor < lease.witness_sequence:
            raise ReleaseTrustError("invalid main Bot activation fence witness")
        nonce = secrets.token_hex(32)
        response = self._exchange(
            {
                **self._lease_request(
                    operation="release_main_bot_activation_fence",
                    nonce=nonce,
                    lease=lease,
                ),
                "tenant_hash": tenant_hash,
                "target_hashes": list(targets),
                "fence_id": fence_id,
                "witness_sequence": witness_floor,
            }
        )
        _fence_id, audit_sequence, witness = self._parse_activation_fence_response(
            response,
            nonce=nonce,
            lease=lease,
            tenant_hash=tenant_hash,
            target_hashes=targets,
            fence_id=fence_id,
            witness_floor=witness_floor,
        )
        return audit_sequence, witness

    @staticmethod
    def _lease_request(
        *,
        operation: str,
        nonce: str,
        lease: ReleaseTrustLease,
    ) -> dict[str, Any]:
        return {
            "protocol_version": _PROTOCOL_VERSION,
            "operation": operation,
            "nonce": nonce,
            "pid": os.getpid(),
            "lease_id": lease.lease_id,
            "binding": lease.binding.to_dict(),
            "checkpoint": lease.checkpoint.to_dict(),
            "ledger_sequence": lease.ledger_sequence,
            "consumption_id": lease.consumption_id,
        }

    @staticmethod
    def _parse_main_bot_audit_response(
        response: Mapping[str, Any],
        *,
        nonce: str,
        lease: ReleaseTrustLease,
        include_count: bool,
    ) -> tuple[int, int, int]:
        required = {
            "protocol_version",
            "decision",
            "nonce",
            "lease_id",
            "audit_sequence",
            "witness_sequence",
        }
        if include_count:
            required.update({"complete", "count"})
        if set(response) != required or response.get("protocol_version") != _PROTOCOL_VERSION:
            raise ReleaseTrustError("invalid external main Bot audit response schema")
        if response.get("decision") != "allow":
            raise ReleaseTrustError("external main Bot audit operation denied")
        if response.get("nonce") != nonce or response.get("lease_id") != lease.lease_id:
            raise ReleaseTrustError("external main Bot audit binding mismatch")
        audit_sequence = response.get("audit_sequence")
        witness = response.get("witness_sequence")
        if (
            isinstance(audit_sequence, bool)
            or not isinstance(audit_sequence, int)
            or audit_sequence < 0
            or isinstance(witness, bool)
            or not isinstance(witness, int)
            or witness <= lease.witness_sequence
        ):
            raise ReleaseTrustError("external main Bot audit sequence is invalid")
        count = 0
        if include_count:
            count = response.get("count")
            if (
                response.get("complete") is not True
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise ReleaseTrustError("external main Bot audit query is incomplete")
        return count, audit_sequence, witness

    @staticmethod
    def _parse_activation_fence_response(
        response: Mapping[str, Any],
        *,
        nonce: str,
        lease: ReleaseTrustLease,
        tenant_hash: str,
        target_hashes: tuple[str, ...],
        fence_id: str | None,
        witness_floor: int,
    ) -> tuple[str, int, int]:
        required = {
            "protocol_version",
            "decision",
            "nonce",
            "lease_id",
            "tenant_hash",
            "target_hashes",
            "fence_id",
            "audit_sequence",
            "witness_sequence",
        }
        if set(response) != required or response.get("protocol_version") != _PROTOCOL_VERSION:
            raise ReleaseTrustError(
                "invalid external main Bot activation fence response schema"
            )
        if response.get("decision") != "allow":
            raise ReleaseTrustError("external main Bot activation fence denied")
        if response.get("nonce") != nonce:
            raise ReleaseTrustError("external main Bot activation fence nonce mismatch")
        response_fence_id = _identifier(
            response.get("fence_id"),
            "main Bot activation fence_id",
        )
        try:
            response_targets = _activation_fence_targets(
                response.get("target_hashes")
            )
        except ReleaseTrustError as exc:
            raise ReleaseTrustError(
                "external main Bot activation fence binding mismatch"
            ) from exc
        if (
            response.get("lease_id") != lease.lease_id
            or response.get("tenant_hash") != tenant_hash
            or response_targets != target_hashes
            or (fence_id is not None and response_fence_id != fence_id)
        ):
            raise ReleaseTrustError(
                "external main Bot activation fence binding mismatch"
            )
        audit_sequence = response.get("audit_sequence")
        witness = response.get("witness_sequence")
        if (
            isinstance(audit_sequence, bool)
            or not isinstance(audit_sequence, int)
            or audit_sequence < 0
            or isinstance(witness, bool)
            or not isinstance(witness, int)
            or witness <= witness_floor
        ):
            raise ReleaseTrustError(
                "external main Bot activation fence witness is invalid"
            )
        return response_fence_id, audit_sequence, witness

    @staticmethod
    def _anchor_request(
        *,
        operation: str,
        nonce: str,
        lease: ReleaseTrustLease,
        scope: str,
    ) -> dict[str, Any]:
        if not _ID_RE.fullmatch(scope):
            raise ReleaseTrustError("invalid external anchor scope")
        return {
            **RootOwnedUnixReleaseTrustBroker._lease_request(
                operation=operation,
                nonce=nonce,
                lease=lease,
            ),
            "anchor_scope": scope,
        }

    @staticmethod
    def _parse_anchor_state(value: object) -> AnchorState:
        if not isinstance(value, dict) or set(value) != {"sequence", "frame_hash"}:
            raise ReleaseTrustError("invalid external anchor state")
        sequence = value.get("sequence")
        frame_hash = value.get("frame_hash")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence < 0
            or not isinstance(frame_hash, str)
            or not _HASH_RE.fullmatch(frame_hash)
            or (sequence == 0 and frame_hash != "0" * 64)
        ):
            raise ReleaseTrustError("invalid external anchor state")
        return AnchorState(sequence, frame_hash)

    def _parse_anchor_response(
        self,
        response: Mapping[str, Any],
        *,
        nonce: str,
        lease: ReleaseTrustLease,
        scope: str,
        expect_swapped: bool,
    ) -> tuple[bool, AnchorState, int]:
        required = {
            "protocol_version",
            "decision",
            "nonce",
            "lease_id",
            "anchor_scope",
            "state",
            "witness_sequence",
        }
        if expect_swapped:
            required.add("swapped")
        if set(response) != required or response.get("protocol_version") != _PROTOCOL_VERSION:
            raise ReleaseTrustError("invalid external anchor response schema")
        if response.get("decision") != "allow":
            raise ReleaseTrustError("external anchor operation denied")
        if response.get("nonce") != nonce:
            raise ReleaseTrustError("external anchor nonce mismatch")
        if response.get("lease_id") != lease.lease_id or response.get("anchor_scope") != scope:
            raise ReleaseTrustError("external anchor binding mismatch")
        witness = _positive_int(response.get("witness_sequence"), "witness_sequence")
        if witness <= lease.witness_sequence:
            raise ReleaseTrustError("external anchor witness did not advance")
        state = self._parse_anchor_state(response.get("state"))
        swapped = response.get("swapped", False)
        if not isinstance(swapped, bool):
            raise ReleaseTrustError("invalid external anchor CAS verdict")
        return swapped, state, witness

    def _validate_socket(self) -> None:
        try:
            metadata = self.path.lstat()
        except OSError as exc:
            raise ReleaseTrustError("release broker socket is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISSOCK(metadata.st_mode):
            raise ReleaseTrustError("release broker path is not a socket")
        if metadata.st_uid != self.expected_peer_uid:
            raise ReleaseTrustError("release broker socket owner mismatch")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ReleaseTrustError("release broker socket is group/other writable")

    def _exchange(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_socket()
        payload = json.dumps(
            dict(request),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout_seconds)
        try:
            connection.connect(str(self.path))
            if not hasattr(socket, "SO_PEERCRED"):
                raise ReleaseTrustError("release broker peer credentials are unsupported")
            credentials = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
            if uid != self.expected_peer_uid:
                raise ReleaseTrustError("release broker peer uid mismatch")
            connection.sendall(payload)
            response = bytearray()
            while not response.endswith(b"\n"):
                chunk = connection.recv(4096)
                if not chunk:
                    raise ReleaseTrustError("release broker response was truncated")
                response.extend(chunk)
                if len(response) > _MAX_RESPONSE_BYTES:
                    raise ReleaseTrustError("release broker response is too large")
            if b"\n" in response[:-1]:
                raise ReleaseTrustError("release broker returned multiple frames")
        except ReleaseTrustError:
            raise
        except OSError as exc:
            raise ReleaseTrustError("release broker exchange failed") from exc
        finally:
            connection.close()
        try:
            decoded = json.loads(bytes(response), object_pairs_hook=_strict_object)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ReleaseTrustError("release broker returned malformed JSON") from exc
        if not isinstance(decoded, dict):
            raise ReleaseTrustError("release broker response must be an object")
        return decoded

    def _parse_lease(
        self,
        response: Mapping[str, Any],
        *,
        nonce: str,
        expected_binding: EmployeeEnvironmentBinding,
        expected_checkpoint: BundleCheckpoint,
        previous: ReleaseTrustLease | None,
    ) -> ReleaseTrustLease:
        required = {
            "protocol_version",
            "decision",
            "nonce",
            "binding",
            "checkpoint",
            "lease_id",
            "workload_identity",
            "workload_digest",
            "ledger_sequence",
            "consumption_id",
            "witness_id",
            "witness_sequence",
            "issued_at",
            "expires_at",
            "recovery_expires_at",
        }
        if set(response) != required or response.get("protocol_version") != _PROTOCOL_VERSION:
            raise ReleaseTrustError("invalid release broker response schema")
        if response.get("decision") != "allow":
            raise ReleaseTrustError("release broker denied the workload")
        if response.get("nonce") != nonce:
            raise ReleaseTrustError("release broker nonce mismatch")
        raw_binding = response.get("binding")
        raw_checkpoint = response.get("checkpoint")
        if not isinstance(raw_binding, dict):
            raise ReleaseTrustError("release broker binding mismatch")
        try:
            binding = EmployeeEnvironmentBinding.from_dict(raw_binding)
        except (TypeError, ValueError) as exc:
            raise ReleaseTrustError("release broker binding mismatch") from exc
        if binding != expected_binding:
            raise ReleaseTrustError("release broker binding mismatch")
        if not isinstance(raw_checkpoint, dict) or set(raw_checkpoint) != {
            "record_count",
            "head_hash",
        }:
            raise ReleaseTrustError("release broker checkpoint mismatch")
        try:
            checkpoint = BundleCheckpoint(
                record_count=raw_checkpoint["record_count"],
                head_hash=raw_checkpoint["head_hash"],
            )
        except (TypeError, ValueError) as exc:
            raise ReleaseTrustError("release broker checkpoint mismatch") from exc
        if checkpoint != expected_checkpoint:
            raise ReleaseTrustError("release broker checkpoint mismatch")

        now = _finite_number(self._clock(), "broker clock")
        issued_at = _finite_number(response.get("issued_at"), "issued_at")
        expires_at = _finite_number(response.get("expires_at"), "expires_at")
        recovery_expires_at = _finite_number(
            response.get("recovery_expires_at"),
            "recovery_expires_at",
        )
        if issued_at > now + 5 or expires_at <= now:
            raise ReleaseTrustError("release broker capability is expired")
        if expires_at > now + 900 or recovery_expires_at <= expires_at or recovery_expires_at > now + 86400:
            raise ReleaseTrustError("release broker recovery capability is invalid")
        workload_digest = response.get("workload_digest")
        if not isinstance(workload_digest, str) or not _HASH_RE.fullmatch(workload_digest):
            raise ReleaseTrustError("invalid workload digest")
        lease = ReleaseTrustLease(
            binding=binding,
            checkpoint=checkpoint,
            lease_id=_identifier(response.get("lease_id"), "lease_id"),
            workload_identity=_identifier(
                response.get("workload_identity"),
                "workload_identity",
            ),
            workload_digest=workload_digest,
            ledger_sequence=_positive_int(response.get("ledger_sequence"), "ledger_sequence"),
            consumption_id=_identifier(response.get("consumption_id"), "consumption_id"),
            witness_id=_identifier(response.get("witness_id"), "witness_id"),
            witness_sequence=_positive_int(response.get("witness_sequence"), "witness_sequence"),
            issued_at=issued_at,
            expires_at=expires_at,
            recovery_expires_at=recovery_expires_at,
        )
        if previous is not None and (
            lease.lease_id != previous.lease_id
            or lease.binding != previous.binding
            or lease.checkpoint != previous.checkpoint
            or lease.workload_identity != previous.workload_identity
            or lease.workload_digest != previous.workload_digest
            or lease.ledger_sequence != previous.ledger_sequence
            or lease.consumption_id != previous.consumption_id
            or lease.witness_id != previous.witness_id
            or lease.witness_sequence <= previous.witness_sequence
            or lease.expires_at <= previous.expires_at
            or lease.recovery_expires_at <= previous.recovery_expires_at
        ):
            raise ReleaseTrustError("release broker witness did not advance")
        return lease


__all__ = [
    "ExternalWitnessAnchor",
    "RuntimeReleaseTrustSession",
    "ReleaseTrustError",
    "ReleaseTrustLease",
    "ReleaseTrustProvider",
    "RootOwnedUnixReleaseTrustBroker",
    "authorize_runtime_employee_release",
]
