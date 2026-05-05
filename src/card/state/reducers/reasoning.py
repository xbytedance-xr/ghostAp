"""Reasoning sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, ReasoningBlock, FooterState
from ...events import CardEvent, CardEventType
from ...ui_text import UI_TEXT


def reduce_reasoning(state: CardState, event: CardEvent) -> CardState:
    """Handle REASONING_STARTED / REASONING_DELTA / REASONING_DONE."""
    match event.type:
        case CardEventType.REASONING_STARTED:
            block_id = event.payload.get("block_id", "")
            new_block = ReasoningBlock(block_id=block_id, status="active", content="")
            footer = replace(state.footer, status="thinking", status_text=UI_TEXT["card_lifecycle_reasoning"])
            return replace(state, blocks=state.blocks + (new_block,), footer=footer)

        case CardEventType.REASONING_DELTA:
            block_id = event.payload.get("block_id", "")
            text = event.payload.get("text", "")
            # O(1) lookup via block_index
            idx = state.block_index.get(block_id)
            if idx is not None and idx < len(state.blocks) and state.blocks[idx].kind == "reasoning":
                b = state.blocks[idx]
                new_content = b.content + text
                updated = replace(b, content=new_content, char_count=len(new_content))
                blocks = state.blocks[:idx] + (updated,) + state.blocks[idx + 1:]
                return replace(state, blocks=blocks)
            # Auto-create block if not found
            new_block = ReasoningBlock(block_id=block_id, status="active",
                                     content=text, char_count=len(text))
            return replace(state, blocks=state.blocks + (new_block,))

        case CardEventType.REASONING_DONE:
            block_id = event.payload.get("block_id", "")
            # O(1) lookup via block_index
            idx = state.block_index.get(block_id)
            if idx is not None and idx < len(state.blocks) and state.blocks[idx].kind == "reasoning":
                b = state.blocks[idx]
                updated = replace(b, status="completed")
                blocks = state.blocks[:idx] + (updated,) + state.blocks[idx + 1:]
                footer = replace(state.footer, status=None, status_text=None)
                return replace(state, blocks=blocks, footer=footer)
            footer = replace(state.footer, status=None, status_text=None)
            return replace(state, footer=footer)

    return state
