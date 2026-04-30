"""Tool call sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, ContentBlock, FooterState
from ...events import CardEvent, CardEventType


def reduce_tool(state: CardState, event: CardEvent) -> CardState:
    """Handle TOOL_STARTED / TOOL_DELTA / TOOL_DONE / TOOL_FAILED."""
    match event.type:
        case CardEventType.TOOL_STARTED:
            block_id = event.payload.get("block_id", "")
            tool_name = event.payload.get("tool_name", "")
            tool_input = event.payload.get("tool_input", "")
            new_block = ContentBlock(
                kind="tool_call", block_id=block_id, status="active",
                tool_name=tool_name, tool_input=tool_input, content="",
            )
            footer = replace(state.footer, status="tool_running",
                             status_text=f"🔧 执行中: {tool_name}")
            return replace(state, blocks=state.blocks + (new_block,), footer=footer)

        case CardEventType.TOOL_DELTA:
            block_id = event.payload.get("block_id", "")
            content = event.payload.get("content", "")
            blocks = list(state.blocks)
            for i, b in enumerate(blocks):
                if b.block_id == block_id and b.kind == "tool_call":
                    blocks[i] = replace(b, content=b.content + content)
                    break
            return replace(state, blocks=tuple(blocks))

        case CardEventType.TOOL_DONE:
            block_id = event.payload.get("block_id", "")
            tool_output = event.payload.get("tool_output", "")
            tool_summary = event.payload.get("tool_summary", "")
            blocks = list(state.blocks)
            for i, b in enumerate(blocks):
                if b.block_id == block_id and b.kind == "tool_call":
                    blocks[i] = replace(b, status="completed",
                                         tool_output=tool_output, tool_summary=tool_summary)
                    break
            footer = replace(state.footer, status=None, status_text=None)
            return replace(state, blocks=tuple(blocks), footer=footer)

        case CardEventType.TOOL_FAILED:
            block_id = event.payload.get("block_id", "")
            error = event.payload.get("error", "")
            blocks = list(state.blocks)
            for i, b in enumerate(blocks):
                if b.block_id == block_id and b.kind == "tool_call":
                    blocks[i] = replace(b, status="failed", tool_output=error)
                    break
            footer = replace(state.footer, status=None, status_text=None)
            return replace(state, blocks=tuple(blocks), footer=footer)

    return state
