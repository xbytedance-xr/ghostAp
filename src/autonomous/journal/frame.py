"""Authenticated transaction-frame encoding for the autonomous journal."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

FRAME_MAGIC = "GHOSTAP-JOURNAL"
FRAME_SCHEMA_VERSION = 1
COMMIT_MARKER = "COMMITTED"
UNCOMMITTED_MARKER = "UNCOMMITTED"
GENESIS_HASH = "0" * 64
_HEX_CHARS = frozenset("0123456789abcdef")


class JournalIntegrityError(ValueError):
    """A committed frame failed structural or cryptographic verification."""


class IncompleteFrameError(JournalIntegrityError):
    """The physical tail does not contain a complete committed frame."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _valid_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX_CHARS for char in value)
    )


def _require_hmac_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("journal hmac key must be at least 32 bytes")


@dataclass(frozen=True)
class JournalEvent:
    """A typed event whose payload is protected by a canonical hash."""

    event_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    timestamp: float = field(default_factory=time.time)
    payload_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, str) or not self.event_type:
            raise ValueError("event_type must be a non-empty string")
        if not isinstance(self.aggregate_id, str) or not self.aggregate_id:
            raise ValueError("aggregate_id must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be a mapping")
        if (
            isinstance(self.timestamp, bool)
            or not isinstance(self.timestamp, (int, float))
            or not math.isfinite(float(self.timestamp))
        ):
            raise ValueError("timestamp must be finite")
        computed = _sha256(_canonical_json(dict(self.payload)))
        if self.payload_hash and not hmac.compare_digest(self.payload_hash, computed):
            raise ValueError("event payload hash mismatch")
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "timestamp", float(self.timestamp))
        object.__setattr__(self, "payload_hash", computed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "aggregate_id": self.aggregate_id,
            "payload": dict(self.payload),
            "timestamp": self.timestamp,
            "payload_hash": self.payload_hash,
        }

    @property
    def entry_type(self) -> str:
        """Legacy alias retained while callers migrate to JournalEvent."""
        return self.event_type

    @property
    def entity_id(self) -> str:
        """Legacy alias retained while callers migrate to aggregate_id."""
        return self.aggregate_id

    @property
    def data(self) -> Mapping[str, Any]:
        """Legacy alias retained while callers migrate to payload."""
        return self.payload

    @classmethod
    def from_dict(cls, value: Any) -> JournalEvent:
        if not isinstance(value, dict) or set(value) != {
            "event_type",
            "aggregate_id",
            "payload",
            "timestamp",
            "payload_hash",
        }:
            raise JournalIntegrityError("invalid journal event envelope")
        try:
            return cls(
                event_type=value["event_type"],
                aggregate_id=value["aggregate_id"],
                payload=value["payload"],
                timestamp=value["timestamp"],
                payload_hash=value["payload_hash"],
            )
        except (TypeError, ValueError) as exc:
            raise JournalIntegrityError(str(exc)) from exc


