from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.autonomous.ingress.models import EmployeeIngressPayload
from src.autonomous.ingress.projection import IngressProjectionState
from src.autonomous.ingress.service import EmployeeIngressService
from src.autonomous.journal.anchor import FileAnchor, MemoryAnchor
from src.autonomous.journal.blob_store import AesGcmEncryptionProvider, BlobStore
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.journal.writer import JournalWriter

HMAC_KEY = b"employee-attachment-staging-hmac-key"
PNG = b"\x89PNG\r\n\x1a\n" + b"safe-image-content"
PDF = b"%PDF-1.7\n% safe document\n"
ELF = b"\x7fELF" + b"\x00" * 32


@pytest.fixture
def api():
    assert importlib.util.find_spec("src.autonomous.ingress.attachments") is not None, (
        "employee attachment staging module is missing"
    )
    return importlib.import_module("src.autonomous.ingress.attachments")


class _Vault:
    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets
        self.calls: list[tuple[str, str, str]] = []

    def resolve(self, credential_ref: str, agent_id: str, app_id: str) -> str:
        self.calls.append((credential_ref, agent_id, app_id))
        return self.secrets[credential_ref]


class _Downloader:
    def __init__(self, resources: dict[str, object]) -> None:
        self.resources = resources
        self.calls: list[object] = []

    def download(self, descriptor):
        self.calls.append(descriptor)
        resource = self.resources[descriptor.resource_id]
        if callable(resource):
            return resource()
        return resource


class _Builder:
    def __init__(self, downloader: _Downloader) -> None:
        self.downloader = downloader
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, *, app_id: str, app_secret: str, timeout: float):
        self.calls.append((app_id, app_secret, timeout))
        return self.downloader


def _descriptor(api, content: bytes = PNG, **overrides: object):
    values: dict[str, object] = {
        "schema_version": 1,
        "message_id": "om_message_1",
        "resource_type": "image",
        "resource_id": "img_v2_resource_1",
        "declared_mime_type": "image/png",
        "declared_size_bytes": len(content),
        "declared_sha256": hashlib.sha256(content).hexdigest(),
        "user_filename": "diagram.png",
    }
    if overrides.get("resource_type") == "file" and "resource_id" not in overrides:
        values["resource_id"] = "file_v2_resource_1"
    values.update(overrides)
    return api.EmployeeAttachmentDescriptor(**values)


def _request(api, descriptors: tuple[object, ...], **overrides: object):
    values: dict[str, object] = {
        "schema_version": 1,
        "acceptance_id": "acc_" + "a" * 64,
        "envelope_id": "ing_" + "b" * 64,
        "tenant_key": "tenant/customer:one",
        "agent_id": "agt_alpha",
        "app_id": "cli_alpha",
        "credential_ref": "cred_employee_alpha",
        "descriptors": descriptors,
    }
    values.update(overrides)
    return api.AuthorizedAttachmentStagingRequest(**values)


def _service(
    api,
    tmp_path: Path,
    resources: dict[str, object],
    *,
    policy=None,
    timeout: float = 0.2,
    writer: JournalWriter | None = None,
    vault: _Vault | None = None,
    builder: _Builder | None = None,
    fault_hook=None,
    name_factory=None,
):
    writer = writer or JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
    )
    vault = vault or _Vault({"cred_employee_alpha": "employee-secret"})
    downloader = _Downloader(resources)
    builder = builder or _Builder(downloader)
    service = api.AttachmentStagingService(
        writer=writer,
        root=tmp_path / "staging",
        credential_resolver=vault,
        downloader_builder=builder,
        policy=policy or api.AttachmentPolicy(),
        download_timeout_seconds=timeout,
        fault_hook=fault_hook,
        name_factory=name_factory,
    )
    return service, writer, vault, builder, downloader


def _staging_reducer_events(api):
    staging_id = "stg_illegal_order"
    aggregate_id = "astg_" + hashlib.sha256(staging_id.encode()).hexdigest()
    base = (
        api.tenant_storage_component("tenant/customer:one"),
        "agt_alpha",
        "ing_" + "b" * 64,
    )
    start = JournalEvent(
        event_type="employee.ingress.attachment_staging_started",
        aggregate_id=aggregate_id,
        payload={
            "staging_id": staging_id,
            "acceptance_id": "acc_" + "a" * 64,
            "envelope_id": "ing_" + "b" * 64,
            "tenant_key": "tenant/customer:one",
            "agent_id": "agt_alpha",
            "app_id": "cli_alpha",
            "relative_paths": ["/".join((*base, "att_one.bin"))],
            "temporary_paths": ["/".join((*base, ".att_one.tmp"))],
            "content_hashes": [hashlib.sha256(PNG).hexdigest()],
        },
    )

    def event(event_type: str, **payload: object) -> JournalEvent:
        return JournalEvent(
            event_type=event_type,
            aggregate_id=aggregate_id,
            payload={"staging_id": staging_id, **payload},
        )

    return {
        "start": start,
        "parent": event(
            "employee.ingress.attachment_staging_parent_bound",
            parent_device=1,
            parent_inode=2,
        ),
        "leaf": event(
            "employee.ingress.attachment_staging_leaf_prepared",
            index=0,
            leaf_device=1,
            leaf_inode=3,
        ),
        "completed": event("employee.ingress.attachment_staging_completed"),
        "failed": event(
            "employee.ingress.attachment_staging_failed",
            reason="validation",
        ),
        "cleanup_started": event("employee.ingress.attachment_cleanup_started"),
        "cleanup_leaf_started": event(
            "employee.ingress.attachment_cleanup_leaf_started",
            index=0,
            target_kind="identity",
            target_device=1,
            target_inode=3,
        ),
        "cleanup_leaf_completed": event(
            "employee.ingress.attachment_cleanup_leaf_completed",
            index=0,
        ),
        "cleanup_completed": event("employee.ingress.attachment_cleanup_completed"),
    }


