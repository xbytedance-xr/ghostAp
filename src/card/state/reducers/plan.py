"""Plan sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, ContentBlock
from ...events import CardEvent, CardEventType

PLAN_BLOCK_ID = "_plan"


def reduce_plan(state: CardState, event: CardEvent) -> CardState:
    """Handle PLAN_UPDATED."""
    if event.type != CardEventType.PLAN_UPDATED:
        return state

    content = event.payload.get("content", "")
    blocks = list(state.blocks)

    # Find existing plan block and update, or create new
    for i, b in enumerate(blocks):
        if b.block_id == PLAN_BLOCK_ID and b.kind == "plan":
            blocks[i] = replace(b, content=content)
            return replace(state, blocks=tuple(blocks))

    # Create new plan block
    new_block = ContentBlock(kind="plan", block_id=PLAN_BLOCK_ID, status="active", content=content)
    blocks.append(new_block)
    return replace(state, blocks=tuple(blocks))
