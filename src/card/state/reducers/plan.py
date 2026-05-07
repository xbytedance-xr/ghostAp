"""Plan sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, PlanBlock
from ...events import CardEvent, CardEventType

PLAN_BLOCK_ID = "_plan"


def reduce_plan(state: CardState, event: CardEvent) -> CardState:
    """Handle PLAN_UPDATED."""
    if event.type != CardEventType.PLAN_UPDATED:
        return state

    content = event.payload.get("content", "")
    blocks = [b for b in state.blocks if not (b.block_id == PLAN_BLOCK_ID and b.kind == "plan")]

    # Keep the plan panel at the beginning of the card so task context stays visible.
    new_block = PlanBlock(block_id=PLAN_BLOCK_ID, status="active", content=content)
    blocks.insert(0, new_block)
    return replace(state, blocks=tuple(blocks))
