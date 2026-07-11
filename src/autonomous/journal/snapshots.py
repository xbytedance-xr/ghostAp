"""Snapshot publication with atomic fsync-replace-directory-fsync protocol."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .projections import ProjectionState


@dataclass(frozen=True)
class SnapshotMetadata:
    """Records the source journal position and integrity hash."""

    source_sequence: int
    source_hash: str
    snapshot_hash: str
    created_at: float


class SnapshotError(RuntimeError):
    """Failed to publish or load a snapshot."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fsync_file(fd_or_file: Any) -> None:
    """Fsync a file descriptor or file object."""
    fd = fd_or_file if isinstance(fd_or_file, int) else fd_or_file.fileno()
    os.fsync(fd)


def _fsync_directory(directory: str | Path) -> None:
    """Fsync a directory to ensure metadata durability."""
    fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _serialize_state(state: ProjectionState) -> dict[str, Any]:
    """Convert projection state to a JSON-serializable dict."""
    return {
        "goals": {
            goal_id: goal.to_dict()
            for goal_id, goal in state.goals.items()
        },
        "runs": {
            run_id: run.to_dict()
            for run_id, run in state.runs.items()
        },
        "plans": {
            plan_id: plan.to_dict()
            for plan_id, plan in state.plans.items()
        },
        "steps": {
            step_id: step.to_dict()
            for step_id, step in state.steps.items()
        },
        "effects": {
            effect_id: effect.to_dict()
            for effect_id, effect in state.effects.items()
        },
        "inbox": {
            event_id: {
                "event_id": record.event_id,
                "dedup_key": record.dedup_key,
                "source_type": record.source_type,
                "payload": record.payload,
                "received_at": record.received_at,
                "processed": record.processed,
                "goal_id": record.goal_id,
                "run_id": record.run_id,
                "tombstone": record.tombstone,
            }
            for event_id, record in state.inbox.items()
        },
        "dedup_keys": sorted(state.dedup_keys),
        "occurrence_keys": sorted(state.occurrence_keys),
        "cursor_sequence": state.cursor_sequence,
        "cursor_hash": state.cursor_hash,
    }


class SnapshotStore:
    """Publishes and loads projection snapshots with crash-safe durability.

    Publication protocol:
    1. Write snapshot to a temporary file in the snapshot directory
    2. fsync the temporary file
    3. Atomic rename (replace) over the target path
    4. fsync the directory to ensure the rename is durable
    """

    SNAPSHOT_FILENAME = "projection_snapshot.json"

    def __init__(self, snapshot_dir: str | Path) -> None:
        self._dir = Path(snapshot_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def snapshot_path(self) -> Path:
        return self._dir / self.SNAPSHOT_FILENAME

    def publish(
        self,
        state: ProjectionState,
        *,
        source_sequence: int,
        source_hash: str,
        created_at: float,
    ) -> SnapshotMetadata:
        """Atomically publish a snapshot with full fsync durability.

        Protocol:
        1. Serialize state to canonical JSON
        2. Write to temp file + fsync
        3. Atomic replace (os.replace)
        4. Directory fsync
        """
        serialized = _serialize_state(state)
        payload_bytes = _canonical_json(serialized)
        snapshot_hash = _sha256(payload_bytes)

        metadata = SnapshotMetadata(
            source_sequence=source_sequence,
            source_hash=source_hash,
            snapshot_hash=snapshot_hash,
            created_at=created_at,
        )

        envelope = {
            "metadata": {
                "source_sequence": metadata.source_sequence,
                "source_hash": metadata.source_hash,
                "snapshot_hash": metadata.snapshot_hash,
                "created_at": metadata.created_at,
            },
            "state": serialized,
        }

        envelope_bytes = _canonical_json(envelope) + b"\n"

        # Step 1: Write to temp file
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._dir),
                prefix=".snapshot_",
                suffix=".tmp",
            )
            os.write(fd, envelope_bytes)

            # Step 2: fsync the file
            _fsync_file(fd)
            os.close(fd)
            fd = None

            # Step 3: Atomic replace
            os.replace(tmp_path, str(self.snapshot_path))
            tmp_path = None

            # Step 4: Directory fsync
            _fsync_directory(self._dir)

        except BaseException:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

        return metadata

    def load(self) -> tuple[SnapshotMetadata, dict[str, Any]] | None:
        """Load the latest snapshot if available.

        Returns (metadata, state_dict) or None if no snapshot exists.
        Validates integrity using the stored snapshot_hash.
        """
        if not self.snapshot_path.exists():
            return None

        try:
            raw = self.snapshot_path.read_bytes()
            envelope = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"failed to read snapshot: {exc}") from exc

        if not isinstance(envelope, dict) or "metadata" not in envelope:
            raise SnapshotError("invalid snapshot envelope")

        meta_dict = envelope["metadata"]
        state_dict = envelope["state"]

        # Verify integrity
        payload_bytes = _canonical_json(state_dict)
        computed_hash = _sha256(payload_bytes)

        if computed_hash != meta_dict["snapshot_hash"]:
            raise SnapshotError("snapshot hash mismatch - data corrupted")

        metadata = SnapshotMetadata(
            source_sequence=meta_dict["source_sequence"],
            source_hash=meta_dict["source_hash"],
            snapshot_hash=meta_dict["snapshot_hash"],
            created_at=meta_dict["created_at"],
        )

        return metadata, state_dict

    def exists(self) -> bool:
        """Check if a snapshot file exists."""
        return self.snapshot_path.exists()

    def delete(self) -> None:
        """Remove the snapshot file if it exists."""
        if self.snapshot_path.exists():
            os.unlink(str(self.snapshot_path))
            _fsync_directory(self._dir)
