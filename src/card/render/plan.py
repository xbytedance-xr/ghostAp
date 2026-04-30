"""Plan panel rendering."""

from __future__ import annotations

from src.card.state.models import ContentBlock


def render_plan_panel(block: ContentBlock) -> dict:
    """Render plan as a collapsible_panel."""
    return {
        "tag": "collapsible_panel",
        "expanded": True,
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
        "border": {"color": "blue", "corner_radius": "5px"},
        "vertical_spacing": "8px",
        "padding": "8px 8px 8px 8px",
        "elements": [{"tag": "markdown", "content": block.content}],
    }