def test_ack_contract_rejects_url_and_local_path_resource_descriptors() -> None:
    base = {
        "resource_type": "file",
        "mime_type": "text/plain",
        "size_bytes": 4,
        "sha256": hashlib.sha256(b"safe").hexdigest(),
    }
    for resource_id in (
        "https://attacker.invalid/payload",
        "/etc/passwd",
        "../../outside",
        r"C:\\Windows\\system.ini",
        "file://local/path",
    ):
        with pytest.raises(ValueError, match="resource_id"):
            EmployeeIngressPayload(
                schema_version=1,
                envelope_id="ing_" + "1" * 64,
                normalized_parts=(),
                attachment_descriptors=({**base, "resource_id": resource_id},),
            )


def test_ack_path_only_encrypts_descriptor_and_never_stages_or_downloads(api, tmp_path) -> None:
    content = b"safe"
    payload = EmployeeIngressPayload(
        schema_version=1,
        envelope_id="ing_" + "1" * 64,
        normalized_parts=(),
        attachment_descriptors=(
            {
                "resource_type": "file",
                "resource_id": "file_v2_resource_1",
                "mime_type": "text/plain",
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        ),
    )
    from src.autonomous.ingress.models import EmployeeIngressMetadata

    metadata = EmployeeIngressMetadata(
        schema_version=1,
        envelope_id=payload.envelope_id,
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        bot_principal_id="bot_alpha",
        app_id="cli_alpha",
        channel_generation=1,
        connection_id="conn_1",
        event_id="evt_1",
        message_id="om_message_1",
        event_type="im.message.receive_v1",
        action_identity="",
        chat_id="oc_1",
        thread_root_message_id="om_root",
        sender_principal_id="ou_requester",
        received_at="2026-07-13T00:00:00Z",
        semantic_digest=payload.payload_sha256,
        payload_sha256=payload.payload_sha256,
        payload_size_bytes=payload.canonical_size_bytes,
        attachment_count=1,
        attachment_total_bytes=len(content),
    )
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
    )
    blob_store = BlobStore(
        tmp_path / "blobs",
        AesGcmEncryptionProvider(lambda _ref: b"employee-ingress-data-key-32byte"),
    )
    ingress = EmployeeIngressService(
        writer=writer,
        blob_store=blob_store,
        ingress_state=IngressProjectionState(),
        active_key_id="k1",
    )
    with patch.object(
        api.AttachmentStagingService,
        "stage",
        side_effect=AssertionError("ACK path must not enter staging"),
    ) as stage:
        ack = ingress.accept(metadata, payload, request_id="req_1")

    assert ack.duplicate is False
    stage.assert_not_called()
    assert ingress.get_payload(ack.acceptance.acceptance_id) == payload
    ingress.close()
    writer.close()


def test_official_sdk_downloader_uses_only_typed_message_resource_coordinates(api) -> None:
    captured = []

    class ResourceAPI:
        def get(self, request):
            captured.append(request)
            return SimpleNamespace(
                success=lambda: True,
                file=SimpleNamespace(read=lambda maximum: PNG[:maximum]),
                file_name="remote.png",
            )

    client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message_resource=ResourceAPI())))
    descriptor = _descriptor(api)
    result = api.LarkEmployeeAttachmentDownloader(client).download(descriptor)

    assert result == api.DownloadedAttachment(content=PNG, file_name="remote.png")
    assert len(captured) == 1
    assert captured[0].message_id == "om_message_1"
    assert captured[0].file_key == "img_v2_resource_1"
    assert captured[0].type == "image"


@pytest.mark.parametrize(
    "sequence",
    [
        ("cleanup_started",),
        ("cleanup_started", "cleanup_completed", "parent"),
        ("parent", "cleanup_started", "completed"),
        ("cleanup_started", "failed"),
        ("leaf",),
        ("parent", "parent"),
        ("parent", "leaf", "leaf"),
        ("parent", "failed", "leaf"),
        ("parent", "leaf", "completed", "failed"),
        ("parent", "leaf", "completed", "cleanup_started", "cleanup_started"),
        ("parent", "leaf", "completed", "cleanup_completed"),
        ("failed", "cleanup_started", "cleanup_leaf_completed"),
        ("failed", "cleanup_started", "cleanup_leaf_started", "cleanup_leaf_started"),
    ],
)
def test_reducer_rejects_cleanup_before_terminal_and_duplicate_late_transitions(
    api,
    sequence,
) -> None:
    events = _staging_reducer_events(api)
    state = api.AttachmentStagingState()
    api._apply_event(state, events["start"])

    with pytest.raises(api.AttachmentStateError):
        for name in sequence:
            api._apply_event(state, events[name])


@pytest.mark.parametrize(
    "sequence",
    [
        (
            "failed",
            "cleanup_started",
            "cleanup_leaf_started",
            "cleanup_leaf_completed",
            "cleanup_completed",
        ),
        (
            "parent",
            "failed",
            "cleanup_started",
            "cleanup_leaf_started",
            "cleanup_leaf_completed",
            "cleanup_completed",
        ),
        (
            "parent",
            "leaf",
            "completed",
            "cleanup_started",
            "cleanup_leaf_started",
            "cleanup_leaf_completed",
            "cleanup_completed",
        ),
    ],
)
def test_reducer_accepts_only_terminal_then_cleanup_sequences(api, sequence) -> None:
    events = _staging_reducer_events(api)
    state = api.AttachmentStagingState()
    api._apply_event(state, events["start"])

    for name in sequence:
        api._apply_event(state, events[name])

    record = next(iter(state.by_staging_id.values()))
    assert record.status in {"failed", "completed"}
    assert record.cleanup_state == "completed"


