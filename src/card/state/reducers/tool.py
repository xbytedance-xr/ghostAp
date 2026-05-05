"""Tool call sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, ToolBlock, FooterState
from ...events import CardEvent, CardEventType
from ...ui_text import UI_TEXT


def reduce_tool(state: CardState, event: CardEvent) -> CardState:
    """Handle TOOL_STARTED / TOOL_DELTA / TOOL_DONE / TOOL_FAILED."""
    match event.type:
        case CardEventType.TOOL_STARTED:
            block_id = event.payload.get("block_id", "")
            tool_name = event.payload.get("tool_name", "")
            tool_input = event.payload.get("tool_input", "")
            new_block = ToolBlock(
                block_id=block_id, status="active",
                tool_name=tool_name, tool_input=tool_input, content="",
            )
            footer = replace(state.footer, status="tool_running",
                             status_text=UI_TEXT["card_tool_running"].format(tool_name=tool_name))
            return replace(state, blocks=state.blocks + (new_block,), footer=footer)

        case CardEventType.TOOL_DELTA:
            block_id = event.payload.get("block_id", "")
            content = event.payload.get("content", "")
            # O(1) lookup via block_index
            idx = state.block_index.get(block_id)
            if idx is not None and idx < len(state.blocks) and state.blocks[idx].kind == "tool_call":
                b = state.blocks[idx]
                updated = replace(b, content=b.content + content)
                blocks = state.blocks[:idx] + (updated,) + state.blocks[idx + 1:]
                return replace(state, blocks=blocks)
            return state

        case CardEventType.TOOL_DONE:
            block_id = event.payload.get("block_id", "")
            tool_output = event.payload.get("tool_output", "")
            tool_summary = event.payload.get("tool_summary", "")
            # O(1) lookup via block_index
            idx = state.block_index.get(block_id)
            if idx is not None and idx < len(state.blocks) and state.blocks[idx].kind == "tool_call":
                b = state.blocks[idx]
                updated = replace(b, status="completed",
                                     tool_output=tool_output, tool_summary=tool_summary)
                blocks = state.blocks[:idx] + (updated,) + state.blocks[idx + 1:]
                footer = replace(state.footer, status=None, status_text=None)
                return replace(state, blocks=blocks, footer=footer)
            footer = replace(state.footer, status=None, status_text=None)
            return replace(state, footer=footer)

        case CardEventType.TOOL_FAILED:
            block_id = event.payload.get("block_id", "")
            error = event.payload.get("error", "")
            # O(1) lookup via block_index
            idx = state.block_index.get(block_id)
            if idx is not None and idx < len(state.blocks) and state.blocks[idx].kind == "tool_call":
                b = state.blocks[idx]
                updated = replace(b, status="failed", tool_output=error)
                blocks = state.blocks[:idx] + (updated,) + state.blocks[idx + 1:]
                footer = replace(state.footer, status=None, status_text=None)
                return replace(state, blocks=blocks, footer=footer)
            footer = replace(state.footer, status=None, status_text=None)
            return replace(state, footer=footer)

    return state