def _version_map(value: Any, field_name: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise JournalIntegrityError(f"{field_name} must be a mapping")
    result: dict[str, int] = {}
    for key, version in value.items():
        if (
            not isinstance(key, str)
            or not key
            or isinstance(version, bool)
            or not isinstance(version, int)
            or version < 0
        ):
            raise JournalIntegrityError(f"invalid {field_name}")
        result[key] = version
    return result


def _security_payload(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in envelope.items()
        if key not in {"checksum", "hmac_digest"}
    }


def _with_digests(envelope: dict[str, Any], key: bytes) -> dict[str, Any]:
    secured = dict(envelope)
    checksum = _sha256(_canonical_json(_security_payload(secured)))
    secured["checksum"] = checksum
    hmac_payload = {
        key_name: value
        for key_name, value in secured.items()
        if key_name != "hmac_digest"
    }
    secured["hmac_digest"] = hmac.new(
        key,
        _canonical_json(hmac_payload),
        hashlib.sha256,
    ).hexdigest()
    return secured


def _encode_with_stable_length(envelope: dict[str, Any], key: bytes) -> bytes:
    length = 0
    for _ in range(8):
        candidate = dict(envelope)
        candidate["byte_length"] = length
        candidate = _with_digests(candidate, key)
        record = _canonical_json(candidate) + b"\n"
        new_length = len(record)
        if new_length == length:
            return record
        length = new_length
    raise RuntimeError("failed to stabilize transaction frame length")


@dataclass(frozen=True)
class TransactionFrame:
    """A fully authenticated physical journal transaction."""

    magic: str
    schema_version: int
    byte_length: int
    commit_marker: str
    committed: bool
    tx_id: str
    sequence: int
    writer_epoch: int
    timestamp: float
    expected_versions: Mapping[str, int]
    aggregate_versions: Mapping[str, int]
    previous_hash: str
    events: tuple[JournalEvent, ...]
    checksum: str
    hmac_digest: str
    _record: bytes = field(repr=False, compare=False)

    @classmethod
    def seal(
        cls,
        *,
        tx_id: str,
        sequence: int,
        writer_epoch: int,
        timestamp: float,
        expected_versions: Mapping[str, int],
        aggregate_versions: Mapping[str, int],
        previous_hash: str,
        events: tuple[JournalEvent, ...],
        hmac_key: bytes,
        committed: bool = True,
    ) -> TransactionFrame:
        _require_hmac_key(hmac_key)
        if not isinstance(tx_id, str) or not tx_id:
            raise ValueError("tx_id must be a non-empty string")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ValueError("sequence must be a positive integer")
        if (
            isinstance(writer_epoch, bool)
            or not isinstance(writer_epoch, int)
            or writer_epoch < 0
        ):
            raise ValueError("writer_epoch must be a non-negative integer")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
        ):
            raise ValueError("timestamp must be finite")
        if not _valid_hash(previous_hash):
            raise ValueError("previous_hash must be sha256 hex")
        if not isinstance(events, tuple) or not events:
            raise ValueError("events must be a non-empty tuple")
        expected = _version_map(dict(expected_versions), "expected_versions")
        aggregate = _version_map(dict(aggregate_versions), "aggregate_versions")
        envelope = {
            "magic": FRAME_MAGIC,
            "schema_version": FRAME_SCHEMA_VERSION,
            "byte_length": 0,
            "commit_marker": COMMIT_MARKER if committed else UNCOMMITTED_MARKER,
            "committed": bool(committed),
            "tx_id": tx_id,
            "sequence": sequence,
            "writer_epoch": writer_epoch,
            "timestamp": float(timestamp),
            "expected_versions": expected,
            "aggregate_versions": aggregate,
            "previous_hash": previous_hash,
            "events": [event.to_dict() for event in events],
        }
        record = _encode_with_stable_length(envelope, hmac_key)
        return decode_frame(record, hmac_key, allow_uncommitted=True)

    @property
    def frame_hash(self) -> str:
        return self.record_hash(self._record)

    @property
    def frame_id(self) -> str:
        """Legacy alias retained while callers migrate to tx_id."""
        return self.tx_id

    @property
    def prev_hash(self) -> str:
        """Legacy alias retained while callers migrate to previous_hash."""
        return self.previous_hash

    @property
    def entries(self) -> tuple[JournalEvent, ...]:
        """Legacy alias retained while callers migrate to events."""
        return self.events

    @staticmethod
    def record_hash(record: bytes) -> str:
        return _sha256(record)

    def to_bytes(self) -> bytes:
        return self._record


