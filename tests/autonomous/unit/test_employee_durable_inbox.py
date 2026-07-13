from __future__ import annotations

import hashlib
import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

import src.autonomous.ingress.service as ingress_service_module
from src.autonomous.data.keyring import EmployeeDataKeyring
from src.autonomous.ingress.models import EmployeeIngressMetadata, EmployeeIngressPayload
from src.autonomous.ingress.projection import IngressProjectionState
from src.autonomous.ingress.service import (
    EmployeeIngressService,
    IngressConflictError,
    IngressCorrelationError,
)
from src.autonomous.journal.anchor import MemoryAnchor
from src.autonomous.journal.blob_store import AesGcmEncryptionProvider, BlobStore
from src.autonomous.journal.writer import JournalWriter

HMAC_KEY = b"employee-ingress-unit-hmac-key-32b"
DATA_KEY = b"employee-ingress-data-key-32byte"


def _payload(*, text: str = "inspect the change") -> EmployeeIngressPayload:
    return EmployeeIngressPayload(
        schema_version=1,
        envelope_id="ing_" + "1" * 64,
        normalized_parts=({"type": "text", "text": text},),
        attachment_descriptors=(),
    )


def _metadata(payload: EmployeeIngressPayload, **overrides: object) -> EmployeeIngressMetadata:
    values: dict[str, object] = {
        "schema_version": 1,
        "envelope_id": payload.envelope_id,
        "tenant_key": "tenant_1",
        "agent_id": "agt_alpha",
        "bot_principal_id": "bot_alpha",
        "app_id": "cli_alpha",
        "channel_generation": 3,
        "connection_id": "conn_1",
        "event_id": "evt_1",
        "message_id": "om_1",
        "event_type": "im.message.receive_v1",
        "action_identity": "",
        "chat_id": "oc_1",
        "thread_root_message_id": "om_root",
        "sender_principal_id": "ou_requester",
        "received_at": "2026-07-13T00:00:00Z",
        "semantic_digest": payload.payload_sha256,
        "payload_sha256": payload.payload_sha256,
        "payload_size_bytes": payload.canonical_size_bytes,
        "attachment_count": 0,
        "attachment_total_bytes": 0,
    }
    values.update(overrides)
    return EmployeeIngressMetadata(**values)


def _service(tmp_path: Path) -> tuple[EmployeeIngressService, JournalWriter, BlobStore]:
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    store = BlobStore(
        tmp_path / "ingress-blobs",
        AesGcmEncryptionProvider(lambda _key_ref: DATA_KEY),
    )
    service = EmployeeIngressService(
        writer=writer,
        blob_store=store,
        ingress_state=IngressProjectionState(),
        active_key_id="k1",
    )
    return service, writer, store


def test_accept_anchors_safe_metadata_and_keeps_payload_only_in_encrypted_blob(
    tmp_path: Path,
) -> None:
    service, writer, store = _service(tmp_path)
    payload = _payload(text="secret customer request")
    metadata = _metadata(payload)

    ack = service.accept(metadata, payload, request_id="req_1")

    assert ack.duplicate is False
    assert ack.acceptance.journal_sequence == 1
    assert writer.anchor.read().sequence == ack.acceptance.journal_sequence
    assert service.get_payload(ack.acceptance.acceptance_id) == payload
    record = service.state.by_acceptance_id[ack.acceptance.acceptance_id]
    assert record.blob_ref.payload_hash == payload.payload_sha256
    assert dict(record.blob_ref.labels) == {
        "schema": "employee-ingress-v1",
        "tenant": "tenant_1",
        "employee": "agt_alpha",
        "envelope_id": payload.envelope_id,
        "dedup_key": metadata.dedup_key,
        "semantic_digest": metadata.semantic_digest,
    }
    journal_bytes = writer.journal_path.read_bytes()
    assert b"secret customer request" not in journal_bytes
    assert b"normalized_parts" not in journal_bytes
    assert DATA_KEY not in journal_bytes
    service.close()
    writer.close()
    assert store.closed


