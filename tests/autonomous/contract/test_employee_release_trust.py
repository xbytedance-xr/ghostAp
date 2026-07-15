from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.autonomous.acceptance.employee_release import (
    BundleCheckpoint,
    EmployeeEnvironmentBinding,
    EmployeeEvidenceBundle,
    EmployeeEvidenceStatus,
    EmployeeReleaseAttestation,
    EmployeeReleaseManifest,
)
from src.autonomous.acceptance.release_trust import (
    ReleaseTrustError,
    ReleaseTrustLease,
    RootOwnedUnixReleaseTrustBroker,
    authorize_runtime_employee_release,
)


def _binding() -> EmployeeEnvironmentBinding:
    return EmployeeEnvironmentBinding(
        profile_id="employee-release-v1",
        release_id="release-2026-07-14-001",
        commit_sha="a" * 40,
        service_instance_id="ghostap-prod-a",
        staging_tenant_hash="b" * 64,
        production_tenant_hash="c" * 64,
    )


def _attestation() -> EmployeeReleaseAttestation:
    return EmployeeReleaseAttestation(
        checkpoint=BundleCheckpoint(23, "d" * 64),
        binding=_binding(),
        issued_at=1_000_000.0,
        key_id="independent-qa-2026",
        signature="c2lnbmF0dXJl",
    )


def _response(request: dict[str, object], *, now: float = 1_000_001.0) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "decision": "allow",
        "nonce": request["nonce"],
        "binding": _binding().to_dict(),
        "checkpoint": {"record_count": 23, "head_hash": "d" * 64},
        "lease_id": "lease-release-2026-07-14-001",
        "workload_identity": "kubernetes://prod/ghostap/pod-123",
        "workload_digest": "e" * 64,
        "ledger_sequence": 41,
        "consumption_id": "consume-release-2026-07-14-001",
        "witness_id": "witness-prod-1",
        "witness_sequence": 9001,
        "issued_at": now,
        "expires_at": now + 300,
        "recovery_expires_at": now + 3600,
    }


class _BrokerServer:
    def __init__(self, path: Path, responder) -> None:
        self.path = path
        self.responder = responder
        self.request: dict[str, object] | None = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        assert self.ready.wait(2)
        return self

    def __exit__(self, *_args):
        self.thread.join(timeout=2)

    def _run(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        os.chmod(self.path, 0o600)
        server.listen(1)
        self.ready.set()
        try:
            connection, _ = server.accept()
            with connection:
                payload = b""
                while not payload.endswith(b"\n"):
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    payload += chunk
                self.request = json.loads(payload)
                response = self.responder(self.request)
                connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        finally:
            server.close()


def test_root_owned_broker_consumes_nonce_bound_release_attestation(tmp_path: Path) -> None:
    path = tmp_path / "release.sock"
    now = 1_000_001.0
    with _BrokerServer(path, lambda request: _response(request, now=now)) as server:
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now,
        )
        lease = broker.consume(_attestation())

    assert server.request is not None
    assert server.request["operation"] == "consume_release_attestation"
    assert server.request["pid"] == os.getpid()
    assert server.request["attestation"]["binding"] == _binding().to_dict()
    assert lease.binding == _binding()
    assert lease.checkpoint == BundleCheckpoint(23, "d" * 64)
    assert lease.ledger_sequence == 41
    assert lease.valid_at(now)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda response: {**response, "nonce": "replayed"}, "nonce"),
        (
            lambda response: {**response, "binding": replace(_binding(), release_id="other-release").to_dict()},
            "binding",
        ),
        (
            lambda response: {**response, "checkpoint": {"record_count": 22, "head_hash": "d" * 64}},
            "checkpoint",
        ),
        (lambda response: {**response, "expires_at": 1_000_000.0}, "expired"),
        (lambda response: {**response, "ledger_sequence": 0}, "ledger"),
    ],
)
def test_broker_rejects_replay_mismatch_and_invalid_capability(
    tmp_path: Path,
    mutation,
    match: str,
) -> None:
    path = tmp_path / "release.sock"
    now = 1_000_001.0
    with _BrokerServer(path, lambda request: mutation(_response(request, now=now))):
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now,
        )
        with pytest.raises(ReleaseTrustError, match=match):
            broker.consume(_attestation())