def test_replay_rejects_cleanup_then_late_parent_and_completion(api, tmp_path) -> None:
    events = _staging_reducer_events(api)
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
    )
    aggregate_id = events["start"].aggregate_id
    for name in (
        "start",
        "cleanup_started",
        "cleanup_completed",
        "parent",
        "completed",
    ):
        writer.commit(
            [events[name]],
            writer.get_aggregate_versions([aggregate_id]),
        )

    def replay() -> None:
        service = api.AttachmentStagingService(
            writer=writer,
            root=tmp_path / "staging",
            credential_resolver=_Vault({}),
            downloader_builder=lambda **_: None,
        )
        service.close()

    with pytest.raises(api.AttachmentStateError):
        replay()
    writer.close()


def test_two_instances_reject_late_parent_without_anchoring_invalid_event(
    api,
    tmp_path,
) -> None:
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
    )
    stale, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=writer,
    )
    active, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
    )
    completed = active.stage(_request(api, (_descriptor(api),)))
    active.cleanup(completed.staging_id)
    current = active.state.by_staging_id[completed.staging_id]
    anchor_before = writer.anchor.read()
    frames_before = len(tuple(writer.replay()))
    late_parent = JournalEvent(
        event_type="employee.ingress.attachment_staging_parent_bound",
        aggregate_id=current.aggregate_id,
        payload={
            "staging_id": current.staging_id,
            "parent_device": current.parent_device,
            "parent_inode": current.parent_inode,
        },
    )

    with pytest.raises(api.AttachmentStateError):
        stale._commit_unlocked(current.aggregate_id, late_parent)

    assert writer.anchor.read() == anchor_before
    assert len(tuple(writer.replay())) == frames_before
    stale._rebuild_unlocked()
    assert stale.state.by_staging_id[current.staging_id].cleanup_state == "completed"
    stale.close()
    active.close()
    writer.close()


def test_two_instances_reject_late_completion_after_failed_cleanup_without_commit(
    api,
    tmp_path,
) -> None:
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
    )
    stale, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=writer,
    )
    active, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            )
        },
        writer=writer,
    )
    invalid = _descriptor(api, declared_sha256="0" * 64)
    with pytest.raises(api.AttachmentValidationError):
        active.stage(_request(api, (invalid,)))
    current = next(iter(active.state.by_staging_id.values()))
    anchor_before = writer.anchor.read()
    frames_before = len(tuple(writer.replay()))
    late_completed = JournalEvent(
        event_type="employee.ingress.attachment_staging_completed",
        aggregate_id=current.aggregate_id,
        payload={"staging_id": current.staging_id},
    )

    with pytest.raises(api.AttachmentStateError):
        stale._commit_unlocked(current.aggregate_id, late_completed)

    assert writer.anchor.read() == anchor_before
    assert len(tuple(writer.replay())) == frames_before
    stale._rebuild_unlocked()
    replayed = stale.state.by_staging_id[current.staging_id]
    assert replayed.status == "failed"
    assert replayed.cleanup_state == "completed"
    stale.close()
    active.close()
    writer.close()


def test_descriptor_rejects_untyped_coordinates_and_payload_storage_identity(api) -> None:
    with pytest.raises(ValueError, match="message_id"):
        _descriptor(api, message_id="https://open.feishu.cn/message")
    with pytest.raises(ValueError, match="resource_type"):
        _descriptor(api, resource_type="url")
    with pytest.raises(ValueError, match="resource_id"):
        _descriptor(api, resource_type="image", resource_id="file_v2_wrong_space")
    with pytest.raises(ValueError, match="resource_id"):
        _descriptor(api, resource_type="file", resource_id="img_v2_wrong_space")
    with pytest.raises(TypeError):
        api.EmployeeAttachmentDescriptor.from_dict(
            {
                **_descriptor(api).to_dict(),
                "attempt_id": "attacker-controlled",
            }
        )
    with pytest.raises(TypeError):
        api.EmployeeAttachmentDescriptor.from_dict(
            {
                **_descriptor(api).to_dict(),
                "envelope_id": "ing_" + "f" * 64,
            }
        )


def test_policy_rejects_count_per_file_and_total_size_before_credentials(api, tmp_path) -> None:
    policy = api.AttachmentPolicy(
        max_count=2,
        max_file_bytes=8,
        max_total_bytes=12,
    )
    service, writer, vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        policy=policy,
    )
    one = _descriptor(api, b"12345678", declared_mime_type="text/plain", resource_type="file")
    two = _descriptor(
        api,
        b"abcdefgh",
        resource_id="file_v2_resource_2",
        declared_mime_type="text/plain",
        resource_type="file",
    )
    three = _descriptor(api, b"x", resource_id="img_v2_resource_3")

    with pytest.raises(api.AttachmentPolicyError, match="count"):
        service.stage(_request(api, (one, two, three)))
    with pytest.raises(api.AttachmentPolicyError, match="per-file"):
        service.stage(
            _request(
                api,
                (_descriptor(api, b"123456789", declared_mime_type="text/plain", resource_type="file"),),
            )
        )
    with pytest.raises(api.AttachmentPolicyError, match="total"):
        service.stage(_request(api, (one, two)))

    assert vault.calls == []
    assert tuple(writer.replay()) == ()
    service.close()
    writer.close()


def test_download_timeout_fails_terminally_and_cleans_server_paths(api, tmp_path) -> None:
    def slow_resource():
        time.sleep(0.2)
        return api.DownloadedAttachment(content=PNG, file_name="late.png")

    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": slow_resource},
        timeout=0.03,
    )
    started = time.monotonic()
    with pytest.raises(api.AttachmentTimeoutError):
        service.stage(_request(api, (_descriptor(api),)))
    assert time.monotonic() - started < 0.15
    record = next(iter(service.state.by_staging_id.values()))
    assert record.status == "failed"
    assert record.cleanup_state == "completed"
    assert service.trusted_paths(record.staging_id) == ()
    assert list((tmp_path / "staging").rglob("*.bin")) == []
    assert [frame.events[0].event_type for frame in writer.replay()] == [
        "employee.ingress.attachment_staging_started",
        "employee.ingress.attachment_staging_parent_bound",
        "employee.ingress.attachment_staging_failed",
        "employee.ingress.attachment_cleanup_started",
        "employee.ingress.attachment_cleanup_leaf_started",
        "employee.ingress.attachment_cleanup_leaf_completed",
        "employee.ingress.attachment_cleanup_completed",
    ]
    service.close()
    writer.close()