def test_true_concurrent_redelivery_publishes_one_blob_and_one_acceptance(
    tmp_path: Path,
) -> None:
    service, writer, _store = _service(tmp_path)
    payload = _payload()

    def deliver(index: int):
        metadata = _metadata(
            payload,
            channel_generation=3 + index,
            connection_id=f"conn_{index + 1}",
        )
        return service.accept(metadata, payload, request_id=f"req_{index + 1}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        acknowledgements = list(pool.map(deliver, range(32)))

    assert sum(not ack.duplicate for ack in acknowledgements) == 1
    assert {ack.acceptance for ack in acknowledgements} == {
        acknowledgements[0].acceptance
    }
    assert len(service.state.by_dedup_key) == 1
    assert len(tuple((tmp_path / "ingress-blobs").glob("*.blob"))) == 1
    assert len(tuple(writer.replay())) == 1
    service.close()
    writer.close()


@pytest.mark.parametrize(
    "changed",
    ("semantic_digest", "sender_principal_id", "chat_id", "action_identity"),
)
def test_same_dedup_key_with_changed_semantics_or_provenance_is_conflict(
    tmp_path: Path,
    changed: str,
) -> None:
    service, writer, _store = _service(tmp_path)
    payload = _payload()
    original = _metadata(payload)
    service.accept(original, payload, request_id="req_1")
    values: dict[str, object] = {
        "semantic_digest": "f" * 64,
        "sender_principal_id": "ou_attacker",
        "chat_id": "oc_attacker",
        "action_identity": "corr_attacker",
    }

    with pytest.raises(IngressConflictError):
        service.accept(
            replace(original, **{changed: values[changed]}),
            payload,
            request_id="req_2",
        )

    assert len(tuple(writer.replay())) == 1
    assert len(service.state.by_dedup_key) == 1
    service.close()
    writer.close()


def test_fallback_requires_matching_server_generated_action_correlation(
    tmp_path: Path,
) -> None:
    service, writer, _store = _service(tmp_path)
    payload = _payload()
    metadata = _metadata(payload, event_id="", action_identity="corr_server_1")

    with pytest.raises(IngressCorrelationError):
        service.accept(metadata, payload, request_id="req_1")
    with pytest.raises(IngressCorrelationError):
        service.accept(
            metadata,
            payload,
            request_id="req_2",
            action_correlation="corr_from_user_json",
        )

    ack = service.accept(
        metadata,
        payload,
        request_id="req_3",
        action_correlation="corr_server_1",
    )
    assert ack.duplicate is False
    service.close()
    writer.close()


def test_terminal_disposition_tombstones_payload_before_gc_but_keeps_acceptance(
    tmp_path: Path,
) -> None:
    service, writer, _store = _service(tmp_path)
    payload = _payload()
    ack = service.accept(_metadata(payload), payload, request_id="req_1")

    disposition = service.record_disposition(
        ack.acceptance.acceptance_id,
        state="terminal",
        reason_code="completed",
    )
    assert disposition.state == "terminal"
    assert service.gc_terminal_payloads() == 1

    record = service.state.by_acceptance_id[ack.acceptance.acceptance_id]
    assert record.acceptance == ack.acceptance
    assert record.payload_tombstoned is True
    assert not tuple((tmp_path / "ingress-blobs").glob("*.blob"))
    assert len(tuple((tmp_path / "ingress-blobs" / "quarantine").glob("*.blob"))) == 1
    service.close()
    writer.close()


def test_ingress_owns_only_its_dedicated_store_and_reuses_data_keyring(
    tmp_path: Path,
) -> None:
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
    )
    keyring = EmployeeDataKeyring(keys={"k1": DATA_KEY}, active_key_id="k1")
    data_store = BlobStore(
        tmp_path / "data-blobs",
        AesGcmEncryptionProvider(keyring.resolve),
    )
    service = EmployeeIngressService.from_keyring(
        writer=writer,
        ingress_state=IngressProjectionState(),
        keyring=keyring,
        blob_root=tmp_path / "ingress-blobs",
    )

    with patch.object(
        service.blob_store,
        "close",
        wraps=service.blob_store.close,
    ) as close:
        service.close()
        service.close()

    assert service.blob_store.closed
    assert close.call_count == 1
    assert not data_store.closed
    assert service.blob_store.root != data_store.root
    assert "manager.admission" not in inspect.getsource(ingress_service_module)
    assert "DurableInbox" not in inspect.getsource(ingress_service_module)
    data_store.close()
    writer.close()


def test_payload_metadata_hashes_must_match_before_blob_publish(tmp_path: Path) -> None:
    service, writer, _store = _service(tmp_path)
    payload = _payload()
    metadata = _metadata(payload)

    with pytest.raises(ValueError, match="payload"):
        service.accept(
            replace(metadata, payload_sha256=hashlib.sha256(b"other").hexdigest()),
            payload,
            request_id="req_1",
        )

    assert len(tuple(writer.replay())) == 0
    assert not tuple((tmp_path / "ingress-blobs").glob("*.blob"))
    service.close()
    writer.close()