def test_broker_rejects_group_writable_socket_before_connect(tmp_path: Path) -> None:
    path = tmp_path / "release.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o620)
    try:
        broker = RootOwnedUnixReleaseTrustBroker(path, expected_peer_uid=os.getuid())
        with pytest.raises(ReleaseTrustError, match="writable"):
            broker.consume(_attestation())
    finally:
        server.close()


def test_recovery_renewal_must_advance_external_witness(tmp_path: Path) -> None:
    path = tmp_path / "release.sock"
    now = time.time()
    first: object
    with _BrokerServer(path, lambda request: _response(request, now=now)):
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now,
        )
        first = broker.consume(_attestation())

    path.unlink()
    with _BrokerServer(
        path,
        lambda request: {
            **_response(request, now=now + 200),
            "lease_id": first.lease_id,
            "ledger_sequence": first.ledger_sequence,
            "consumption_id": first.consumption_id,
            "witness_sequence": first.witness_sequence,
        },
    ):
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now + 200,
        )
        with pytest.raises(ReleaseTrustError, match="witness"):
            broker.renew(first)


class _FakeProvider:
    def __init__(self, lease: ReleaseTrustLease) -> None:
        self.lease = lease
        self.consumed = 0
        self.renewed = 0
        self.closed = False

    def consume(self, attestation: EmployeeReleaseAttestation) -> ReleaseTrustLease:
        self.consumed += 1
        assert attestation.binding == self.lease.binding
        return self.lease

    def renew(self, lease: ReleaseTrustLease) -> ReleaseTrustLease:
        self.renewed += 1
        return replace(
            lease,
            witness_sequence=lease.witness_sequence + 1,
            expires_at=lease.expires_at + 300,
            recovery_expires_at=lease.recovery_expires_at + 300,
        )

    def close(self) -> None:
        self.closed = True


def _runtime_settings(tmp_path: Path, *, captured_at: float) -> SimpleNamespace:
    binding = _binding()
    manifest = EmployeeReleaseManifest.load(
        "src/autonomous/acceptance/employee_release_manifest.json"
    )
    bundle_path = tmp_path / "evidence.jsonl"
    bundle = EmployeeEvidenceBundle(bundle_path)
    checkpoint = BundleCheckpoint.empty()
    for gate in manifest.gates:
        details: dict[str, object] = {
            "assertions": {name: True for name in gate.required_assertions}
        }
        if gate.minimum_bot_count:
            details["bot_count"] = gate.minimum_bot_count
        if gate.minimum_duration_seconds:
            details["duration_seconds"] = gate.minimum_duration_seconds
        for metric in gate.required_zero_metrics:
            details[metric] = 0
        for metric, maximum, exclusive in gate.maximum_metrics:
            details[metric] = maximum - 0.1 if exclusive else maximum
        checkpoint = bundle.append(
            gate_id=gate.gate_id,
            environment=gate.environment,
            tenant_hash=binding.tenant_hash_for(gate.environment),
            status=EmployeeEvidenceStatus.PASSED,
            details=details,
            binding=binding,
            captured_at=captured_at,
            attestor="tenant-qa@example.invalid",
        )
    attestation = replace(_attestation(), checkpoint=checkpoint, issued_at=captured_at)
    checkpoint_path = tmp_path / "attestation.json"
    checkpoint_path.write_text(
        json.dumps({**attestation.unsigned_dict(), "signature": attestation.signature}),
        encoding="utf-8",
    )
    return SimpleNamespace(
        autonomous_employee_release_evidence_bundle=str(bundle_path),
        autonomous_employee_release_checkpoint=str(checkpoint_path),
        autonomous_employee_release_id=binding.release_id,
        autonomous_employee_commit_sha=binding.commit_sha,
        autonomous_employee_service_instance_id=binding.service_instance_id,
        autonomous_employee_staging_tenant_hash=binding.staging_tenant_hash,
        autonomous_employee_production_tenant_hash=binding.production_tenant_hash,
    )


def _lease(checkpoint: BundleCheckpoint, *, now: float) -> ReleaseTrustLease:
    return ReleaseTrustLease(
        binding=_binding(),
        checkpoint=checkpoint,
        lease_id="lease-release-2026-07-14-001",
        workload_identity="kubernetes://prod/ghostap/pod-123",
        workload_digest="e" * 64,
        ledger_sequence=41,
        consumption_id="consume-release-2026-07-14-001",
        witness_id="witness-prod-1",
        witness_sequence=9001,
        issued_at=now,
        expires_at=now + 300,
        recovery_expires_at=now + 3600,
    )


