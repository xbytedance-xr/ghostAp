import base64
import hashlib
import json
import stat
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

import src.autonomous.journal.blob_store as blob_store_module
from src.autonomous.journal.blob_store import (
    AesGcmEncryptionProvider,
    BlobAuthenticationError,
    BlobFormatError,
    BlobIntegrityError,
    BlobPublishError,
    BlobRef,
    BlobStore,
    InvalidEncryptionKeyError,
    KeyResolutionError,
)

KEY_REF = "test-key-v1"
KEY = b"0123456789abcdef0123456789abcdef"
OTHER_KEY = b"abcdef0123456789abcdef0123456789"
PAYLOAD = b"private payload: customer=alice@example.com"
LABELS = {"classification": "restricted", "tenant": "tenant-1"}


def key_resolver(key_ref: str) -> bytes:
    if key_ref != KEY_REF:
        raise KeyError(key_ref)
    return KEY


def make_store(root: Path, *, key: bytes = KEY) -> BlobStore:
    def resolve(requested_ref: str) -> bytes:
        if requested_ref != KEY_REF:
            raise KeyError(requested_ref)
        return key

    return BlobStore(root, AesGcmEncryptionProvider(resolve))


def blob_path(root: Path, ref: BlobRef) -> Path:
    return root / f"{ref.blob_hash}.blob"


def rewrite_envelope(root: Path, ref: BlobRef, **changes: object) -> BlobRef:
    original_path = blob_path(root, ref)
    envelope = json.loads(original_path.read_bytes())
    envelope.update(changes)
    rewritten = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()
    rewritten_hash = hashlib.sha256(rewritten).hexdigest()
    rewritten_path = root / f"{rewritten_hash}.blob"
    rewritten_path.write_bytes(rewritten)
    rewritten_path.chmod(0o600)
    return replace(
        ref,
        blob_hash=rewritten_hash,
        payload_hash=str(envelope["payload_hash"]),
        labels_hash=str(envelope["labels_hash"]),
        key_ref=str(envelope["key_ref"]),
    )


def test_round_trip_returns_immutable_content_addressed_ref(tmp_path: Path) -> None:
    store = make_store(tmp_path / "blobs")

    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    assert store.read(ref) == PAYLOAD
    assert ref.payload_hash == hashlib.sha256(PAYLOAD).hexdigest()
    assert len(ref.blob_hash) == 64
    assert len(ref.labels_hash) == 64
    assert ref.key_ref == KEY_REF
    with pytest.raises(FrozenInstanceError):
        ref.blob_hash = "f" * 64  # type: ignore[misc]


def test_disk_envelope_contains_required_metadata_but_no_plaintext_or_key(
    tmp_path: Path,
) -> None:
    root = tmp_path / "blobs"
    ref = make_store(root).stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    raw = blob_path(root, ref).read_bytes()
    envelope = json.loads(raw)

    assert set(envelope) == {
        "ciphertext",
        "key_ref",
        "labels_hash",
        "magic",
        "nonce",
        "payload_hash",
        "schema_version",
        "tag",
    }
    assert envelope["magic"] == blob_store_module.BLOB_MAGIC
    assert envelope["schema_version"] == blob_store_module.BLOB_SCHEMA_VERSION
    assert envelope["key_ref"] == KEY_REF
    assert envelope["labels_hash"] == ref.labels_hash
    assert envelope["payload_hash"] == ref.payload_hash
    assert PAYLOAD not in raw
    assert KEY not in raw
    assert base64.b64decode(envelope["ciphertext"], validate=True) != PAYLOAD


def test_store_directory_and_blob_permissions_are_private(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    root.mkdir(mode=0o755)
    ref = make_store(root).stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(blob_path(root, ref).stat().st_mode) == 0o600


def test_same_payload_uses_random_nonce_and_produces_distinct_blobs(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path / "blobs")

    first = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)
    second = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    assert first.payload_hash == second.payload_hash
    assert first.labels_hash == second.labels_hash
    assert first.blob_hash != second.blob_hash
    assert blob_path(tmp_path / "blobs", first).read_bytes() != blob_path(
        tmp_path / "blobs", second
    ).read_bytes()


def test_existing_identical_content_address_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(blob_store_module, "_random_nonce", lambda: b"\x01" * 12)
    store = make_store(tmp_path / "blobs")

    first = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)
    second = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    assert second == first
    assert len(list((tmp_path / "blobs").glob("*.blob"))) == 1


