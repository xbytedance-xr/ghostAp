"""Spec PLAN/TASK artifact reducers."""

from __future__ import annotations

from dataclasses import replace

from ...events import CardEvent, CardEventType
from ..models import CardState, SpecPlanBlock, SpecTaskBlock


def reduce_spec_artifacts(state: CardState, event: CardEvent) -> CardState:
    """Render structured Spec artifacts without falling back to raw model output."""
    match event.type:
        case CardEventType.SPEC_PLAN_UPDATED:
            cycle_num = int(event.payload.get("cycle_num") or 0)
            plan = event.payload.get("plan") or {}
            if not isinstance(plan, dict):
                plan = {}
            blocks = tuple(
                block for block in state.blocks
                if not (
                    block.kind == "spec_plan"
                    and int((getattr(block, "data", None) or {}).get("cycle_num") or 0) == cycle_num
                )
            )
            if not _has_plan_content(plan):
                return replace(state, blocks=blocks)
            data = dict(plan)
            data["cycle_num"] = cycle_num
            return replace(
                state,
                blocks=blocks + (SpecPlanBlock(block_id=f"spec_plan_{cycle_num}", data=data),),
            )

        case CardEventType.SPEC_TASKS_UPDATED:
            cycle_num = int(event.payload.get("cycle_num") or 0)
            tasks = event.payload.get("tasks") or []
            if not isinstance(tasks, list):
                tasks = []
            blocks = tuple(
                block for block in state.blocks
                if not (
                    block.kind == "spec_task"
                    and int((getattr(block, "data", None) or {}).get("cycle_num") or 0) == cycle_num
                )
            )

            task_blocks: list[SpecTaskBlock] = []
            for index, task in enumerate(tasks, start=1):
                if not isinstance(task, dict):
                    continue
                description = str(task.get("description") or "").strip()
                if not description:
                    continue
                task_id = task.get("task_id") or index
                data = dict(task)
                data["cycle_num"] = cycle_num
                data["task_index"] = index
                data["task_id"] = task_id
                safe_task_id = "".join(ch if ch.isalnum() else "_" for ch in str(task_id)).strip("_") or str(index)
                task_blocks.append(
                    SpecTaskBlock(
                        block_id=f"spec_task_{cycle_num}_{safe_task_id}",
                        data=data,
                    )
                )
            return replace(state, blocks=blocks + tuple(task_blocks))

    return state


def _has_plan_content(plan: dict) -> bool:
    if str(plan.get("architecture") or "").strip():
        return True
    for key in ("tech_stack", "steps", "file_changes", "test_plan", "risks", "notes"):
        value = plan.get(key)
        if isinstance(value, list) and any(str(item).strip() for item in value):
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False
