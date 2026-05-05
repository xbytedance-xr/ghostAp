"""Reasoning panel rendering."""

from __future__ import annotations

from src.card.render.budget import RenderBudget
from src.card.state.models import ContentBlock
from src.card.themes import PANEL_STYLES
from src.card.ui_text import UI_TEXT


def render_reasoning_panel(block: ContentBlock, budget: RenderBudget | None = None) -> dict:
    """Render reasoning/thinking as a collapsible_panel."""
    if budget is None:
        budget = RenderBudget()

    is_active = block.status == "active"

    if is_active:
        title_text = UI_TEXT["reasoning_panel_thinking"]
        expanded = True
        content = block.content
    else:
        char_count = block.char_count or len(block.content)
        title_text = UI_TEXT["reasoning_panel_done"].format(char_count=char_count)
        expanded = False
        content = block.content
        if len(content) > budget.reasoning_tail_chars:
            content = "…" + content[-budget.reasoning_tail_chars:]

    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": title_text},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": PANEL_STYLES["border_normal"], "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": content, "text_size": "notation"}],
    }