def test_runtime_release_requires_complete_local_bundle_before_external_consumption(
    tmp_path: Path,
) -> None:
    now = 1_000_001.0
    settings = _runtime_settings(tmp_path, captured_at=now)
    attestation = EmployeeReleaseAttestation.load(settings.autonomous_employee_release_checkpoint)
    provider = _FakeProvider(_lease(attestation.checkpoint, now=now))

    session = authorize_runtime_employee_release(
        settings,
        provider,
        now=now,
    )

    assert provider.consumed == 1
    assert session.valid(now)
    session.close()
    assert provider.closed


def test_runtime_release_does_not_call_broker_for_checkpoint_mismatch(tmp_path: Path) -> None:
    now = 1_000_001.0
    settings = _runtime_settings(tmp_path, captured_at=now)
    attestation = EmployeeReleaseAttestation.load(settings.autonomous_employee_release_checkpoint)
    tampered = replace(
        attestation,
        checkpoint=replace(attestation.checkpoint, record_count=attestation.checkpoint.record_count - 1),
    )
    Path(settings.autonomous_employee_release_checkpoint).write_text(
        json.dumps({**tampered.unsigned_dict(), "signature": tampered.signature}),
        encoding="utf-8",
    )
    provider = _FakeProvider(_lease(attestation.checkpoint, now=now))

    with pytest.raises(ReleaseTrustError, match="evidence"):
        authorize_runtime_employee_release(settings, provider, now=now)

    assert provider.consumed == 0


def test_runtime_release_session_renews_before_expiry_and_rejects_bad_lineage(
    tmp_path: Path,
) -> None:
    now = 1_000_001.0
    settings = _runtime_settings(tmp_path, captured_at=now)
    attestation = EmployeeReleaseAttestation.load(settings.autonomous_employee_release_checkpoint)
    provider = _FakeProvider(_lease(attestation.checkpoint, now=now))
    session = authorize_runtime_employee_release(settings, provider, now=now)

    assert session.renew_if_needed(now + 200, renewal_window_seconds=120)
    assert provider.renewed == 1

    provider.renew = lambda lease: replace(lease, consumption_id="consume-forged")
    with pytest.raises(ReleaseTrustError, match="lineage"):
        session.renew_if_needed(now + 500, renewal_window_seconds=120)


def test_external_journal_anchor_requires_nonce_bound_advancing_witness(
    tmp_path: Path,
) -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)
    path = tmp_path / "release.sock"

    def read_response(request: dict[str, object]) -> dict[str, object]:
        return {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "anchor_scope": "employee-journal:ghostap-prod-a",
            "state": {"sequence": 0, "frame_hash": "0" * 64},
            "witness_sequence": lease.witness_sequence + 1,
        }

    with _BrokerServer(path, read_response):
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now,
        )
        from src.autonomous.acceptance.release_trust import RuntimeReleaseTrustSession

        session = RuntimeReleaseTrustSession(broker, lease)
        anchor = session.journal_anchor("employee-journal:ghostap-prod-a")
        assert anchor.read().sequence == 0

    path.unlink()

    def cas_response(request: dict[str, object]) -> dict[str, object]:
        return {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "anchor_scope": "employee-journal:ghostap-prod-a",
            "state": {"sequence": 1, "frame_hash": "f" * 64},
            "witness_sequence": lease.witness_sequence + 2,
            "swapped": True,
        }

    with _BrokerServer(path, cas_response):
        assert anchor.compare_and_swap(0, "0" * 64, 1, "f" * 64) is True


def test_external_journal_anchor_rejects_stale_witness(tmp_path: Path) -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)
    path = tmp_path / "release.sock"
    with _BrokerServer(
        path,
        lambda request: {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "anchor_scope": "employee-journal:ghostap-prod-a",
            "state": {"sequence": 0, "frame_hash": "0" * 64},
            "witness_sequence": lease.witness_sequence,
        },
    ):
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now,
        )
        from src.autonomous.acceptance.release_trust import RuntimeReleaseTrustSession

        anchor = RuntimeReleaseTrustSession(broker, lease).journal_anchor(
            "employee-journal:ghostap-prod-a"
        )
        with pytest.raises(ReleaseTrustError, match="witness"):
            anchor.read()


