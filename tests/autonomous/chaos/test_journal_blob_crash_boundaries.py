from pathlib import Path
from typing import Any

import pytest

import src.autonomous.journal.blob_store as blob_store_module
from src.autonomous.journal import (
    AesGcmEncryptionProvider,
    BlobRef,
    BlobStore,
    JournalIntegrityError,
    JournalWriter,
    MemoryAnchor,
)
from src.autonomous.journal.frame import JournalEvent

HMAC_KEY = b"journal-blob-chaos-test-key-at-least-32-bytes"
BLOB_KEY = b"0123456789abcdef0123456789abcdef"


def blob_event(blob_ref: BlobRef) -> JournalEvent:
    to_dict = getattr(blob_ref, "to_dict", None)
    reference = to_dict() if callable(to_dict) else {
        field: getattr(blob_ref, field)
        for field in (
            "blob_id",
            "content_hash",
            "ciphertext_hash",
            "size",
            "labels",
            "key_ref",
        )
        if hasattr(blob_ref, field)
    }
    return JournalEvent(
        event_type="evidence.recorded",
        aggregate_id="run_1",
        payload={"blob_ref": reference},
    )


def open_writer(
    base_dir: Path,
    anchor: Any,
    *,
    writer_epoch: int = 7,
    blob_ref_validator: Any = None,
) -> JournalWriter:
    return JournalWriter.open(
        base_dir,
        anchor=anchor,
        hmac_key=HMAC_KEY,
        writer_epoch=writer_epoch,
        blob_ref_validator=blob_ref_validator,
    )


def test_blob_stage_failure_never_produces_a_journal_frame(tmp_path: Path) -> None:
    base_dir = tmp_path / "journal"
    anchor = MemoryAnchor()
    writer = open_writer(base_dir, anchor)

    def failing_stage_and_publish(*_: Any, **__: Any) -> BlobRef:
        raise OSError("injected failure before blob publish")

    before = list(writer.replay(from_sequence=1))
    with pytest.raises(OSError, match="before blob publish"):
        blob_ref = failing_stage_and_publish(
            b"sensitive model output",
            labels={"purpose": "evidence"},
            key_ref="tenant-key-v1",
        )
        writer.commit([blob_event(blob_ref)], {"run_1": 0})

    assert list(writer.replay(from_sequence=1)) == before == []
    writer.close()


@pytest.mark.parametrize(
    "boundary",
    ["_write_bytes", "_fsync_file", "_atomic_replace", "_fsync_directory"],
)
def test_real_blob_publish_failure_never_produces_a_journal_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    base_dir = tmp_path / "journal"
    anchor = MemoryAnchor()
    writer = open_writer(base_dir, anchor)
    store = BlobStore(
        tmp_path / "blobs",
        AesGcmEncryptionProvider(lambda _key_ref: BLOB_KEY),
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"injected {boundary} failure")

    monkeypatch.setattr(blob_store_module, boundary, fail)

    with pytest.raises(Exception, match="injected"):
        ref = store.stage_and_publish(
            b"sensitive model output",
            labels={"purpose": "evidence"},
            key_ref="tenant-key-v1",
        )
        writer.commit([blob_event(ref)], {"run_1": 0})

    writer.close()
    restarted = open_writer(base_dir, anchor, writer_epoch=8)
    assert list(restarted.replay(from_sequence=1)) == []
    restarted.close()


def test_published_blob_and_frame_survive_real_writer_restart(tmp_path: Path) -> None:
    base_dir = tmp_path / "journal"
    blob_dir = tmp_path / "blobs"
    anchor = MemoryAnchor()
    store = BlobStore(
        blob_dir,
        AesGcmEncryptionProvider(lambda _key_ref: BLOB_KEY),
    )

    def validate(ref: BlobRef) -> bool:
        try:
            return store.read(ref) == b"sensitive model output"
        except Exception:
            return False

    ref = store.stage_and_publish(
        b"sensitive model output",
        labels={"purpose": "evidence"},
        key_ref="tenant-key-v1",
    )
    writer = open_writer(base_dir, anchor, blob_ref_validator=validate)
    writer.commit([blob_event(ref)], {"run_1": 0})
    writer.close()

    restarted = open_writer(
        base_dir,
        anchor,
        writer_epoch=8,
        blob_ref_validator=validate,
    )
    frames = list(restarted.replay(from_sequence=1))
    restarted.close()

    assert len(frames) == 1
    assert frames[0].events[0].payload["blob_ref"]["blob_id"] == ref.blob_id


def test_published_blob_must_validate_before_blobref_frame_commit(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "journal"
    anchor = MemoryAnchor()
    blob_id = "b" * 64
    published: set[str] = set()

    def validate(blob_ref: BlobRef) -> bool:
        blob_id = getattr(blob_ref, "blob_id", None)
        if blob_id is None:
            blob_id = getattr(blob_ref, "ciphertext_hash")
        return blob_id in published

    writer = open_writer(base_dir, anchor, blob_ref_validator=validate)
    ref = BlobRef(
        blob_id=blob_id,
        content_hash="a" * 64,
        ciphertext_hash=blob_id,
        size=22,
        labels={"purpose": "evidence"},
        key_ref="tenant-key-v1",
    )

    journal_before_publish = (base_dir / "journal.jsonl").read_bytes()
    with pytest.raises(JournalIntegrityError, match="blob"):
        writer.commit([blob_event(ref)], {"run_1": 0})
    assert (base_dir / "journal.jsonl").read_bytes() == journal_before_publish

    published.add(blob_id)
    result = writer.commit([blob_event(ref)], {"run_1": 0})
    assert getattr(result, "frame", result).sequence == 1
    writer.close()


def test_missing_blob_referenced_by_valid_frame_fails_closed_on_restart(
    tmp_path: Path,
) -> None:
    base_dir = tmp_path / "journal"
    anchor = MemoryAnchor()
    blob_id = "d" * 64
    existing = {blob_id}

    def validate(blob_ref: BlobRef) -> bool:
        blob_id = getattr(blob_ref, "blob_id", None)
        if blob_id is None:
            blob_id = getattr(blob_ref, "ciphertext_hash")
        return blob_id in existing

    ref = BlobRef(
        blob_id=blob_id,
        content_hash="c" * 64,
        ciphertext_hash=blob_id,
        size=19,
        labels={"purpose": "tool_result"},
        key_ref="tenant-key-v1",
    )
    writer = open_writer(base_dir, anchor, blob_ref_validator=validate)
    writer.commit([blob_event(ref)], {"run_1": 0})
    writer.close()
    existing.clear()

    with pytest.raises(JournalIntegrityError, match="blob"):
        open_writer(
            base_dir,
            anchor,
            writer_epoch=8,
            blob_ref_validator=validate,
        )
