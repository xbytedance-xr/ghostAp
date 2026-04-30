"""Reasoning sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, ContentBlock, FooterState
from ...events import CardEvent, CardEventType


def reduce_reasoning(state: CardState, event: CardEvent) -> CardState:
    """Handle REASONING_STARTED / REASONING_DELTA / REASONING_DONE."""
    match event.type:
        case CardEventType.REASONING_STARTED:
            block_id = event.payload.get("block_id", "")
            new_block = ContentBlock(kind="reasoning", block_id=block_id, status="active", content="")
            footer = replace(state.footer, status="thinking", status_text="💭 深度思考中...")
            return replace(state, blocks=state.blocks + (new_block,), footer=footer)

        case CardEventType.REASONING_DELTA:
            block_id = event.payload.get("block_id", "")
            text = event.payload.get("text", "")
            blocks = list(state.blocks)
            found = False
            for i, b in enumerate(blocks):
                if b.block_id == block_id and b.kind == "reasoning":
                    new_content = b.content + text
                    blocks[i] = replace(b, content=new_content, char_count=len(new_content))
                    found = True
                    break
            if not found:
                new_block = ContentBlock(kind="reasoning", block_id=block_id, status="active",
                                         content=text, char_count=len(text))
                blocks.append(new_block)
            return replace(state, blocks=tuple(blocks))

        case CardEventType.REASONING_DONE:
            block_id = event.payload.get("block_id", "")
            blocks = list(state.blocks)
            for i, b in enumerate(blocks):
                if b.block_id == block_id and b.kind == "reasoning":
                    blocks[i] = replace(b, status="completed")
                    break
            footer = replace(state.footer, status=None, status_text=None)
            return replace(state, blocks=tuple(blocks), footer=footer)

    return state
