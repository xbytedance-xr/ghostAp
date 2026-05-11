"""Text block sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, TextBlock, FooterState
from ...events import CardEvent, CardEventType


def reduce_text(state: CardState, event: CardEvent) -> CardState:
    """Handle TEXT_STARTED / TEXT_DELTA / TEXT_DONE."""
    match event.type:
        case CardEventType.TEXT_STARTED:
            block_id = event.payload.get("block_id", "")
            new_block = TextBlock(block_id=block_id, status="active", element_id=f"el_{block_id}")
            return replace(state, blocks=state.blocks + (new_block,),
                           footer=replace(state.footer, status="thinking", status_text="💭 正在思考..."))

        case CardEventType.TEXT_DELTA:
            block_id = event.payload.get("block_id", "")
            text = event.payload.get("text", "")
            # O(1) lookup via block_index
            idx = state.block_index.get(block_id)
            if idx is not None and idx < len(state.blocks) and state.blocks[idx].kind == "text":
                b = state.blocks[idx]
                # Strip leading newlines on first chunk to prevent first char wrapping
                if not b.content:
                    text = text.lstrip("\n")
                updated = replace(b, content=b.content + text)
                blocks = state.blocks[:idx] + (updated,) + state.blocks[idx + 1:]
                return replace(state, blocks=blocks)
            # Auto-create block for convenience (from_acp uses "_active_text").
            text = text.lstrip("\n")
            new_block = TextBlock(block_id=block_id, status="active",
                                     element_id=f"el_{block_id}", content=text)
            return replace(state, blocks=state.blocks + (new_block,),
                           footer=replace(state.footer, status="thinking", status_text="💭 正在思考..."))

        case CardEventType.TEXT_DONE:
            block_id = event.payload.get("block_id", "")
            idx = state.block_index.get(block_id)
            if idx is not None and idx < len(state.blocks) and state.blocks[idx].kind == "text":
                b = state.blocks[idx]
                updated = replace(b, status="completed", element_id=None)
                blocks = state.blocks[:idx] + (updated,) + state.blocks[idx + 1:]
                return replace(state, blocks=blocks,
                               footer=replace(state.footer, status=None, status_text=None))
            return replace(state, footer=replace(state.footer, status=None, status_text=None))

    return state
