"""Reasoning panel rendering."""

from __future__ import annotations

from src.card.render.budget import RenderBudget
from src.card.state.models import ContentBlock
from src.card.ui_text import UI_TEXT


def render_reasoning_panel(
    block: ContentBlock,
    budget: RenderBudget | None = None,
    *,
    content_override: str | None = None,
) -> dict:
    """Render reasoning/thinking as a compact grey left-aligned panel.

    Always visible (no collapsible). Active reasoning shows full content;
    completed reasoning keeps tail truncation via reasoning_tail_chars.

    Args:
        block: The reasoning block (used for status/char_count metadata).
        budget: Render budget for truncation limits.
        content_override: If provided, use this instead of block.content.
            Needed when block_index maps to the *last* block with a shared
            block_id but the atom carries the correct per-block content.
    """
    if budget is None:
        budget = RenderBudget()

    is_active = block.status == "active"
    raw_content = content_override if content_override is not None else block.content
    raw_content = raw_content or ""

    if is_active:
        title_text = UI_TEXT["reasoning_panel_thinking"]
        content = raw_content
    else:
        char_count = block.char_count or len(raw_content)
        title_text = UI_TEXT["reasoning_panel_done"].format(char_count=char_count)
        content = raw_content
        if len(content) > budget.reasoning_tail_chars:
            content = "…" + content[-budget.reasoning_tail_chars:]

    # Build content: title line + reasoning body
    display_content = f"**{title_text}**\n{content}" if content else f"**{title_text}**"

    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": display_content,
                        "text_size": "normal",
                        "text_align": "left",
                    },
                ],
            },
        ],
    }
