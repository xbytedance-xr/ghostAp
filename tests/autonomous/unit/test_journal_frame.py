import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.autonomous.journal.anchor import AnchorState, MemoryAnchor
from src.autonomous.journal.frame import (
    COMMIT_MARKER,
    FRAME_MAGIC,
    FRAME_SCHEMA_VERSION,
    GENESIS_HASH,
    IncompleteFrameError,
    JournalEvent,
    JournalIntegrityError,
    TransactionFrame,
    decode_frame,
)

HMAC_KEY = b"frame-test-key-with-at-least-32-bytes"
EVENT_TIMESTAMP = 1_750_000_000.125


def event(aggregate_id: str = "goal_1") -> JournalEvent:
    return JournalEvent(
        event_type="goal.created",
        aggregate_id=aggregate_id,
        payload={"title": "durable goal"},
        timestamp=EVENT_TIMESTAMP,
    )


def frame_bytes(**overrides: object) -> bytes:
    values = {
        "tx_id": "tx_1",
        "sequence": 1,
        "writer_epoch": 7,
        "timestamp": 1_750_000_000.25,
        "expected_versions": {"goal_1": 0},
        "aggregate_versions": {"goal_1": 1},
        "previous_hash": GENESIS_HASH,
        "events": (event(),),
        "hmac_key": HMAC_KEY,
    }
    values.update(overrides)
    return TransactionFrame.seal(**values).to_bytes()


def mutate(record: bytes, field: str, value: object) -> bytes:
    decoded = json.loads(record)
    decoded[field] = value
    return json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()


def test_event_payload_hash_uses_canonical_json() -> None:
    first = JournalEvent(
        event_type="goal.updated",
        aggregate_id="goal_1",
        payload={"nested": {"β": 2, "a": 1}, "items": [3, 2, 1]},
        timestamp=EVENT_TIMESTAMP,
    )
    second = JournalEvent(
        event_type="goal.updated",
        aggregate_id="goal_1",
        payload={"items": [3, 2, 1], "nested": {"a": 1, "β": 2}},
        timestamp=EVENT_TIMESTAMP,
    )
    canonical = json.dumps(
        first.payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()

    assert first.payload_hash == hashlib.sha256(canonical).hexdigest()
    assert second.payload_hash == first.payload_hash


def test_frame_encoding_is_stable_and_newline_delimited() -> None:
    frame = TransactionFrame.seal(
        tx_id="tx_stable",
        sequence=12,
        writer_epoch=4,
        timestamp=1_750_000_000.25,
        expected_versions={"goal_2": 5, "goal_1": 3},
        aggregate_versions={"goal_2": 6, "goal_1": 4},
        previous_hash="a" * 64,
        events=(event("goal_2"), event("goal_1")),
        hmac_key=HMAC_KEY,
    )

    first = frame.to_bytes()
    second = frame.to_bytes()

    assert first == second
    assert first.endswith(b"\n")
    assert json.loads(first)["byte_length"] == len(first)
    assert frame.frame_hash == TransactionFrame.record_hash(first)


def test_frame_round_trip_preserves_complete_security_envelope() -> None:
    record = frame_bytes()

    decoded = decode_frame(record, HMAC_KEY)

    assert decoded.magic == FRAME_MAGIC
    assert decoded.schema_version == FRAME_SCHEMA_VERSION
    assert decoded.byte_length == len(record)
    assert decoded.commit_marker == COMMIT_MARKER
    assert decoded.committed is True
    assert decoded.tx_id == "tx_1"
    assert decoded.sequence == 1
    assert decoded.writer_epoch == 7
    assert decoded.timestamp == 1_750_000_000.25
    assert decoded.expected_versions == {"goal_1": 0}
    assert decoded.aggregate_versions == {"goal_1": 1}
    assert decoded.previous_hash == GENESIS_HASH
    assert decoded.events == (event(),)
    assert len(decoded.checksum) == 64
    assert len(decoded.hmac_digest) == 64
    assert decoded.frame_hash == TransactionFrame.record_hash(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sequence", 9),
        ("timestamp", 1_750_000_009.25),
        ("aggregate_versions", {"goal_1": 9}),
        ("expected_versions", {"goal_1": 8}),
        ("commit_marker", "COMMITTEZ"),
        ("committed", False),
        ("tx_id", "tx_9"),
        ("writer_epoch", 9),
        ("previous_hash", "f" * 64),
        ("checksum", "f" * 64),
        ("hmac_digest", "f" * 64),
    ],
)
def test_security_metadata_tampering_is_detected(field: str, value: object) -> None:
    with pytest.raises(JournalIntegrityError):
        decode_frame(mutate(frame_bytes(), field, value), HMAC_KEY)


