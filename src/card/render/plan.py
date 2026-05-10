"""Plan panel rendering."""

from __future__ import annotations

from src.card.state.models import ContentBlock
from src.card.themes import PANEL_STYLES
from .budget import RenderBudget


def render_plan_panel(
    block: ContentBlock,
    *,
    budget: RenderBudget | None = None,
    phase: str = "running",
    content_override: str | None = None,
) -> dict:
    """Render plan as a collapsible_panel.

    Args:
        block: The content block containing plan markdown.
        budget: Render budget (provides plan_max_chars).
        phase: Current engine phase. Panel auto-collapses when not 'running'.
        content_override: If provided, use this instead of block.content.
    """
    max_chars = budget.plan_max_chars if budget else 2000
    content = content_override if content_override is not None else block.content
    if len(content) > max_chars and phase != "running":
        content = content[:max_chars] + "\n\n…(已截断，展开查看完整计划)"

    return {
        "tag": "collapsible_panel",
        "expanded": phase == "running",
        "header": {
            "title": {"tag": "markdown", "content": "📋 **执行计划**"},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": PANEL_STYLES["border_plan"], "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": content}],
    }