@pytest.mark.parametrize(
    ("content", "declared_mime", "filename", "reason"),
    [
        (PDF, "image/png", "wrong.png", "MIME"),
        (ELF, "application/octet-stream", "payload.bin", "executable"),
        (b"#!/bin/sh\necho owned\n", "text/plain", "notes.txt", "executable"),
        (b"safe text", "text/plain", "launch.exe", "executable"),
    ],
)
def test_mime_magic_and_executable_policy_rejects_unsafe_content(
    api,
    tmp_path,
    content,
    declared_mime,
    filename,
    reason,
) -> None:
    descriptor = _descriptor(
        api,
        content,
        resource_type="file",
        resource_id="file_v2_resource_1",
        declared_mime_type=declared_mime,
        user_filename=filename,
    )
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"file_v2_resource_1": api.DownloadedAttachment(content=content, file_name=filename)},
    )
    with pytest.raises(api.AttachmentValidationError, match=reason):
        service.stage(_request(api, (descriptor,)))
    record = next(iter(service.state.by_staging_id.values()))
    assert record.status == "failed"
    assert service.trusted_paths(record.staging_id) == ()
    assert list((tmp_path / "staging").rglob("*.bin")) == []
    service.close()
    writer.close()


def test_content_hash_mismatch_is_terminal_and_never_returns_a_path(api, tmp_path) -> None:
    descriptor = _descriptor(api, declared_sha256="0" * 64)
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    with pytest.raises(api.AttachmentValidationError, match="hash"):
        service.stage(_request(api, (descriptor,)))
    record = next(iter(service.state.by_staging_id.values()))
    assert service.trusted_paths(record.staging_id) == ()
    assert record.status == "failed"
    service.close()
    writer.close()


def test_user_filename_is_metadata_only_and_storage_path_is_random_and_contained(
    api,
    tmp_path,
) -> None:
    descriptor = _descriptor(api, user_filename="../../outside/diagram.png")
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="../../remote.png")},
    )
    record = service.stage(_request(api, (descriptor,)))
    paths = service.trusted_paths(record.staging_id)

    assert record.status == "completed"
    assert len(paths) == 1
    assert paths[0].read_bytes() == PNG
    assert paths[0].resolve().is_relative_to((tmp_path / "staging").resolve())
    assert "outside" not in paths[0].parts
    assert "diagram.png" not in paths[0].parts
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
    for parent in paths[0].parents:
        if parent == tmp_path:
            break
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    service.close()
    writer.close()


def test_parent_symlink_is_rejected_without_writing_outside_root(api, tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    tenant_component = api.tenant_storage_component("tenant/customer:one")
    (staging / tenant_component).symlink_to(outside, target_is_directory=True)
    service, writer, _vault, _builder, downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    with pytest.raises(api.AttachmentStorageError, match="parent"):
        service.stage(_request(api, (_descriptor(api),)))
    assert downloader.calls == []
    assert list(outside.iterdir()) == []
    service.close()
    writer.close()


def test_leaf_symlink_is_rejected_without_touching_target(api, tmp_path) -> None:
    external = tmp_path / "external.bin"
    external.write_bytes(b"do-not-touch")
    final_name = "att_fixed.bin"

    def create_leaf_symlink():
        envelope_dir = (
            tmp_path
            / "staging"
            / api.tenant_storage_component("tenant/customer:one")
            / "agt_alpha"
            / ("ing_" + "b" * 64)
        )
        (envelope_dir / final_name).symlink_to(external)
        return api.DownloadedAttachment(content=PNG, file_name="diagram.png")

    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": create_leaf_symlink},
        name_factory=lambda: "fixed",
    )
    with pytest.raises(api.AttachmentStorageError, match="leaf"):
        service.stage(_request(api, (_descriptor(api),)))
    assert external.read_bytes() == b"do-not-touch"
    assert not (tmp_path / "staging").resolve().joinpath("external.bin").exists()
    service.close()
    writer.close()


def test_root_symlink_is_rejected_without_chmodding_or_opening_target(api, tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    (tmp_path / "staging").symlink_to(target, target_is_directory=True)
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=MemoryAnchor(),
        hmac_key=HMAC_KEY,
    )
    with pytest.raises(api.AttachmentStorageError, match="root"):
        api.AttachmentStagingService(
            writer=writer,
            root=tmp_path / "staging",
            credential_resolver=_Vault({}),
            downloader_builder=lambda **_: None,
        )
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    writer.close()


def test_employee_credentials_are_selected_exactly_with_no_manager_fallback(api, tmp_path) -> None:
    vault = _Vault({"cred_employee_alpha": "employee-secret"})
    downloader = _Downloader({"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")})
    builder = _Builder(downloader)
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        vault=vault,
        builder=builder,
    )
    record = service.stage(_request(api, (_descriptor(api),)))

    assert record.status == "completed"
    assert vault.calls == [("cred_employee_alpha", "agt_alpha", "cli_alpha")]
    assert builder.calls == [("cli_alpha", "employee-secret", 0.2)]
    assert "manager" not in repr(service).casefold()
    journal_bytes = writer.journal_path.read_bytes()
    assert b"employee-secret" not in journal_bytes
    assert b"cred_employee_alpha" not in journal_bytes
    service.close()
    writer.close()


class _Crash(BaseException):
    pass


def test_crash_after_publish_leaves_owned_paths_for_restart_recovery(api, tmp_path) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )

    def crash(stage: str, _record: object) -> None:
        if stage == "after_publish":
            raise _Crash

    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
        fault_hook=crash,
        name_factory=lambda: "crash",
    )
    with pytest.raises(_Crash):
        service.stage(_request(api, (_descriptor(api),)))
    crashed_record = next(iter(service.state.by_staging_id.values()))
    assert crashed_record.status == "started"
    assert list((tmp_path / "staging").rglob("*.bin"))
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    assert recovered.recover() == 1
    record = recovered.state.by_staging_id[crashed_record.staging_id]
    assert record.status == "failed"
    assert record.cleanup_state == "completed"
    assert recovered.trusted_paths(record.staging_id) == ()
    assert list((tmp_path / "staging").rglob("*.bin")) == []
    recovered.close()
    recovered_writer.close()


