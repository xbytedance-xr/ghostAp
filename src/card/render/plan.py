"""Plan panel rendering."""

from __future__ import annotations

import re

from src.card.state.models import ContentBlock
from src.card.themes import PANEL_STYLES

from .budget import RenderBudget

_NUMBERED_PLAN_ITEM_RE = re.compile(r"(?<!\S)(?P<num>\d+)\s*[.．、]\s+")


def render_plan_panel(
    block: ContentBlock,
    *,
    budget: RenderBudget | None = None,
    phase: str = "running",
    content_override: str | None = None,
) -> dict:
    """Render plan as an always-expanded panel.

    Args:
        block: The content block containing plan markdown.
        budget: Unused for plan truncation; accepted for renderer API compatibility.
        phase: Unused; plans stay expanded so each item remains visible.
        content_override: If provided, use this instead of block.content.
    """
    content = content_override if content_override is not None else block.content
    content = _format_plan_content(content)

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
        "border": {"color": PANEL_STYLES["border_plan"], "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [{"tag": "markdown", "content": content}],
    }


def _format_plan_content(content: str) -> str:
    """Normalize inline numbered plans into one visible item per line."""
    raw = str(content or "").strip()
    matches = list(_NUMBERED_PLAN_ITEM_RE.finditer(raw))
    if len(matches) <= 1:
        return raw

    lines: list[str] = []
    prefix = raw[:matches[0].start()].strip()
    if prefix:
        lines.append(prefix)

    for index, match in enumerate(matches):
        item_start = match.end()
        item_end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        item = raw[item_start:item_end].strip()
        if item:
            lines.append(f"{match.group('num')}. {item}")
    return "\n".join(lines)