def test_payload_hash_tampering_is_detected() -> None:
    decoded = json.loads(frame_bytes())
    decoded["events"][0]["payload"]["title"] = "rewritten"
    record = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(JournalIntegrityError):
        decode_frame(record, HMAC_KEY)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload_hash", "f" * 64),
        ("timestamp", EVENT_TIMESTAMP + 1),
        ("event_type", "goal.deleted"),
        ("aggregate_id", "goal_9"),
    ],
)
def test_event_security_metadata_tampering_is_detected(field: str, value: object) -> None:
    decoded = json.loads(frame_bytes())
    decoded["events"][0][field] = value
    record = json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(JournalIntegrityError):
        decode_frame(record, HMAC_KEY)


def test_declared_physical_length_is_verified() -> None:
    record = mutate(frame_bytes(), "byte_length", 1)

    with pytest.raises(JournalIntegrityError, match="length"):
        decode_frame(record, HMAC_KEY)


def test_uncommitted_frame_is_distinguished_from_committed_corruption() -> None:
    record = frame_bytes(committed=False)

    with pytest.raises(IncompleteFrameError):
        decode_frame(record, HMAC_KEY)


def test_physical_tail_truncation_is_incomplete() -> None:
    record = frame_bytes()

    with pytest.raises(IncompleteFrameError):
        decode_frame(record[:-24], HMAC_KEY)

    with pytest.raises(IncompleteFrameError):
        decode_frame(record[:-1], HMAC_KEY)


def test_malformed_committed_record_is_integrity_failure() -> None:
    record = frame_bytes()
    malformed = record.replace(b'"sequence":1', b'"sequence"::', 1)

    with pytest.raises(JournalIntegrityError):
        decode_frame(malformed, HMAC_KEY)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("magic", "NOT-GHOSTAP"),
        ("schema_version", 999),
    ],
)
def test_unknown_frame_format_fails_closed(field: str, value: object) -> None:
    with pytest.raises(JournalIntegrityError):
        decode_frame(mutate(frame_bytes(), field, value), HMAC_KEY)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sequence", True),
        ("writer_epoch", -1),
        ("timestamp", "1750000000.25"),
        ("expected_versions", []),
        ("aggregate_versions", {"goal_1": True}),
        ("previous_hash", "not-a-hash"),
        ("events", {}),
        ("checksum", 7),
    ],
)
def test_frame_fields_are_strictly_typed(field: str, value: object) -> None:
    with pytest.raises(JournalIntegrityError):
        decode_frame(mutate(frame_bytes(), field, value), HMAC_KEY)


def test_hmac_key_must_be_at_least_32_bytes() -> None:
    with pytest.raises(ValueError, match="32"):
        frame_bytes(hmac_key=b"too-short")

    with pytest.raises(ValueError, match="32"):
        decode_frame(frame_bytes(), b"too-short")


def test_memory_anchor_compare_and_swap_prevents_rollback_and_forks() -> None:
    anchor = MemoryAnchor()
    first_hash = "1" * 64
    fork_hash = "2" * 64

    assert anchor.read() == AnchorState(sequence=0, frame_hash=GENESIS_HASH)
    assert anchor.compare_and_swap(0, "f" * 64, 1, first_hash) is False
    assert anchor.compare_and_swap(0, GENESIS_HASH, 1, first_hash) is True
    assert anchor.read() == AnchorState(sequence=1, frame_hash=first_hash)
    assert anchor.compare_and_swap(0, GENESIS_HASH, 1, fork_hash) is False
    assert anchor.compare_and_swap(1, first_hash, 1, fork_hash) is False
    assert anchor.compare_and_swap(1, first_hash, 0, GENESIS_HASH) is False
    assert anchor.compare_and_swap(1, first_hash, 3, fork_hash) is False
    assert anchor.read() == AnchorState(sequence=1, frame_hash=first_hash)


def test_memory_anchor_allows_only_one_concurrent_branch() -> None:
    anchor = MemoryAnchor()
    barrier = threading.Barrier(3)
    candidate_hashes = ("a" * 64, "b" * 64)

    def advance(candidate_hash: str) -> bool:
        barrier.wait()
        return anchor.compare_and_swap(0, GENESIS_HASH, 1, candidate_hash)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(advance, candidate_hash) for candidate_hash in candidate_hashes]
        barrier.wait()
        results = [future.result() for future in futures]

    assert sorted(results) == [False, True]
    assert anchor.read() in {
        AnchorState(sequence=1, frame_hash=candidate_hash)
        for candidate_hash in candidate_hashes
    }