def test_after_publish_content_tamper_never_anchors_staging_completed(api, tmp_path) -> None:
    def tamper(stage: str, record: object) -> None:
        if stage == "after_publish":
            final = tmp_path / "staging" / record.relative_paths[0]
            final.write_bytes(b"tampered-after-publish")
            final.chmod(0o600)

    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        fault_hook=tamper,
    )

    with pytest.raises(api.AttachmentStorageError, match="hash"):
        service.stage(_request(api, (_descriptor(api),)))

    record = next(iter(service.state.by_staging_id.values()))
    event_types = [
        event.event_type
        for frame in writer.replay()
        for event in frame.events
    ]
    assert "employee.ingress.attachment_staging_completed" not in event_types
    assert record.status == "failed"
    assert record.cleanup_state == "completed"
    assert service.trusted_paths(record.staging_id) == ()
    service.close()
    writer.close()


def test_after_publish_multileaf_hardlink_never_anchors_completed(api, tmp_path) -> None:
    first = _descriptor(api)
    second = _descriptor(api, resource_id="img_v2_resource_2")
    outside_alias = tmp_path / "outside-after-publish-hardlink.bin"

    def tamper(stage: str, record: object) -> None:
        if stage == "after_publish":
            first_final = tmp_path / "staging" / record.relative_paths[0]
            os.link(first_final, outside_alias)

    names = iter(("first", "second"))
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="one.png"),
            "img_v2_resource_2": api.DownloadedAttachment(content=PNG, file_name="two.png"),
        },
        fault_hook=tamper,
        name_factory=lambda: next(names),
    )

    with pytest.raises(api.AttachmentStorageError, match="leaf"):
        service.stage(_request(api, (first, second)))

    record = next(iter(service.state.by_staging_id.values()))
    event_types = [
        event.event_type
        for frame in writer.replay()
        for event in frame.events
    ]
    assert "employee.ingress.attachment_staging_completed" not in event_types
    assert record.status == "failed"
    assert record.cleanup_state == "started"
    assert outside_alias.read_bytes() == b""
    assert service.trusted_paths(record.staging_id) == ()

    outside_alias.unlink()
    service.cleanup(record.staging_id)
    assert service.state.by_staging_id[record.staging_id].cleanup_state == "completed"
    service.close()
    writer.close()


def test_restart_cleans_zero_byte_temp_crashed_before_leaf_identity_anchor(
    api,
    tmp_path,
) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )

    def crash(stage: str, _record: object) -> None:
        if stage == "after_empty_leaf_fsync":
            raise _Crash

    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
        fault_hook=crash,
        name_factory=lambda: "emptycrash",
    )
    with pytest.raises(_Crash):
        service.stage(_request(api, (_descriptor(api),)))
    crashed = next(iter(service.state.by_staging_id.values()))
    assert crashed.status == "started"
    assert crashed.leaf_identities == (None,)
    temporary = tmp_path / "staging" / crashed.temporary_paths[0]
    assert temporary.stat().st_size == 0
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    assert recovered.recover() == 1
    record = recovered.state.by_staging_id[crashed.staging_id]
    assert record.status == "failed"
    assert record.cleanup_state == "completed"
    assert not temporary.exists()
    recovered.close()
    recovered_writer.close()


def test_restart_resumes_unbound_leaf_cleanup_started_before_completion(
    api,
    tmp_path,
) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )

    def crash(stage: str, _record: object) -> None:
        if stage == "after_unbound_leaf_cleanup_started":
            raise _Crash

    invalid = _descriptor(api, declared_sha256="0" * 64)
    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
        fault_hook=crash,
    )
    with pytest.raises(_Crash):
        service.stage(_request(api, (invalid,)))
    crashed = next(iter(service.state.by_staging_id.values()))
    assert crashed.status == "failed"
    assert crashed.cleanup_state == "started"
    assert crashed.leaf_identities == (None,)
    assert crashed.leaf_cleanup_states == ("started",)
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    assert recovered.recover() == 1
    record = recovered.state.by_staging_id[crashed.staging_id]
    assert record.cleanup_state == "completed"
    assert record.leaf_cleanup_states == ("completed",)
    recovered.close()
    recovered_writer.close()


@pytest.mark.parametrize("replacement_content", [b"replacement", b""])
def test_unbound_cleanup_target_rejects_replacement_after_started_anchor(
    api,
    tmp_path,
    replacement_content,
) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )

    def crash_before_binding(stage: str, _record: object) -> None:
        if stage == "after_empty_leaf_fsync":
            raise _Crash

    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
        fault_hook=crash_before_binding,
        name_factory=lambda: "unboundreplace",
    )
    with pytest.raises(_Crash):
        service.stage(_request(api, (_descriptor(api),)))
    crashed = next(iter(service.state.by_staging_id.values()))
    temporary = tmp_path / "staging" / crashed.temporary_paths[0]
    assert temporary.stat().st_size == 0
    service.close()
    writer.close()

    displaced = temporary.with_name("displaced-original-empty.tmp")
    replaced = False

    def replace_after_started(stage: str, _record: object) -> None:
        nonlocal replaced
        if stage != "after_unbound_leaf_cleanup_started" or replaced:
            return
        replaced = True
        temporary.rename(displaced)
        temporary.write_bytes(replacement_content)
        temporary.chmod(0o600)

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
        fault_hook=replace_after_started,
    )

    with pytest.raises(api.AttachmentStorageError, match="unbound attachment leaf"):
        recovered.recover()

    blocked = recovered.state.by_staging_id[crashed.staging_id]
    assert blocked.cleanup_state == "started"
    assert blocked.leaf_cleanup_states == ("started",)
    assert displaced.exists()
    assert temporary.read_bytes() == replacement_content

    temporary.unlink()
    displaced.rename(temporary)
    assert recovered.recover() == 1
    converged = recovered.state.by_staging_id[crashed.staging_id]
    assert converged.cleanup_state == "completed"
    assert not temporary.exists()
    recovered.close()
    recovered_writer.close()


