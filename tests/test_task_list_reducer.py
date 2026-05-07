"""Tests for task_list reducer."""
from __future__ import annotations

from src.card.state.models import CardState, CardMetadata, TaskListBlock, PlanBlock
from src.card.state.reducer import reduce_card_state
from src.card.events import CardEvent, CardEventType


def _make_task_list_event(tasks, current_task_id="t1"):
    return CardEvent(
        type=CardEventType.TASK_LIST_UPDATED,
        payload={"tasks": tasks, "current_task_id": current_task_id},
    )


def _initial_state():
    return CardState(metadata=CardMetadata())


class TestTaskListReducer:
    def test_first_task_list_creates_block_at_position_0(self):
        """First TASK_LIST_UPDATED creates TaskListBlock at blocks[0]."""
        state = _initial_state()
        tasks = [
            {"task_id": "t1", "name": "Task 1", "status": "in_progress"},
            {"task_id": "t2", "name": "Task 2", "status": "pending"},
        ]
        new_state = reduce_card_state(state, _make_task_list_event(tasks))
        assert len(new_state.blocks) == 1
        assert isinstance(new_state.blocks[0], TaskListBlock)
        assert new_state.blocks[0].block_id == "_task_list"
        assert len(new_state.blocks[0].tasks) == 2
        assert new_state.blocks[0].current_task_id == "t1"

    def test_upsert_replaces_not_appends(self):
        """Repeated TASK_LIST_UPDATED replaces existing block, not appends."""
        state = _initial_state()
        tasks_v1 = [{"task_id": "t1", "name": "Task 1", "status": "pending"}]
        tasks_v2 = [
            {"task_id": "t1", "name": "Task 1", "status": "completed"},
            {"task_id": "t2", "name": "Task 2", "status": "in_progress"},
        ]
        state = reduce_card_state(state, _make_task_list_event(tasks_v1))
        state = reduce_card_state(state, _make_task_list_event(tasks_v2, "t2"))

        task_list_blocks = [b for b in state.blocks if isinstance(b, TaskListBlock)]
        assert len(task_list_blocks) == 1
        assert len(task_list_blocks[0].tasks) == 2
        assert task_list_blocks[0].current_task_id == "t2"

    def test_task_list_before_plan_block(self):
        """TaskListBlock stays at position 0 even with PlanBlock present."""
        state = _initial_state()
        # First add a plan
        plan_event = CardEvent(
            type=CardEventType.PLAN_UPDATED,
            payload={"content": "1. Do stuff"},
        )
        state = reduce_card_state(state, plan_event)
        assert isinstance(state.blocks[0], PlanBlock)

        # Then add task list
        tasks = [{"task_id": "t1", "name": "Task 1", "status": "in_progress"}]
        state = reduce_card_state(state, _make_task_list_event(tasks))

        # TaskListBlock should be at position 0, PlanBlock at position 1
        assert isinstance(state.blocks[0], TaskListBlock)
        assert isinstance(state.blocks[1], PlanBlock)

    def test_structural_version_bumped(self):
        """TASK_LIST_UPDATED bumps structural_version."""
        state = _initial_state()
        old_sv = state.structural_version
        tasks = [{"task_id": "t1", "name": "Task 1", "status": "pending"}]
        new_state = reduce_card_state(state, _make_task_list_event(tasks))
        assert new_state.structural_version > old_sv

    def test_empty_tasks_list(self):
        """Empty tasks list still creates block (valid state)."""
        state = _initial_state()
        new_state = reduce_card_state(state, _make_task_list_event([]))
        assert isinstance(new_state.blocks[0], TaskListBlock)
        assert new_state.blocks[0].tasks == ()
