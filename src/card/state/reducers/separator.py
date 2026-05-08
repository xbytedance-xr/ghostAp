"""Section separator sub-reducer."""

from __future__ import annotations

from dataclasses import replace

from ..models import CardState, SeparatorBlock
from ...events import CardEvent, CardEventType


def reduce_separator(state: CardState, event: CardEvent) -> CardState:
    """Handle SECTION_SEPARATOR — insert a visual divider for overflow tasks."""
    if event.type is not CardEventType.SECTION_SEPARATOR:
        return state

    block_id = event.payload.get("block_id", "")
    task_name = event.payload.get("task_name", "")
    is_first_overflow = event.payload.get("is_first_overflow", False)
    status_emoji = event.payload.get("status_emoji", "⏳")

    new_block = SeparatorBlock(
        block_id=block_id,
        task_name=task_name,
        is_first_overflow=is_first_overflow,
        status_emoji=status_emoji,
        element_id=f"el_{block_id}",
        status="completed",
    )
    return replace(state, blocks=state.blocks + (new_block,))