def test_restart_recovers_failure_anchored_before_cleanup_started(api, tmp_path) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    names = iter(("published", "rejected"))
    first = _descriptor(api)
    second = _descriptor(
        api,
        PDF,
        resource_type="file",
        resource_id="file_v2_resource_2",
        declared_mime_type="application/pdf",
        declared_sha256="0" * 64,
        user_filename="document.pdf",
    )
    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            ),
            "file_v2_resource_2": api.DownloadedAttachment(
                content=PDF,
                file_name="document.pdf",
            ),
        },
        writer=writer,
        name_factory=lambda: next(names),
    )

    with (
        patch.object(service, "_cleanup_unlocked", side_effect=_Crash),
        pytest.raises(_Crash),
    ):
        service.stage(_request(api, (first, second)))

    crashed_record = next(iter(service.state.by_staging_id.values()))
    assert crashed_record.status == "failed"
    assert crashed_record.cleanup_state == "none"
    assert crashed_record.leaf_identities[0] is not None
    assert crashed_record.leaf_identities[1] is None
    assert len(list((tmp_path / "staging").rglob("*.bin"))) == 1
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    assert recovered.recover() == 1
    record = recovered.state.by_staging_id[crashed_record.staging_id]
    assert record.status == "failed"
    assert record.cleanup_state == "completed"
    assert list((tmp_path / "staging").rglob("*.bin")) == []
    recovered.close()
    recovered_writer.close()


def test_recover_does_not_cleanup_successfully_completed_staging(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            )
        },
    )
    completed = service.stage(_request(api, (_descriptor(api),)))

    assert service.recover() == 0
    assert service.state.by_staging_id[completed.staging_id].cleanup_state == "none"
    assert len(service.trusted_paths(completed.staging_id)) == 1
    service.close()
    writer.close()


def test_stage_records_and_replayed_projection_never_expose_unverified_paths(
    api,
    tmp_path,
) -> None:
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            )
        },
        writer=writer,
    )
    completed = service.stage(_request(api, (_descriptor(api),)))

    assert not hasattr(completed, "trusted_paths")
    assert not hasattr(
        service.state.by_staging_id[completed.staging_id],
        "trusted_paths",
    )
    verified = service.trusted_paths(completed.staging_id)
    assert len(verified) == 1
    assert verified[0].is_absolute()
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    projected = recovered.state.by_staging_id[completed.staging_id]
    assert not hasattr(projected, "trusted_paths")
    replay_verified = recovered.trusted_paths(projected.staging_id)
    assert replay_verified == verified
    recovered.close()
    recovered_writer.close()


def test_restart_recovers_cleanup_interrupted_after_durable_start(api, tmp_path) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    cleanup_started = threading.Event()

    def crash(stage: str, _record: object) -> None:
        if stage == "after_cleanup_started" and not cleanup_started.is_set():
            cleanup_started.set()
            raise _Crash

    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
        fault_hook=crash,
        name_factory=lambda: "cleanup",
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    with pytest.raises(_Crash):
        service.cleanup(completed.staging_id)
    assert service.state.by_staging_id[completed.staging_id].cleanup_state == "started"
    assert list((tmp_path / "staging").rglob("*.bin"))
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    assert recovered.recover() == 1
    record = recovered.state.by_staging_id[completed.staging_id]
    assert record.status == "completed"
    assert record.cleanup_state == "completed"
    assert recovered.trusted_paths(record.staging_id) == ()
    assert list((tmp_path / "staging").rglob("*.bin")) == []
    recovered.close()
    recovered_writer.close()


def test_restart_completes_cleanup_after_erased_leaf_unlink_crash(api, tmp_path) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    crashed_once = False

    def crash(stage: str, _record: object) -> None:
        nonlocal crashed_once
        if stage == "after_leaf_unlink" and not crashed_once:
            crashed_once = True
            raise _Crash

    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
        fault_hook=crash,
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    with pytest.raises(_Crash):
        service.cleanup(completed.staging_id)
    crashed = service.state.by_staging_id[completed.staging_id]
    assert crashed.cleanup_state == "started"
    assert crashed.leaf_cleanup_states == ("completed",)
    assert not list((tmp_path / "staging").rglob("*.bin"))
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    assert recovered.recover() == 1
    record = recovered.state.by_staging_id[completed.staging_id]
    assert record.cleanup_state == "completed"
    recovered.close()
    recovered_writer.close()


@pytest.mark.parametrize("alias_scope", ["inside", "outside"])
def test_restart_rejects_alias_added_after_leaf_erasure_completion(
    api,
    tmp_path,
    alias_scope,
) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    alias_path: Path | None = None

    def crash(stage: str, record: object) -> None:
        nonlocal alias_path
        if stage != "after_leaf_erased":
            return
        final = tmp_path / "staging" / record.relative_paths[0]
        alias_path = (
            final.with_name("inside-posterase-hardlink.bin")
            if alias_scope == "inside"
            else tmp_path / "outside-posterase-hardlink.bin"
        )
        os.link(final, alias_path)
        raise _Crash

    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
        fault_hook=crash,
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    final = service.trusted_paths(completed.staging_id)[0]
    with pytest.raises(_Crash):
        service.cleanup(completed.staging_id)
    crashed = service.state.by_staging_id[completed.staging_id]
    assert alias_path is not None
    assert crashed.cleanup_state == "started"
    assert crashed.leaf_cleanup_states == ("completed",)
    assert final.read_bytes() == b""
    assert alias_path.read_bytes() == b""
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    with pytest.raises(api.AttachmentStorageError, match="leaf"):
        recovered.recover()
    blocked = recovered.state.by_staging_id[completed.staging_id]
    assert blocked.cleanup_state == "started"
    assert blocked.leaf_cleanup_states == ("completed",)
    assert final.exists()
    assert alias_path.exists()

    alias_path.unlink()
    assert recovered.recover() == 1
    converged = recovered.state.by_staging_id[completed.staging_id]
    assert converged.cleanup_state == "completed"
    assert not final.exists()
    recovered.close()
    recovered_writer.close()


