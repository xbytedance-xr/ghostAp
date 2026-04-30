"""Text block sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, ContentBlock, FooterState
from ...events import CardEvent, CardEventType


def reduce_text(state: CardState, event: CardEvent) -> CardState:
    """Handle TEXT_STARTED / TEXT_DELTA / TEXT_DONE."""
    match event.type:
        case CardEventType.TEXT_STARTED:
            block_id = event.payload.get("block_id", "")
            new_block = ContentBlock(kind="text", block_id=block_id, status="active", element_id=f"el_{block_id}")
            return replace(state, blocks=state.blocks + (new_block,),
                           footer=replace(state.footer, status="thinking", status_text="💭 正在思考..."))

        case CardEventType.TEXT_DELTA:
            block_id = event.payload.get("block_id", "")
            text = event.payload.get("text", "")
            blocks = list(state.blocks)
            # Find or create the active text block
            found = False
            for i, b in enumerate(blocks):
                if b.block_id == block_id and b.kind == "text":
                    blocks[i] = replace(b, content=b.content + text)
                    found = True
                    break
            if not found:
                # Auto-create block for convenience (from_acp uses "_active_text")
                new_block = ContentBlock(kind="text", block_id=block_id, status="active",
                                         element_id=f"el_{block_id}", content=text)
                blocks.append(new_block)
            return replace(state, blocks=tuple(blocks))

        case CardEventType.TEXT_DONE:
            block_id = event.payload.get("block_id", "")
            blocks = list(state.blocks)
            for i, b in enumerate(blocks):
                if b.block_id == block_id and b.kind == "text":
                    blocks[i] = replace(b, status="completed", element_id=None)
                    break
            return replace(state, blocks=tuple(blocks),
                           footer=replace(state.footer, status=None, status_text=None))

    return state
