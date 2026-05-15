"""Spec review role panel rendering."""

from __future__ import annotations

from src.card.state.models import ContentBlock
from src.card.themes import PANEL_STYLES


def render_review_role_panel(block: ContentBlock) -> dict | None:
    """Render one Spec review role as a separate collapsible panel."""
    data = getattr(block, "data", None)
    if not isinstance(data, dict):
        return None

    title = str(data.get("title") or "审查角色").strip()
    emoji = str(data.get("emoji") or "🔍").strip()
    status_text = str(data.get("status_text") or ("✅ PASS" if data.get("passed") else "❌ 有建议")).strip()
    agent_detail = str(data.get("agent_detail") or "").strip()
    suggestions = [str(item).strip() for item in data.get("suggestions") or [] if str(item).strip()]
    summary = str(data.get("summary") or "").strip()
    passed = bool(data.get("passed"))

    header_parts = [f"{emoji} **{title}**", status_text]
    if suggestions:
        header_parts.append(f"{len(suggestions)} 条建议")
    if agent_detail:
        header_parts.append(agent_detail)

    body_lines: list[str] = [
        f"**角色**：{title}",
        f"**审查结果**：{status_text}",
    ]
    if agent_detail:
        body_lines.append(f"**工具/模型**：{agent_detail}")
    if summary:
        body_lines.append(f"**结论**：{summary}")

    if suggestions:
        body_lines.append("")
        body_lines.append("**具体建议**")
        for idx, suggestion in enumerate(suggestions, start=1):
            body_lines.append(f"{idx}. {suggestion}")
    elif passed:
        body_lines.append("")
        body_lines.append("无改进建议。")
    else:
        body_lines.append("")
        body_lines.append("未返回具体建议。")

    background_style = str(data.get("background_style") or "wathet")
    border_color = str(data.get("border_color") or background_style or PANEL_STYLES["border_normal"])
    expanded = bool(data.get("expanded", bool(suggestions) or not passed))

    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "markdown", "content": " · ".join(header_parts)},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "follow_text",
            "icon_expanded_angle": -180,
        },
        "border": {"color": border_color, "corner_radius": PANEL_STYLES["corner_radius"]},
        "vertical_spacing": PANEL_STYLES["vertical_spacing"],
        "padding": PANEL_STYLES["padding_standard"],
        "elements": [
            {
                "tag": "column_set",
                "flex_mode": "stretch",
                "background_style": background_style,
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "center",
                        "elements": [
                            {"tag": "markdown", "content": "\n".join(body_lines), "text_align": "left"}
                        ],
                    }
                ],
            }
        ],
    }
