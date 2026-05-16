"""Task list sub-reducer."""
from __future__ import annotations

from dataclasses import replace

from ...events import CardEvent, CardEventType
from ..models import CardState, TaskListBlock

TASK_LIST_BLOCK_ID = "_task_list"


def reduce_task_list(state: CardState, event: CardEvent) -> CardState:
    """Handle TASK_LIST_UPDATED — upsert TaskListBlock at position 0."""
    if event.type != CardEventType.TASK_LIST_UPDATED:
        return state

    payload = event.payload or {}
    tasks_data = payload.get("tasks", [])
    current_task_id = payload.get("current_task_id", "")

    # Convert list[dict] to tuple for frozen dataclass
    tasks_tuple = tuple(tasks_data)

    # Remove existing task_list block
    blocks = [b for b in state.blocks if not (b.block_id == TASK_LIST_BLOCK_ID and b.kind == "task_list")]

    # Always insert at position 0 (before PlanBlock and everything else)
    new_block = TaskListBlock(
        block_id=TASK_LIST_BLOCK_ID,
        tasks=tasks_tuple,
        current_task_id=current_task_id,
        status="active",
    )
    blocks.insert(0, new_block)
    return replace(state, blocks=tuple(blocks))
