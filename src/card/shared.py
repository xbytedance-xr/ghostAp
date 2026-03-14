"""Shared card element builders used by both CardBuilder and StreamingCardManager."""

from dataclasses import dataclass
from typing import Optional
from ..config import get_settings


def get_button_size() -> str:
    return get_settings().card_button_size or "medium"


@dataclass
class ProjectTheme:
    name: str
    color: str
    emoji: str
    header_template: str


THEMES = {
    "green": ProjectTheme("green", "green", "🟢", "green"),
    "blue": ProjectTheme("blue", "blue", "🔵", "blue"),
    "purple": ProjectTheme("purple", "purple", "🟣", "purple"),
    "orange": ProjectTheme("orange", "orange", "🟠", "orange"),
    "red": ProjectTheme("red", "red", "🔴", "red"),
    "turquoise": ProjectTheme("turquoise", "turquoise", "🩵", "turquoise"),
    "violet": ProjectTheme("violet", "violet", "🟣", "violet"),
    "indigo": ProjectTheme("indigo", "indigo", "🟣", "indigo"),
    "carmine": ProjectTheme("carmine", "carmine", "🔴", "carmine"),
    "wathet": ProjectTheme("wathet", "wathet", "🔵", "wathet"),
    "grey": ProjectTheme("grey", "grey", "⚪", "grey"),
    "yellow": ProjectTheme("yellow", "yellow", "🟡", "yellow"),
}


def get_theme(color: str) -> ProjectTheme:
    return THEMES.get(color, THEMES["green"])


def apply_compact_style(button: dict) -> dict:
    """Apply compact button styling (small size) for mobile friendliness."""
    if isinstance(button, dict) and button.get("tag") == "button":
        button.setdefault("size", get_button_size())
    return button


def build_mode_buttons(
    is_coco_mode: bool,
    project_id: Optional[str] = None,
    is_claude_mode: bool = False,
) -> list[dict]:
    """Build mode-specific footer buttons (exit/enter mode + switch project)."""
    buttons = []
    size = get_button_size()

    if is_claude_mode:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🚪 退出Claude"},
            "type": "default",
            "size": size,
            "behaviors": [{"type": "callback", "value": {"action": "exit_claude", "project_id": project_id}}],
        })
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 切换项目"},
            "type": "default",
            "size": size,
            "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}],
        })
    elif is_coco_mode:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🚪 退出Coco"},
            "type": "default",
            "size": size,
            "behaviors": [{"type": "callback", "value": {"action": "exit_coco", "project_id": project_id}}],
        })
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔄 切换项目"},
            "type": "default",
            "size": size,
            "behaviors": [{"type": "callback", "value": {"action": "switch_project"}}],
        })
    else:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🤖 Coco模式"},
            "type": "primary",
            "size": size,
            "behaviors": [{"type": "callback", "value": {"action": "enter_coco", "project_id": project_id}}],
        })
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔮 Claude模式"},
            "type": "default",
            "size": size,
            "behaviors": [{"type": "callback", "value": {"action": "enter_claude", "project_id": project_id}}],
        })

    return buttons



def build_responsive_layout(buttons: list[dict]) -> list[dict]:
    """Build responsive button layout based on config setting.

    - desktop: native action layout (horizontal)
    - mobile: forced vertical stack for better touch targets
    - responsive: <=2 buttons use action, >2 use column_set grid
    - flow: use flow layout
    """
    if not buttons:
        return []

    settings = get_settings()
    layout = (settings.card_button_layout or "responsive").strip().lower()

    if layout == "desktop":
        return _build_button_row_action(buttons)
    if layout == "mobile":
        return _build_button_vertical(buttons)
    if layout == "flow":
        return _build_button_flow(buttons)

    # responsive (default)
    
    # Force vertical for >2 buttons if mobile optimization is enabled
    # This helps avoid clutter on small screens while keeping 2 buttons side-by-side
    if settings.card_mobile_force_vertical and len(buttons) > 2:
        mobile_mode = (settings.card_mobile_layout_mode or "vertical").strip().lower()
        if mobile_mode == "flow":
            return _build_button_flow(buttons)
        return _build_button_vertical(buttons)

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
    """Build button row using column_set (schema 2.0 compatible).

    Schema 2.0 does not support the 'action' tag; use column_set grid instead.
    """
    if not buttons:
        return []
    return _build_button_grid(buttons, columns=len(buttons))


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
        
        column_objs = []
        for j in range(columns):
            btn = chunk[j] if j < len(chunk) else None
            # Only create column if it has content OR if we need to preserve grid spacing?
            # Feishu schema 2.0: empty columns are allowed.
            column_objs.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [btn] if btn else [],
            })

        rows.append({
            "tag": "column_set",
            "flex_mode": "stretch",
            "background_style": "default",
            "columns": column_objs,
        })

    return rows

def build_quick_actions(actions: list[str], context: dict = None) -> list[dict]:
    """Build standardized QuickAction buttons.

    Supported actions: confirm, retry, cancel, continue, next, stop
    """
    buttons = []
    context = context or {}

    for action in actions:
        action = action.lower()
        if action == "confirm":
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "✅ 确认"},
                "type": "primary",
                "value": {"action": "confirm", **context}
            })
        elif action == "retry":
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 重试"},
                "type": "primary",
                "value": {"action": "retry", **context}
            })
        elif action == "cancel":
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "❌ 取消"},
                "type": "default",
                "value": {"action": "cancel", **context}
            })
        elif action == "continue":
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "▶️ 继续"},
                "type": "primary",
                "value": {"action": "continue", **context}
            })
        elif action == "next":
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⏭️ 下一步"},
                "type": "primary",
                "value": {"action": "next", **context}
            })
        elif action == "stop":
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🛑 停止"},
                "type": "danger",
                "value": {"action": "stop", **context}
            })

    return [apply_compact_style(b) for b in buttons]


def _build_button_vertical(buttons: list[dict]) -> list[dict]:
    """Build vertical stack layout for buttons (best for mobile)."""
    if not buttons:
        return []

    styled = [apply_compact_style(b) for b in buttons]
    rows = []
    for btn in styled:
        rows.append({
            "tag": "column_set",
            "flex_mode": "none",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [btn]
            }]
        })
    return rows


def _build_button_flow(buttons: list[dict]) -> list[dict]:
    """Build flow layout for buttons."""
    if not buttons:
        return []
    
    styled = [apply_compact_style(b) for b in buttons]
    
    return [{
        "tag": "column_set",
        "flex_mode": "flow",
        "columns": [{
            "tag": "column",
            "width": "auto",
            "elements": [btn]
        } for btn in styled]
    }]