def decode_frame(
    record: bytes,
    hmac_key: bytes,
    *,
    allow_uncommitted: bool = False,
) -> TransactionFrame:
    """Decode and authenticate one physical transaction frame."""

    _require_hmac_key(hmac_key)
    if not isinstance(record, bytes) or not record:
        raise IncompleteFrameError("empty or truncated transaction frame")
    has_newline = record.endswith(b"\n")
    try:
        envelope = json.loads(record)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if not has_newline:
            raise IncompleteFrameError("truncated transaction frame") from exc
        raise JournalIntegrityError("malformed committed transaction frame") from exc
    if not isinstance(envelope, dict):
        raise JournalIntegrityError("transaction frame must be an object")
    required = {
        "magic",
        "schema_version",
        "byte_length",
        "commit_marker",
        "committed",
        "tx_id",
        "sequence",
        "writer_epoch",
        "timestamp",
        "expected_versions",
        "aggregate_versions",
        "previous_hash",
        "events",
        "checksum",
        "hmac_digest",
    }
    if set(envelope) != required:
        raise JournalIntegrityError("invalid transaction frame fields")
    if envelope["magic"] != FRAME_MAGIC:
        raise JournalIntegrityError("invalid transaction frame magic")
    if envelope["schema_version"] != FRAME_SCHEMA_VERSION:
        raise JournalIntegrityError("unsupported transaction frame schema")
    committed = envelope["committed"]
    if not isinstance(committed, bool):
        raise JournalIntegrityError("committed must be boolean")
    expected_marker = COMMIT_MARKER if committed else UNCOMMITTED_MARKER
    if envelope["commit_marker"] != expected_marker:
        raise JournalIntegrityError("invalid commit marker")
    if not isinstance(envelope["tx_id"], str) or not envelope["tx_id"]:
        raise JournalIntegrityError("invalid tx_id")
    sequence = envelope["sequence"]
    writer_epoch = envelope["writer_epoch"]
    timestamp = envelope["timestamp"]
    byte_length = envelope["byte_length"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise JournalIntegrityError("invalid sequence")
    if (
        isinstance(writer_epoch, bool)
        or not isinstance(writer_epoch, int)
        or writer_epoch < 0
    ):
        raise JournalIntegrityError("invalid writer_epoch")
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(float(timestamp))
    ):
        raise JournalIntegrityError("invalid timestamp")
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 1:
        raise JournalIntegrityError("invalid byte length")
    if len(record) != byte_length:
        if len(record) < byte_length and not has_newline:
            raise IncompleteFrameError("transaction frame length is incomplete")
        raise JournalIntegrityError("transaction frame length mismatch")
    expected_versions = _version_map(
        envelope["expected_versions"],
        "expected_versions",
    )
    aggregate_versions = _version_map(
        envelope["aggregate_versions"],
        "aggregate_versions",
    )
    if not _valid_hash(envelope["previous_hash"]):
        raise JournalIntegrityError("invalid previous_hash")
    if not isinstance(envelope["events"], list) or not envelope["events"]:
        raise JournalIntegrityError("invalid events")
    events = tuple(JournalEvent.from_dict(value) for value in envelope["events"])
    if not _valid_hash(envelope["checksum"]) or not _valid_hash(envelope["hmac_digest"]):
        raise JournalIntegrityError("invalid frame digest")
    expected_checksum = _sha256(_canonical_json(_security_payload(envelope)))
    if not hmac.compare_digest(envelope["checksum"], expected_checksum):
        raise JournalIntegrityError("transaction frame checksum mismatch")
    hmac_payload = {
        key: value
        for key, value in envelope.items()
        if key != "hmac_digest"
    }
    expected_hmac = hmac.new(
        hmac_key,
        _canonical_json(hmac_payload),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(envelope["hmac_digest"], expected_hmac):
        raise JournalIntegrityError("transaction frame hmac mismatch")
    if not has_newline:
        raise IncompleteFrameError("transaction frame is missing delimiter")
    if not committed and not allow_uncommitted:
        raise IncompleteFrameError("transaction frame is not committed")
    return TransactionFrame(
        magic=envelope["magic"],
        schema_version=envelope["schema_version"],
        byte_length=byte_length,
        commit_marker=envelope["commit_marker"],
        committed=committed,
        tx_id=envelope["tx_id"],
        sequence=sequence,
        writer_epoch=writer_epoch,
        timestamp=float(timestamp),
        expected_versions=expected_versions,
        aggregate_versions=aggregate_versions,
        previous_hash=envelope["previous_hash"],
        events=events,
        checksum=envelope["checksum"],
        hmac_digest=envelope["hmac_digest"],
        _record=record,
    )