def test_external_anchor_witness_is_monotonic_across_scopes() -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)

    class Provider:
        def read_anchor(self, _lease, _scope):
            from src.autonomous.journal.anchor import AnchorState

            return AnchorState(), lease.witness_sequence + 1

        def compare_and_swap_anchor(self, *_args):
            raise AssertionError("not used")

        def close(self):
            pass

    from src.autonomous.acceptance.release_trust import RuntimeReleaseTrustSession

    session = RuntimeReleaseTrustSession(Provider(), lease)
    first = session.journal_anchor("employee-journal:ghostap-prod-a")
    second = session.journal_anchor("main-bot-audit:ghostap-prod-a")

    assert first.read().sequence == 0
    with pytest.raises(ReleaseTrustError, match="witness"):
        second.read()


def test_broker_main_bot_audit_protocol_is_hash_only_and_complete(tmp_path: Path) -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)
    path = tmp_path / "release.sock"

    def record_response(request: dict[str, object]) -> dict[str, object]:
        assert request["tenant_hash"] == "a" * 64
        assert request["target_hash"] == "b" * 64
        assert "tenant_key" not in request
        return {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "audit_sequence": 7,
            "witness_sequence": lease.witness_sequence + 1,
        }

    with _BrokerServer(path, record_response):
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now,
        )
        assert broker.record_main_bot_send_attempt(
            lease,
            attempt_id="attempt-001",
            tenant_hash="a" * 64,
            operation="reply",
            target_hash="b" * 64,
            attempted_at=now,
        ) == (7, lease.witness_sequence + 1)

    path.unlink()

    def count_response(request: dict[str, object]) -> dict[str, object]:
        return {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "audit_sequence": 7,
            "witness_sequence": lease.witness_sequence + 2,
            "complete": True,
            "count": 3,
        }

    with _BrokerServer(path, count_response):
        assert broker.count_main_bot_send_attempts(
            lease,
            tenant_hash="a" * 64,
            start=now - 1,
            end=now + 1,
        ) == (3, 7, lease.witness_sequence + 2)

    path.unlink()

    def target_count_response(request: dict[str, object]) -> dict[str, object]:
        assert request["operation"] == "count_main_bot_target_send_attempts"
        assert request["tenant_hash"] == "a" * 64
        assert request["target_hash"] == "b" * 64
        return {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "audit_sequence": 7,
            "witness_sequence": lease.witness_sequence + 3,
            "complete": True,
            "count": 1,
        }

    with _BrokerServer(path, target_count_response):
        assert broker.count_main_bot_target_send_attempts(
            lease,
            tenant_hash="a" * 64,
            target_hash="b" * 64,
            start=now - 1,
            end=now + 1,
        ) == (1, 7, lease.witness_sequence + 3)


def test_runtime_release_session_supports_target_scoped_main_bot_audit() -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)

    class Provider:
        def count_main_bot_target_send_attempts(self, _lease, **kwargs):
            assert kwargs == {
                "tenant_hash": hashlib.sha256(b"tenant-a").hexdigest(),
                "target_hash": "b" * 64,
                "start": now - 1,
                "end": now + 1,
            }
            return 0, 7, lease.witness_sequence + 1

        def close(self):
            pass

    from src.autonomous.acceptance.release_trust import RuntimeReleaseTrustSession

    session = RuntimeReleaseTrustSession(Provider(), lease)
    assert (
        session.count_main_bot_target_send_attempts(
            "tenant-a",
            "b" * 64,
            now - 1,
            now + 1,
        )
        == 0
    )


def test_runtime_release_session_acquires_and_releases_bound_activation_fence() -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)
    target_hashes = ("a" * 64, "b" * 64)
    calls: list[tuple[str, dict[str, object]]] = []

    class Provider:
        def acquire_main_bot_activation_fence(self, _lease, **kwargs):
            calls.append(("acquire", kwargs))
            return "fence-001", 7, lease.witness_sequence + 1

        def release_main_bot_activation_fence(self, _lease, **kwargs):
            calls.append(("release", kwargs))
            return 7, lease.witness_sequence + 2

        def close(self):
            pass

    from src.autonomous.acceptance.release_trust import RuntimeReleaseTrustSession

    session = RuntimeReleaseTrustSession(Provider(), lease)
    fence_id = session.acquire_main_bot_activation_fence(
        "tenant-a",
        target_hashes,
    )
    session.release_main_bot_activation_fence(
        "tenant-a",
        target_hashes,
        fence_id=fence_id,
    )

    tenant_hash = hashlib.sha256(b"tenant-a").hexdigest()
    assert calls == [
        (
            "acquire",
            {
                "tenant_hash": tenant_hash,
                "target_hashes": target_hashes,
                "witness_sequence": lease.witness_sequence,
            },
        ),
        (
            "release",
            {
                "tenant_hash": tenant_hash,
                "target_hashes": target_hashes,
                "fence_id": "fence-001",
                "witness_sequence": lease.witness_sequence + 1,
            },
        ),
    ]


