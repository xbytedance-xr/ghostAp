"""Stable DTO and protocol contracts for engine snapshots."""

import dataclasses
from typing import Optional

import pytest

from src.card.engine_snapshot import EngineSnapshot, Snapshotable


def test_engine_snapshot_defaults_are_stable() -> None:
    snapshot = EngineSnapshot()

    assert snapshot.engine_name == ""
    assert snapshot.root_path == ""
    assert snapshot.project_id == ""
    assert snapshot.status == ""
    assert snapshot.is_running is False
    assert snapshot.duration_seconds is None
    assert snapshot.ext == {}


def test_engine_snapshot_is_immutable() -> None:
    snapshot = EngineSnapshot(engine_name="worktree")

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.engine_name = "deep"  # type: ignore[misc]


def test_engine_snapshot_ext_default_is_not_shared() -> None:
    first = EngineSnapshot()
    second = EngineSnapshot()

    assert first.ext is not second.ext


def test_snapshotable_protocol_accepts_complete_shape() -> None:
    class CompleteManager:
        def snapshot(self, chat_id: str, root_path: str) -> Optional[EngineSnapshot]:
            return EngineSnapshot(engine_name="fake")

        def snapshot_active(self, chat_id: str) -> list[EngineSnapshot]:
            return []

    assert isinstance(CompleteManager(), Snapshotable)


def test_snapshotable_protocol_rejects_incomplete_shape() -> None:
    class IncompleteManager:
        def snapshot(self, chat_id: str, root_path: str) -> Optional[EngineSnapshot]:
            return None

    assert not isinstance(IncompleteManager(), Snapshotable)