def test_restart_repeats_exact_zeroing_after_truncate_before_leaf_completion(
    api,
    tmp_path,
) -> None:
    anchor = FileAnchor(tmp_path / "anchor.json")
    writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=1,
    )
    crashed_once = False

    def crash(stage: str, _record: object) -> None:
        nonlocal crashed_once
        if stage == "after_leaf_truncate" and not crashed_once:
            crashed_once = True
            raise _Crash

    service, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        writer=writer,
        fault_hook=crash,
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    staged_path = service.trusted_paths(completed.staging_id)[0]
    with pytest.raises(_Crash):
        service.cleanup(completed.staging_id)
    crashed = service.state.by_staging_id[completed.staging_id]
    assert crashed.cleanup_state == "started"
    assert crashed.leaf_cleanup_states == ("started",)
    assert staged_path.stat().st_size == 0
    service.close()
    writer.close()

    recovered_writer = JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=2,
    )
    recovered, _writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {},
        writer=recovered_writer,
    )
    assert recovered.recover() == 1
    record = recovered.state.by_staging_id[completed.staging_id]
    assert record.cleanup_state == "completed"
    assert not staged_path.exists()
    recovered.close()
    recovered_writer.close()


def test_cleanup_does_not_claim_completion_after_parent_symlink_replacement(
    api,
    tmp_path,
) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            )
        },
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    tenant = tmp_path / "staging" / api.tenant_storage_component("tenant/customer:one")
    displaced = tmp_path / "displaced-tenant"
    tenant.rename(displaced)
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    tenant.symlink_to(attacker, target_is_directory=True)

    with pytest.raises(api.AttachmentStorageError, match="parent"):
        service.cleanup(completed.staging_id)

    assert service.state.by_staging_id[completed.staging_id].cleanup_state == "started"
    assert list(displaced.rglob("*.bin"))
    assert list(attacker.iterdir()) == []
    service.close()
    writer.close()


def test_cleanup_rejects_same_uid_ordinary_parent_directory_substitution(
    api,
    tmp_path,
) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            )
        },
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    verified = service.trusted_paths(completed.staging_id)
    original_parent = verified[0].parent
    displaced = tmp_path / "displaced-envelope"
    original_parent.rename(displaced)
    original_parent.mkdir(mode=0o700)

    with pytest.raises(api.AttachmentStorageError, match="parent identity"):
        service.cleanup(completed.staging_id)

    assert service.state.by_staging_id[completed.staging_id].cleanup_state == "started"
    assert len(list(displaced.glob("*.bin"))) == 1
    assert list(original_parent.iterdir()) == []
    service.close()
    writer.close()


def test_trusted_path_export_rejects_wrong_owner_regular_leaf(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            )
        },
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    real_fstat = os.fstat

    def wrong_leaf_owner(descriptor: int):
        current = real_fstat(descriptor)
        if not stat.S_ISREG(current.st_mode):
            return current
        values = list(current)
        values[4] = current.st_uid + 1
        return os.stat_result(values)

    with (
        patch.object(api.os, "fstat", side_effect=wrong_leaf_owner),
        pytest.raises(api.AttachmentStorageError, match="trusted"),
    ):
        service.trusted_paths(completed.staging_id)

    service.close()
    writer.close()