def test_runtime_release_session_rejects_missing_or_mismatched_fence_provider() -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)

    class MissingProvider:
        def close(self):
            pass

    from src.autonomous.acceptance.release_trust import RuntimeReleaseTrustSession

    missing = RuntimeReleaseTrustSession(MissingProvider(), lease)
    assert missing.main_bot_activation_fence_ready is False
    with pytest.raises(ReleaseTrustError, match="activation fence"):
        missing.acquire_main_bot_activation_fence("tenant-a", ("a" * 64,))

    class MismatchedProvider(MissingProvider):
        def acquire_main_bot_activation_fence(self, _lease, **_kwargs):
            return "fence-001", 7, lease.witness_sequence

        def release_main_bot_activation_fence(self, _lease, **_kwargs):
            return 7, lease.witness_sequence + 1

    mismatched = RuntimeReleaseTrustSession(MismatchedProvider(), lease)
    with pytest.raises(ReleaseTrustError, match="witness"):
        mismatched.acquire_main_bot_activation_fence("tenant-a", ("a" * 64,))


def test_broker_activation_fence_protocol_binds_lease_tenant_targets_and_nonce(
    tmp_path: Path,
) -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)
    tenant_hash = "a" * 64
    target_hashes = ("b" * 64, "c" * 64)
    path = tmp_path / "release.sock"

    def acquire_response(request: dict[str, object]) -> dict[str, object]:
        assert request["operation"] == "acquire_main_bot_activation_fence"
        assert request["lease_id"] == lease.lease_id
        assert request["tenant_hash"] == tenant_hash
        assert request["target_hashes"] == list(target_hashes)
        assert request["witness_sequence"] == lease.witness_sequence
        return {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "tenant_hash": tenant_hash,
            "target_hashes": list(target_hashes),
            "fence_id": "fence-001",
            "audit_sequence": 7,
            "witness_sequence": lease.witness_sequence + 1,
        }

    with _BrokerServer(path, acquire_response):
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now,
        )
        assert broker.acquire_main_bot_activation_fence(
            lease,
            tenant_hash=tenant_hash,
            target_hashes=target_hashes,
            witness_sequence=lease.witness_sequence,
        ) == ("fence-001", 7, lease.witness_sequence + 1)

    path.unlink()

    def release_response(request: dict[str, object]) -> dict[str, object]:
        assert request["operation"] == "release_main_bot_activation_fence"
        assert request["fence_id"] == "fence-001"
        assert request["witness_sequence"] == lease.witness_sequence + 1
        return {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "tenant_hash": tenant_hash,
            "target_hashes": list(target_hashes),
            "fence_id": "fence-001",
            "audit_sequence": 7,
            "witness_sequence": lease.witness_sequence + 2,
        }

    with _BrokerServer(path, release_response):
        assert broker.release_main_bot_activation_fence(
            lease,
            tenant_hash=tenant_hash,
            target_hashes=target_hashes,
            fence_id="fence-001",
            witness_sequence=lease.witness_sequence + 1,
        ) == (7, lease.witness_sequence + 2)


def test_broker_activation_fence_rejects_response_target_mismatch(tmp_path: Path) -> None:
    now = time.time()
    lease = _lease(BundleCheckpoint(23, "d" * 64), now=now)
    path = tmp_path / "release.sock"
    with _BrokerServer(
        path,
        lambda request: {
            "protocol_version": 1,
            "decision": "allow",
            "nonce": request["nonce"],
            "lease_id": lease.lease_id,
            "tenant_hash": "a" * 64,
            "target_hashes": ["c" * 64],
            "fence_id": "fence-001",
            "audit_sequence": 7,
            "witness_sequence": lease.witness_sequence + 1,
        },
    ):
        broker = RootOwnedUnixReleaseTrustBroker(
            path,
            expected_peer_uid=os.getuid(),
            clock=lambda: now,
        )
        with pytest.raises(ReleaseTrustError, match="binding"):
            broker.acquire_main_bot_activation_fence(
                lease,
                tenant_hash="a" * 64,
                target_hashes=("b" * 64,),
                witness_sequence=lease.witness_sequence,
            )
