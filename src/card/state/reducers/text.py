"""Text block sub-reducer."""
from __future__ import annotations

import re
from dataclasses import replace

from ...events import CardEvent, CardEventType
from ..models import CardState, TextBlock

_STRUCTURAL_LINE_RE = re.compile(
    r"^\s*(?:"
    r"#{1,6}\s+|"
    r"(?:[-*+]|\d+[.)])\s+|"
    r">|"
    r"\|.*\||"
    r"`{3,}|"
    r"-{3,}\s*$"
    r")"
)


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
                updated = replace(b, content=_append_stream_text(b.content, text))
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


def _append_stream_text(existing: str, incoming: str) -> str:
    """Append streamed text while removing token-boundary soft newlines.

    ACP providers may emit token chunks ending or starting with a single
    newline even when the final markdown is one paragraph. Feishu markdown
    renders those newlines literally, so collapse only boundary newlines that
    look like continuation text and preserve structural markdown breaks.
    """
    if not existing:
        return incoming.lstrip("\n")
    if not incoming:
        return existing
    if not _should_collapse_boundary(existing, incoming):
        return existing + incoming

    left = existing[:-1] if existing.endswith("\n") else existing
    right = incoming[1:] if incoming.startswith("\n") else incoming
    return f"{left}{_soft_join_separator(left, right)}{right}"


def _should_collapse_boundary(existing: str, incoming: str) -> bool:
    trailing_soft_break = existing.endswith("\n") and not existing.endswith("\n\n")
    leading_soft_break = incoming.startswith("\n") and not incoming.startswith("\n\n")
    if not trailing_soft_break and not leading_soft_break:
        return False
    if _inside_fenced_code(existing):
        return False

    left = existing[:-1] if trailing_soft_break else existing
    right = incoming[1:] if leading_soft_break else incoming
    left_line = left.rsplit("\n", 1)[-1]
    right_line = right.split("\n", 1)[0]
    if not left_line.strip() or not right_line.strip():
        return False
    if left_line.endswith("  "):
        return False
    if _is_structural_markdown_line(left_line) or _is_structural_markdown_line(right_line):
        return False
    return True


def _inside_fenced_code(text: str) -> bool:
    return text.count("```") % 2 == 1


def _is_structural_markdown_line(line: str) -> bool:
    return bool(_STRUCTURAL_LINE_RE.match(line))


def _soft_join_separator(left: str, right: str) -> str:
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return ""
    if _needs_word_space(left[-1], right[0]):
        return " "
    return ""


def _needs_word_space(left: str, right: str) -> bool:
    left_ascii = left.isascii() and left.isalnum()
    right_ascii = right.isascii() and right.isalnum()
    if left_ascii or right_ascii:
        return True
    return False