def test_existing_content_address_with_different_bytes_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(blob_store_module, "_random_nonce", lambda: b"\x02" * 12)
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)
    blob_path(root, ref).write_bytes(b"corrupt")

    with pytest.raises(BlobIntegrityError, match="existing"):
        store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)


def test_blob_hash_tampering_fails_before_decryption(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)
    path = blob_path(root, ref)
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(BlobIntegrityError, match="blob hash"):
        store.read(ref)


def test_gcm_tag_tampering_fails_authentication(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)
    envelope = json.loads(blob_path(root, ref).read_bytes())
    tag = bytearray(base64.b64decode(envelope["tag"], validate=True))
    tag[0] ^= 1
    rewritten_ref = rewrite_envelope(
        root, ref, tag=base64.b64encode(bytes(tag)).decode("ascii")
    )

    with pytest.raises(BlobAuthenticationError):
        store.read(rewritten_ref)


def test_aad_bound_labels_tampering_fails_authentication(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)
    rewritten_ref = rewrite_envelope(root, ref, labels_hash="f" * 64)

    with pytest.raises(BlobAuthenticationError):
        store.read(rewritten_ref)


def test_payload_hash_tampering_is_detected_after_decryption(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)
    rewritten_ref = rewrite_envelope(root, ref, payload_hash="f" * 64)

    with pytest.raises(BlobIntegrityError, match="payload hash"):
        store.read(rewritten_ref)


def test_declared_plaintext_size_is_verified(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    with pytest.raises(BlobIntegrityError, match="size"):
        store.read(replace(ref, size=ref.size + 1))


def test_reference_hashes_must_be_sha256_hex(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    with pytest.raises(BlobIntegrityError, match="blob hash"):
        store.read(replace(ref, blob_hash="../../outside"))


def test_reference_labels_must_match_labels_hash(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    with pytest.raises(BlobIntegrityError, match="labels hash"):
        store.read(replace(ref, labels={"classification": "public"}))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("magic", "NOT-GHOSTAP"),
        ("schema_version", 999),
        ("nonce", "not-base64!"),
    ],
)
def test_unknown_or_malformed_blob_format_fails_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)
    rewritten_ref = rewrite_envelope(root, ref, **{field: value})

    with pytest.raises(BlobFormatError):
        store.read(rewritten_ref)


def test_wrong_key_fails_authentication(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    ref = make_store(root).stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    with pytest.raises(BlobAuthenticationError):
        make_store(root, key=OTHER_KEY).read(ref)


def test_wrong_key_ref_fails_before_key_resolution(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    ref = store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    with pytest.raises(BlobIntegrityError, match="key_ref"):
        store.read(replace(ref, key_ref="other-key-ref"))


def test_missing_key_and_short_key_have_explicit_errors(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    provider = AesGcmEncryptionProvider(key_resolver)

    with pytest.raises(KeyResolutionError):
        BlobStore(root, provider).stage_and_publish(PAYLOAD, LABELS, "missing")

    short_provider = AesGcmEncryptionProvider(lambda _key_ref: b"too-short")
    with pytest.raises(InvalidEncryptionKeyError):
        BlobStore(root, short_provider).stage_and_publish(PAYLOAD, LABELS, KEY_REF)


@pytest.mark.parametrize(
    "boundary",
    ["_write_bytes", "_fsync_file", "_atomic_replace", "_fsync_directory"],
)
def test_publish_boundary_failure_never_returns_a_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    root = tmp_path / boundary
    store = make_store(root)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"injected {boundary} failure")

    monkeypatch.setattr(blob_store_module, boundary, fail)

    with pytest.raises(BlobPublishError, match=boundary):
        store.stage_and_publish(PAYLOAD, LABELS, KEY_REF)

    assert not list(root.glob("*.tmp"))


def test_cleanup_orphan_temps_removes_only_blob_temp_files(tmp_path: Path) -> None:
    root = tmp_path / "blobs"
    store = make_store(root)
    orphan = root / ".blob-orphan.tmp"
    unrelated = root / "keep.tmp"
    orphan.write_bytes(b"partial")
    unrelated.write_bytes(b"not owned by BlobStore")

    assert store.cleanup_orphan_temps() == 1
    assert not orphan.exists()
    assert unrelated.read_bytes() == b"not owned by BlobStore"
