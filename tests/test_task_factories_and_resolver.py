"""Tests for task factory helpers and TaskIdResolver."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pytest

from src.card.orchestrator import TaskIdResolver
from src.card.task_registry import tasks_from_plan_entries, tasks_from_spec_tasks


# ---------------------------------------------------------------------------
# Fake models for testing (avoid importing full ACP/Spec dependencies)
# ---------------------------------------------------------------------------


@dataclass
class FakePlanEntry:
    content: str
    priority: str = "medium"
    status: str = "pending"


@dataclass
class FakePlanInfo:
    entries: list = field(default_factory=list)


class FakeSpecTaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class FakeSpecTask:
    task_id: int
    description: str
    status: FakeSpecTaskStatus = FakeSpecTaskStatus.PENDING
    dependencies: list = field(default_factory=list)
    output: str = ""


class FakeACPEventType(Enum):
    PLAN_UPDATE = "plan_update"
    TEXT_CHUNK = "text_chunk"
    TOOL_CALL_START = "tool_call_start"


@dataclass
class FakeACPEvent:
    event_type: FakeACPEventType
    plan: FakePlanInfo | None = None
    text: str | None = None


# ---------------------------------------------------------------------------
# Tests: tasks_from_plan_entries
# ---------------------------------------------------------------------------


class TestTasksFromPlanEntries:
    def test_basic_conversion(self):
        entries = [
            FakePlanEntry(content="Analyze requirements"),
            FakePlanEntry(content="Write code"),
            FakePlanEntry(content="Run tests"),
        ]
        result = tasks_from_plan_entries(entries)
        assert len(result) == 3
        assert result[0] == {"task_id": "step_0", "name": "Analyze requirements", "status": "pending"}
        assert result[1] == {"task_id": "step_1", "name": "Write code", "status": "pending"}
        assert result[2] == {"task_id": "step_2", "name": "Run tests", "status": "pending"}

    def test_preserves_status(self):
        entries = [
            FakePlanEntry(content="Done task", status="completed"),
            FakePlanEntry(content="Active task", status="in_progress"),
        ]
        result = tasks_from_plan_entries(entries)
        assert result[0]["status"] == "completed"
        assert result[1]["status"] == "in_progress"

    def test_skips_empty_content(self):
        entries = [
            FakePlanEntry(content=""),
            FakePlanEntry(content="  "),
            FakePlanEntry(content="Valid task"),
        ]
        result = tasks_from_plan_entries(entries)
        assert len(result) == 1
        assert result[0]["name"] == "Valid task"

    def test_truncates_long_names(self):
        entries = [FakePlanEntry(content="x" * 200)]
        result = tasks_from_plan_entries(entries)
        assert len(result[0]["name"]) == 120

    def test_invalid_status_defaults_to_pending(self):
        entries = [FakePlanEntry(content="Task", status="unknown_status")]
        result = tasks_from_plan_entries(entries)
        assert result[0]["status"] == "pending"

    def test_empty_list(self):
        result = tasks_from_plan_entries([])
        assert result == []


# ---------------------------------------------------------------------------
# Tests: tasks_from_spec_tasks
# ---------------------------------------------------------------------------


class TestTasksFromSpecTasks:
    def test_basic_conversion(self):
        tasks = [
            FakeSpecTask(task_id=1, description="Implement feature A"),
            FakeSpecTask(task_id=2, description="Fix bug B"),
        ]
        result = tasks_from_spec_tasks(tasks)
        assert len(result) == 2
        assert result[0] == {"task_id": "spec_task_1", "name": "Implement feature A", "status": "pending"}
        assert result[1] == {"task_id": "spec_task_2", "name": "Fix bug B", "status": "pending"}

    def test_preserves_status(self):
        tasks = [
            FakeSpecTask(task_id=1, description="Done", status=FakeSpecTaskStatus.COMPLETED),
            FakeSpecTask(task_id=2, description="Active", status=FakeSpecTaskStatus.IN_PROGRESS),
        ]
        result = tasks_from_spec_tasks(tasks)
        assert result[0]["status"] == "completed"
        assert result[1]["status"] == "in_progress"

    def test_skips_empty_description(self):
        tasks = [
            FakeSpecTask(task_id=1, description=""),
            FakeSpecTask(task_id=2, description="  "),
            FakeSpecTask(task_id=3, description="Valid"),
        ]
        result = tasks_from_spec_tasks(tasks)
        assert len(result) == 1
        assert result[0]["task_id"] == "spec_task_3"

    def test_truncates_long_descriptions(self):
        tasks = [FakeSpecTask(task_id=1, description="y" * 200)]
        result = tasks_from_spec_tasks(tasks)
        assert len(result[0]["name"]) == 120

    def test_empty_list(self):
        result = tasks_from_spec_tasks([])
        assert result == []


# ---------------------------------------------------------------------------
# Tests: TaskIdResolver
# ---------------------------------------------------------------------------


class TestTaskIdResolver:
    def test_initial_state(self):
        resolver = TaskIdResolver(["t1", "t2", "t3"])
        assert resolver.current_task_id == "t1"

    def test_advance_to(self):
        resolver = TaskIdResolver(["t1", "t2", "t3"])
        resolver.advance_to(1)
        assert resolver.current_task_id == "t2"
        resolver.advance_to(2)
        assert resolver.current_task_id == "t3"

    def test_advance_to_out_of_bounds(self):
        resolver = TaskIdResolver(["t1", "t2"])
        resolver.advance_to(5)  # out of bounds, should not change
        assert resolver.current_task_id == "t1"
        resolver.advance_to(-1)
        assert resolver.current_task_id == "t1"

    def test_resolve_without_event(self):
        resolver = TaskIdResolver(["t1", "t2"])
        assert resolver.resolve(None) == "t1"
        resolver.advance_to(1)
        assert resolver.resolve(None) == "t2"

    def test_resolve_with_plan_update(self):
        """resolve() advances when PLAN_UPDATE has in_progress entry."""
        resolver = TaskIdResolver(["step_0", "step_1", "step_2"])

        # Simulate PLAN_UPDATE with second step in_progress
        # We need to patch ACPEventType for the resolve method
        from unittest.mock import patch, MagicMock

        # Create a mock that mimics the real ACPEvent structure
        mock_event = MagicMock()
        mock_event.event_type = MagicMock()
        mock_event.plan = FakePlanInfo(entries=[
            FakePlanEntry(content="Step 1", status="completed"),
            FakePlanEntry(content="Step 2", status="in_progress"),
            FakePlanEntry(content="Step 3", status="pending"),
        ])

        # Patch ACPEventType.PLAN_UPDATE to match the mock's event_type
        with patch("src.acp.models.ACPEventType") as MockACPEventType:
            MockACPEventType.PLAN_UPDATE = mock_event.event_type
            result = resolver.resolve(mock_event)

        assert result == "step_1"

    def test_mark_active(self):
        resolver = TaskIdResolver(["t1", "t2", "t3"])
        resolver.mark_active("t2")
        assert resolver.current_task_id == "t2"

    def test_mark_active_unknown_id(self):
        resolver = TaskIdResolver(["t1", "t2"])
        resolver.mark_active("unknown")  # should not change
        assert resolver.current_task_id == "t1"

    def test_empty_task_ids(self):
        resolver = TaskIdResolver([])
        assert resolver.current_task_id == ""
        assert resolver.resolve(None) == ""

    # --- mark_inactive tests (AC15) ---

    def test_mark_inactive_fallback_to_most_recent(self):
        """When the active task is deactivated, falls back to the most recently activated remaining task."""
        import time as _time
        resolver = TaskIdResolver(["t1", "t2", "t3"])
        resolver.mark_active("t1")
        _time.sleep(0.01)
        resolver.mark_active("t2")
        _time.sleep(0.01)
        resolver.mark_active("t3")
        assert resolver.current_task_id == "t3"

        # Deactivate t3 → should fall back to t2 (most recently activated remaining)
        resolver.mark_inactive("t3")
        assert resolver.current_task_id == "t2"
        assert "t3" not in resolver.active_task_ids

    def test_mark_inactive_fallback_preserves_order(self):
        """When multiple tasks are active and one is deactivated, picks by activation time."""
        import time as _time
        resolver = TaskIdResolver(["t1", "t2", "t3"])
        resolver.mark_active("t1")
        _time.sleep(0.01)
        resolver.mark_active("t3")
        _time.sleep(0.01)
        resolver.mark_active("t2")  # t2 was activated last

        # Deactivate t2 → should fall back to t3 (next most recent)
        resolver.mark_inactive("t2")
        assert resolver.current_task_id == "t3"

    def test_mark_inactive_no_remaining_keeps_last_index(self):
        """When all active tasks are deactivated, keeps last known index/id unchanged."""
        resolver = TaskIdResolver(["t1", "t2", "t3"])
        resolver.mark_active("t2")
        assert resolver.current_task_id == "t2"

        # Deactivate t2 — no remaining active tasks
        resolver.mark_inactive("t2")
        # Should keep "t2" as last_active_id (no fallback target)
        assert resolver.current_task_id == "t2"
        assert len(resolver.active_task_ids) == 0

    def test_mark_inactive_non_active_task_is_noop(self):
        """Deactivating a task that isn't active should be a no-op."""
        resolver = TaskIdResolver(["t1", "t2", "t3"])
        resolver.mark_active("t1")
        resolver.mark_inactive("t2")
        assert resolver.current_task_id == "t1"
        assert "t1" in resolver.active_task_ids

    def test_mark_inactive_unknown_task_is_noop(self):
        """Deactivating an unknown task_id should be safe."""
        resolver = TaskIdResolver(["t1", "t2"])
        resolver.mark_active("t1")
        resolver.mark_inactive("unknown_id")
        assert resolver.current_task_id == "t1"
