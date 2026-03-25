"""Shared card element builders used by both CardBuilder and StreamingCardManager."""

from typing import Optional

from ..config import get_settings
from .styles import BUTTON_CONFIG, THEMES, ProjectTheme


def get_button_size() -> str:
    return get_settings().card_button_size or "medium"


def get_theme(color: str) -> ProjectTheme:
    return THEMES.get(color, THEMES["green"])


def apply_compact_style(button: dict) -> dict:
    """Apply compact button styling (small size) for mobile friendliness."""
    if isinstance(button, dict) and button.get("tag") == "button":
        button.setdefault("size", get_button_size())
    return button


def _create_mode_button(key: str, action: str, project_id: Optional[str] = None) -> dict:
    """Create a button from config with dynamic action value."""
    config = BUTTON_CONFIG.get(key)
    if not config:
        return {}

    value = {"action": action}
    if project_id:
        value["project_id"] = project_id

    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": config["text"]},
        "type": config["type"],
        "size": get_button_size(),
        "behaviors": [{"type": "callback", "value": value}],
    }


def build_mode_buttons(
    is_coco_mode: bool,
    project_id: Optional[str] = None,
    is_claude_mode: bool = False,
    is_ttadk_mode: bool = False,
    is_gemini_mode: bool = False,
) -> list[dict]:
    """Build mode-specific footer buttons (exit/enter mode + switch project)."""
    buttons = []

    if is_claude_mode:
        buttons.append(_create_mode_button("exit_claude", "exit_claude", project_id))
        buttons.append(_create_mode_button("switch_project", "switch_project"))
    elif is_coco_mode:
        buttons.append(_create_mode_button("exit_coco", "exit_coco", project_id))
        buttons.append(_create_mode_button("switch_project", "switch_project"))
    elif is_gemini_mode:
        buttons.append(_create_mode_button("exit_gemini", "exit_gemini", project_id))
        buttons.append(_create_mode_button("switch_project", "switch_project"))
    elif is_ttadk_mode:
        buttons.append(_create_mode_button("switch_ttadk_tool", "show_ttadk_menu", project_id))
        buttons.append(_create_mode_button("exit_ttadk", "exit_ttadk", project_id))
        buttons.append(_create_mode_button("switch_project", "switch_project"))
    else:
        buttons.append(_create_mode_button("enter_coco", "enter_coco", project_id))
        buttons.append(_create_mode_button("enter_claude", "enter_claude", project_id))
        buttons.append(_create_mode_button("enter_gemini", "enter_gemini", project_id))
        buttons.append(_create_mode_button("enter_ttadk", "enter_ttadk", project_id))

    # Filter out empty buttons if config missing
    return [b for b in buttons if b]


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
    is_ttadk_mode: bool = False,
    is_gemini_mode: bool = False,
    ttadk_tool_name: Optional[str] = None,
    ttadk_model_name: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve card title and header template based on mode and project name."""
    if is_claude_mode:
        mode_icon, header_template = "🔮", "purple"
    elif is_coco_mode:
        mode_icon, header_template = "🤖", "blue"
    elif is_gemini_mode:
        mode_icon, header_template = "✨", "turquoise"
    elif is_ttadk_mode:
        mode_icon, header_template = "🎮", "orange"
    else:
        mode_icon, header_template = "🧠", "turquoise"

    # If a theme_color is provided (from project), use it for the template
    if theme_color and not is_claude_mode and not is_coco_mode and not is_gemini_mode and not is_ttadk_mode:
        header_template = get_theme(theme_color).header_template

    ttadk_suffix = _build_ttadk_title_suffix(ttadk_tool_name, ttadk_model_name) if is_ttadk_mode else ""

    if project_name:
        if is_claude_mode:
            title = f"🔮 {project_name} · Claude"
        elif is_coco_mode:
            title = f"🤖 {project_name} · Coco"
        elif is_gemini_mode:
            title = f"✨ {project_name} · Gemini"
        elif is_ttadk_mode:
            title = f"🎮 {project_name} · TTADK{ttadk_suffix}"
        else:
            title = f"🧠 {project_name}"
    else:
        if is_claude_mode:
            mode_name = "Claude 编程模式"
        elif is_coco_mode:
            mode_name = "编程模式"
        elif is_gemini_mode:
            mode_name = "Gemini 编程模式"
        elif is_ttadk_mode:
            mode_name = f"TTADK{ttadk_suffix}"
        else:
            mode_name = "智能模式"
        title = f"{mode_icon} {mode_name}"

    return title, header_template


def _build_ttadk_title_suffix(tool_name: Optional[str], model_name: Optional[str]) -> str:
    tool = (tool_name or "").strip()
    model = (model_name or "").strip()
    if tool and model:
        return f" · {tool}({model})"
    if tool:
        return f" · {tool}"
    return ""


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
        chunk = styled[i : i + columns]

        column_objs = []
        for j in range(columns):
            btn = chunk[j] if j < len(chunk) else None
            # Only create column if it has content OR if we need to preserve grid spacing?
            # Feishu schema 2.0: empty columns are allowed.
            column_objs.append(
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "elements": [btn] if btn else [],
                }
            )

        rows.append(
            {
                "tag": "column_set",
                "flex_mode": "stretch",
                "background_style": "default",
                "columns": column_objs,
            }
        )

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
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "✅ 确认"},
                    "type": "primary",
                    "value": {"action": "confirm", **context},
                }
            )
        elif action == "retry":
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🔄 重试"},
                    "type": "primary",
                    "value": {"action": "retry", **context},
                }
            )
        elif action == "cancel":
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "❌ 取消"},
                    "type": "default",
                    "value": {"action": "cancel", **context},
                }
            )
        elif action == "continue":
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "▶️ 继续"},
                    "type": "primary",
                    "value": {"action": "continue", **context},
                }
            )
        elif action == "next":
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "⏭️ 下一步"},
                    "type": "primary",
                    "value": {"action": "next", **context},
                }
            )
        elif action == "stop":
            buttons.append(
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🛑 停止"},
                    "type": "danger",
                    "value": {"action": "stop", **context},
                }
            )

    return [apply_compact_style(b) for b in buttons]


def _build_button_vertical(buttons: list[dict]) -> list[dict]:
    """Build vertical stack layout for buttons (best for mobile)."""
    if not buttons:
        return []

    styled = [apply_compact_style(b) for b in buttons]
    rows = []
    for btn in styled:
        rows.append(
            {
                "tag": "column_set",
                "flex_mode": "none",
                "columns": [{"tag": "column", "width": "weighted", "weight": 1, "elements": [btn]}],
            }
        )
    return rows


def _build_button_flow(buttons: list[dict]) -> list[dict]:
    """Build flow layout for buttons."""
    if not buttons:
        return []

    styled = [apply_compact_style(b) for b in buttons]

    return [
        {
            "tag": "column_set",
            "flex_mode": "flow",
            "columns": [{"tag": "column", "width": "auto", "elements": [btn]} for btn in styled],
        }
    ]
