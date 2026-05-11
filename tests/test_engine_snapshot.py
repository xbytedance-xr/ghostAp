"""Tests for src/card/engine_snapshot.py — EngineSnapshot DTO and Snapshotable protocol."""

import dataclasses
from typing import Optional

import pytest

from src.card.engine_snapshot import EngineSnapshot, Snapshotable


class TestEngineSnapshotInstantiation:
    """Verify EngineSnapshot can be created with default and full parameters."""

    def test_default_instantiation(self):
        """All fields have defaults; empty constructor should work."""
        snap = EngineSnapshot()
        assert snap.engine_name == ""
        assert snap.root_path == ""
        assert snap.project_id == ""
        assert snap.tool_calls_count == 0
        assert snap.completed_steps == 0
        assert snap.total_steps == 0
        assert snap.satisfied_count == 0
        assert snap.total_criteria == 0
        assert snap.duration_seconds is None
        assert snap.status == ""
        assert snap.is_running is False
        assert snap.iteration_count == 0
        assert snap.cycle_count == 0
        assert snap.cycle_count_total == 0
        assert snap.ext == {}

    def test_full_instantiation(self):
        """All fields can be set via constructor."""
        snap = EngineSnapshot(
            engine_name="deep",
            root_path="/work/project",
            project_id="proj_123",
            tool_calls_count=42,
            completed_steps=8,
            total_steps=10,
            satisfied_count=3,
            total_criteria=5,
            duration_seconds=123.4,
            status="running",
            is_running=True,
            iteration_count=2,
            cycle_count=1,
            cycle_count_total=3,
            ext={"key": "value"},
        )
        assert snap.engine_name == "deep"
        assert snap.root_path == "/work/project"
        assert snap.project_id == "proj_123"
        assert snap.tool_calls_count == 42
        assert snap.completed_steps == 8
        assert snap.total_steps == 10
        assert snap.satisfied_count == 3
        assert snap.total_criteria == 5
        assert snap.duration_seconds == 123.4
        assert snap.status == "running"
        assert snap.is_running is True
        assert snap.iteration_count == 2
        assert snap.cycle_count == 1
        assert snap.cycle_count_total == 3
        assert snap.ext == {"key": "value"}


class TestEngineSnapshotFrozen:
    """Verify frozen=True prevents mutation."""

    def test_frozen_immutability(self):
        """Assigning to a frozen dataclass field should raise."""
        snap = EngineSnapshot(engine_name="worktree")
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.engine_name = "deep"

    def test_frozen_int_field(self):
        """Int fields also cannot be reassigned."""
        snap = EngineSnapshot(tool_calls_count=5)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.tool_calls_count = 10

    def test_frozen_bool_field(self):
        snap = EngineSnapshot(is_running=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snap.is_running = False


class TestEngineSnapshotExtField:
    """Verify ext dict is accessible and independent per instance."""

    def test_ext_field_access(self):
        """ext dict values are readable."""
        snap = EngineSnapshot(ext={"iteration_details": [1, 2, 3], "review": "passed"})
        assert snap.ext["iteration_details"] == [1, 2, 3]
        assert snap.ext["review"] == "passed"

    def test_ext_default_is_independent(self):
        """Each instance gets its own ext dict (not shared)."""
        snap1 = EngineSnapshot()
        snap2 = EngineSnapshot()
        assert snap1.ext is not snap2.ext


class TestSnapshotableProtocol:
    """Verify Snapshotable protocol is runtime_checkable and works with stubs."""

    def test_snapshotable_protocol_check(self):
        """A class with snapshot() and snapshot_active() satisfies Snapshotable."""

        class FakeManager:
            def snapshot(self, chat_id: str, root_path: str) -> Optional[EngineSnapshot]:
                return EngineSnapshot(engine_name="fake")

            def snapshot_active(self, chat_id: str) -> list[EngineSnapshot]:
                return [EngineSnapshot(engine_name="fake")]

        mgr = FakeManager()
        assert isinstance(mgr, Snapshotable)

    def test_incomplete_class_fails_snapshotable(self):
        """A class missing snapshot_active() should not satisfy Snapshotable."""

        class IncompleteManager:
            def snapshot(self, chat_id: str, root_path: str) -> Optional[EngineSnapshot]:
                return None

        mgr = IncompleteManager()
        assert not isinstance(mgr, Snapshotable)

    def test_snapshotable_stub_returns_correct_types(self):
        """Verify stub returns are the correct types."""

        class StubManager:
            def snapshot(self, chat_id: str, root_path: str) -> Optional[EngineSnapshot]:
                return EngineSnapshot(engine_name="test", root_path=root_path)

            def snapshot_active(self, chat_id: str) -> list[EngineSnapshot]:
                return []

        mgr = StubManager()
        result = mgr.snapshot("chat1", "/work")
        assert isinstance(result, EngineSnapshot)
        assert result.root_path == "/work"
        assert mgr.snapshot_active("chat1") == []
