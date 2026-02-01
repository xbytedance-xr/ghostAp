"""Shared card element builders used by both CardBuilder and StreamingCardManager."""

from typing import Optional
from ..config import get_settings


BUTTON_SIZE = "small"


def apply_compact_style(button: dict) -> dict:
    """Apply compact button styling (small size) for mobile friendliness."""
    if isinstance(button, dict) and button.get("tag") == "button":
        button.setdefault("size", BUTTON_SIZE)
    return button


def build_mode_buttons(
    is_coco_mode: bool,
    project_id: Optional[str] = None,
    is_claude_mode: bool = False,
) -> list[dict]:
    """Build mode-specific footer buttons (exit/enter mode + switch project)."""
    buttons = []

    if is_claude_mode:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🚪 退出Claude"},
            "type": "default",
            "size": BUTTON_SIZE,
            "behaviors": [{"type": "callback", "value": {"action": "exit_claude", "project_id": project_id}}],
        })
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 切换项目"},
            "type": "default",
            "size": BUTTON_SIZE,
            "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}],
        })
    elif is_coco_mode:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🚪 退出Coco"},
            "type": "default",
            "size": BUTTON_SIZE,
            "behaviors": [{"type": "callback", "value": {"action": "exit_coco", "project_id": project_id}}],
        })
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 切换项目"},
            "type": "default",
            "size": BUTTON_SIZE,
            "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}],
        })
    else:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🤖 Coco模式"},
            "type": "primary",
            "size": BUTTON_SIZE,
            "behaviors": [{"type": "callback", "value": {"action": "enter_coco", "project_id": project_id}}],
        })
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔮 Claude模式"},
            "type": "default",
            "size": BUTTON_SIZE,
            "behaviors": [{"type": "callback", "value": {"action": "enter_claude", "project_id": project_id}}],
        })

    return buttons


def build_responsive_layout(buttons: list[dict]) -> list[dict]:
    """Build responsive button layout based on config setting.

    - desktop: native action layout
    - mobile: forced two-column column_set
    - responsive: <=2 buttons use action, >2 use column_set grid
    """
    if not buttons:
        return []

    layout = (get_settings().card_button_layout or "responsive").strip().lower()
    if layout == "desktop":
        return _build_button_row_action(buttons)
    if layout == "mobile":
        return _build_button_grid(buttons)

    # responsive (default)
    if len(buttons) <= 2:
        return _build_button_row_action(buttons)
    return _build_button_grid(buttons)


def resolve_title_and_template(
    project_name: Optional[str],
    is_coco_mode: bool,
    is_claude_mode: bool,
    theme_color: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve card title and header template based on mode and project name."""
    if is_claude_mode:
        mode_icon, header_template = "🔮", "purple"
    elif is_coco_mode:
        mode_icon, header_template = "🤖", "blue"
    else:
        mode_icon, header_template = "🧠", "turquoise"

    # If a theme_color is provided (from project), use it for the template
    if theme_color and not is_claude_mode and not is_coco_mode:
        from .themes import get_theme
        header_template = get_theme(theme_color).header_template

    if project_name:
        if is_claude_mode:
            title = f"🔮 {project_name} · Claude"
        elif is_coco_mode:
            title = f"🤖 {project_name} · Coco"
        else:
            title = f"🧠 {project_name}"
    else:
        if is_claude_mode:
            mode_name = "Claude 编程模式"
        elif is_coco_mode:
            mode_name = "编程模式"
        else:
            mode_name = "智能模式"
        title = f"{mode_icon} {mode_name}"

    return title, header_template


# ---- Internal helpers ----

def _build_button_row_action(buttons: list[dict]) -> list[dict]:
    if not buttons:
        return []
    styled = [apply_compact_style(b) for b in buttons]
    return [{"tag": "action", "actions": styled}]


def _build_button_grid(buttons: list[dict], columns: int = 2) -> list[dict]:
    """Build two-column grid layout for buttons."""
    if not buttons:
        return []
    if columns <= 0:
        columns = 2

    styled = [apply_compact_style(b) for b in buttons]
    rows: list[dict] = []

    for i in range(0, len(styled), columns):
        chunk = styled[i:i + columns]
        col_1 = chunk[0] if len(chunk) > 0 else None
        col_2 = chunk[1] if len(chunk) > 1 else None

        rows.append({
            "tag": "column_set",
            "flex_mode": "stretch",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [col_1] if col_1 else [],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [col_2] if col_2 else [],
                },
            ],
        })

    return rows