def test_trusted_path_rechecks_mode_after_read(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    real_read = api._read_at_most

    def chmod_during_read(fd: int, maximum: int) -> bytes:
        content = real_read(fd, maximum)
        os.fchmod(fd, 0o644)
        return content

    with (
        patch.object(api, "_read_at_most", side_effect=chmod_during_read),
        pytest.raises(api.AttachmentStorageError, match="trusted"),
    ):
        service.trusted_paths(completed.staging_id)

    service.close()
    writer.close()


def test_trusted_path_rejects_same_inode_rewrite_after_read(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    final = service.trusted_paths(completed.staging_id)[0]
    real_read = api._read_at_most
    rewritten = b"X" * len(PNG)

    def rewrite_after_read(fd: int, maximum: int) -> bytes:
        content = real_read(fd, maximum)
        writer_fd = os.open(final, os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            os.pwrite(writer_fd, rewritten, 0)
            os.fsync(writer_fd)
        finally:
            os.close(writer_fd)
        return content

    with (
        patch.object(api, "_read_at_most", side_effect=rewrite_after_read),
        pytest.raises(api.AttachmentStorageError, match="trusted"),
    ):
        service.trusted_paths(completed.staging_id)

    assert final.read_bytes() == rewritten
    service.close()
    writer.close()


def test_trusted_path_reopens_current_parent_after_read(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    original = service.trusted_paths(completed.staging_id)[0]
    original_parent = original.parent
    displaced_parent = tmp_path / "trusted-read-displaced-parent"
    real_read = api._read_at_most
    replaced = False

    def replace_parent_during_read(fd: int, maximum: int) -> bytes:
        nonlocal replaced
        content = real_read(fd, maximum)
        if not replaced:
            replaced = True
            original_parent.rename(displaced_parent)
            original_parent.mkdir(mode=0o700)
            replacement = original_parent / original.name
            replacement.write_bytes(PNG)
            replacement.chmod(0o600)
        return content

    with (
        patch.object(api, "_read_at_most", side_effect=replace_parent_during_read),
        pytest.raises(api.AttachmentStorageError, match="parent identity"),
    ):
        service.trusted_paths(completed.staging_id)

    service.close()
    writer.close()


def test_cleanup_rejects_same_uid_ordinary_leaf_substitution(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            )
        },
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    verified = service.trusted_paths(completed.staging_id)
    original = verified[0]
    displaced = original.with_name("unexpected-displaced.bin")
    original.rename(displaced)
    original.write_bytes(PNG)
    original.chmod(0o600)

    with pytest.raises(api.AttachmentStorageError, match="leaf identity"):
        service.cleanup(completed.staging_id)

    cleanup_record = service.state.by_staging_id[completed.staging_id]
    assert cleanup_record.cleanup_state == "started"
    assert cleanup_record.leaf_cleanup_states == ("started",)
    assert displaced.read_bytes() == b""
    assert original.read_bytes() == PNG
    service.close()
    writer.close()


def test_cleanup_does_not_complete_when_bound_inode_was_moved_outside_parent(
    api,
    tmp_path,
) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    original = service.trusted_paths(completed.staging_id)[0]
    outside = tmp_path / "moved-outside.bin"
    original.rename(outside)

    with pytest.raises(api.AttachmentStorageError, match="identity is missing"):
        service.cleanup(completed.staging_id)

    record = service.state.by_staging_id[completed.staging_id]
    assert record.cleanup_state == "started"
    assert record.leaf_cleanup_states == ("none",)
    assert outside.read_bytes() == PNG
    service.close()
    writer.close()


def test_trusted_path_rejects_same_content_regular_leaf_replacement(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {
            "img_v2_resource_1": api.DownloadedAttachment(
                content=PNG,
                file_name="diagram.png",
            )
        },
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    original = service.trusted_paths(completed.staging_id)[0]
    displaced = original.with_name("same-content-displaced.bin")
    original.rename(displaced)
    original.write_bytes(PNG)
    original.chmod(0o600)

    with pytest.raises(api.AttachmentStorageError, match="leaf"):
        service.trusted_paths(completed.staging_id)

    assert displaced.read_bytes() == PNG

    service.close()
    writer.close()


@pytest.mark.parametrize("alias_kind", ["temporary", "unexpected"])
def test_trusted_path_rejects_exact_inode_with_second_parent_name(
    api,
    tmp_path,
    alias_kind,
) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    final = service.trusted_paths(completed.staging_id)[0]
    alias = (
        tmp_path / "staging" / completed.temporary_paths[0]
        if alias_kind == "temporary"
        else final.with_name("unexpected-hardlink.bin")
    )
    os.link(final, alias)

    with pytest.raises(api.AttachmentStorageError, match="leaf"):
        service.trusted_paths(completed.staging_id)

    service.close()
    writer.close()


def test_leaf_prepared_hardlink_is_rejected_before_writing_user_bytes(api, tmp_path) -> None:
    alias_path: Path | None = None

    def add_alias(stage: str, record: object) -> None:
        nonlocal alias_path
        if stage != "after_leaf_prepared":
            return
        temporary = tmp_path / "staging" / record.temporary_paths[0]
        alias_path = temporary.with_name("unexpected-prewrite-hardlink.bin")
        os.link(temporary, alias_path)

    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        fault_hook=add_alias,
    )

    with pytest.raises(api.AttachmentStorageError, match="leaf"):
        service.stage(_request(api, (_descriptor(api),)))

    record = next(iter(service.state.by_staging_id.values()))
    assert alias_path is not None
    assert alias_path.stat().st_size == 0
    assert record.status == "failed"
    assert record.cleanup_state == "started"
    assert record.leaf_cleanup_states == ("started",)
    service.close()
    writer.close()


def test_post_rename_hardlink_is_erased_and_never_completes_staging(api, tmp_path) -> None:
    alias_path: Path | None = None

    def add_alias(stage: str, record: object) -> None:
        nonlocal alias_path
        if stage != "after_leaf_rename":
            return
        final = tmp_path / "staging" / record.relative_paths[0]
        alias_path = final.with_name("unexpected-postrename-hardlink.bin")
        os.link(final, alias_path)

    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
        fault_hook=add_alias,
    )

    with pytest.raises(api.AttachmentStorageError, match="leaf"):
        service.stage(_request(api, (_descriptor(api),)))

    record = next(iter(service.state.by_staging_id.values()))
    final = tmp_path / "staging" / record.relative_paths[0]
    assert alias_path is not None
    assert final.read_bytes() == b""
    assert alias_path.read_bytes() == b""
    assert record.status == "failed"
    assert record.cleanup_state == "started"
    assert record.leaf_cleanup_states == ("started",)
    service.close()
    writer.close()


def test_cleanup_rejects_bound_inode_present_at_temp_and_final(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    final = service.trusted_paths(completed.staging_id)[0]
    temporary = tmp_path / "staging" / completed.temporary_paths[0]
    os.link(final, temporary)

    with pytest.raises(api.AttachmentStorageError, match="multiple names"):
        service.cleanup(completed.staging_id)

    assert service.state.by_staging_id[completed.staging_id].cleanup_state == "started"
    assert service.state.by_staging_id[completed.staging_id].leaf_cleanup_states == ("started",)
    assert final.exists()
    assert temporary.exists()
    assert final.read_bytes() == b""
    assert temporary.read_bytes() == b""
    service.close()
    writer.close()


def test_only_completed_staging_exposes_gateway_trusted_paths(api, tmp_path) -> None:
    service, writer, _vault, _builder, _downloader = _service(
        api,
        tmp_path,
        {"img_v2_resource_1": api.DownloadedAttachment(content=PNG, file_name="diagram.png")},
    )
    completed = service.stage(_request(api, (_descriptor(api),)))
    paths = service.trusted_paths(completed.staging_id)
    assert len(paths) == 1
    assert all(path.is_absolute() for path in paths)

    paths[0].chmod(0o644)
    with pytest.raises(api.AttachmentStorageError, match="trusted"):
        service.trusted_paths(completed.staging_id)
    service.close()
    writer.close()
