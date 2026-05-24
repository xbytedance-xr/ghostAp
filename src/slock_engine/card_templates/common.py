"""Common utilities for Slock card templates.

Shared constants, helper functions, and card building primitives
used across all card template modules.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from src.card.shared import apply_compact_style, build_responsive_layout
from src.utils.redact import redact_sensitive

from ..models import (
    ABORT_OPTIONS,
    AGENT_STATUS_BG_COLOR_MAP,
    AgentIdentity,
    AgentStatus,
    CouncilRun,
    CouncilStatus,
    EscalationLevel,
    EscalationRequest,
    SlockMemory,
    SlockTask,
    TaskStatus,
)

# Re-export for convenience
__all__ = [
    "apply_compact_style",
    "build_responsive_layout",
    "redact_sensitive",
    "DISPLAY_TZ",
    "STATUS_LABEL_ZH",
    "TASK_STATUS_LABEL_ZH",
    "STATUS_ICON_MAP",
    "STATUS_BG_STYLE_MAP",
    "TASK_STATUS_ICONS",
    "TASK_STATUS_BG_COLOR_MAP",
    "COUNCIL_STATUS_LABEL_ZH",
    "TASK_CONTENT_COMPACT_LEN",
    "TASK_CONTENT_PREVIEW_LEN",
    "TASK_CONTENT_DETAIL_LEN",
    "build_callback_button",
    "build_slock_group_jump_button",
    "build_chat_multi_url",
    "truncate_dynamic_label",
    "build_collapsible_panel",
    "build_card_wrapper",
    "build_column_set_row",
    "build_column",
    "build_mobile_card_row",
    "validate_background_style",
    "build_empty_state_card",
    "build_error_state_card",
    "build_usage_hint_card",
]

DISPLAY_TZ = ZoneInfo("Asia/Shanghai")

STATUS_LABEL_ZH: dict[str, str] = {
    "idle": "空闲",
    "waking": "唤醒中",
    "thinking": "思考中",
    "running": "运行中",
    "checking": "检查中",
    "sending": "发送中",
    "moving": "迁移中",
    "discussing": "讨论中",
    "pending_discussion": "等待确认",
}

TASK_STATUS_LABEL_ZH: dict[str, str] = {
    "todo": "待办",
    "in_progress": "进行中",
    "in_review": "审查中",
    "done": "已完成",
}

STATUS_ICON_MAP: dict[AgentStatus, str] = {
    AgentStatus.IDLE: "🟢",
    AgentStatus.WAKING: "🟡",
    AgentStatus.THINKING: "🟡",
    AgentStatus.RUNNING: "🔵",
    AgentStatus.CHECKING: "🔵",
    AgentStatus.SENDING: "⚪",
    AgentStatus.MOVING: "🔶",
    AgentStatus.DISCUSSING: "💬",
    AgentStatus.PENDING_DISCUSSION: "⏳",
}

STATUS_BG_STYLE_MAP: dict[AgentStatus, str] = {
    AgentStatus.IDLE: "default",
    AgentStatus.WAKING: "grey",
    AgentStatus.THINKING: "grey",
    AgentStatus.RUNNING: "card_primary",
    AgentStatus.CHECKING: "card_primary",
    AgentStatus.SENDING: "grey",
    AgentStatus.MOVING: "grey",
    AgentStatus.DISCUSSING: "grey",
    AgentStatus.PENDING_DISCUSSION: "grey",
}

# Legal Feishu column_set background_style values
_VALID_BACKGROUND_STYLES = frozenset({"default", "grey", "card_primary"})

import logging as _logging

_bg_logger = _logging.getLogger(__name__)


def validate_background_style(value: str) -> str:
    """Guard function: return value if legal, else fallback to 'default' with warning."""
    if value in _VALID_BACKGROUND_STYLES:
        return value
    _bg_logger.warning(
        "Invalid background_style '%s' — not in Feishu legal values %s, falling back to 'default'",
        value,
        _VALID_BACKGROUND_STYLES,
    )
    return "default"


TASK_STATUS_ICONS: dict[TaskStatus, str] = {
    TaskStatus.TODO: "⬜",
    TaskStatus.IN_PROGRESS: "🔵",
    TaskStatus.IN_REVIEW: "🟡",
    TaskStatus.DONE: "✅",
}

TASK_STATUS_BG_COLOR_MAP: dict[TaskStatus, str] = {
    TaskStatus.TODO: "grey",
    TaskStatus.IN_PROGRESS: "blue",
    TaskStatus.IN_REVIEW: "yellow",
    TaskStatus.DONE: "green",
}

COUNCIL_STATUS_LABEL_ZH: dict[CouncilStatus, str] = {
    CouncilStatus.STARTING: "准备中",
    CouncilStatus.STAGE1_RUNNING: "独立作答中",
    CouncilStatus.STAGE1_DONE: "独立意见完成",
    CouncilStatus.STAGE2_RUNNING: "匿名互评中",
    CouncilStatus.STAGE2_DONE: "匿名互评完成",
    CouncilStatus.STAGE3_RUNNING: "主席综合中",
    CouncilStatus.COMPLETED: "已完成",
    CouncilStatus.FAILED: "失败",
}

# -- Task content truncation constants (CJK-aware) --
# Compact: status card inline agent row
TASK_CONTENT_COMPACT_LEN = 24
# Preview: general card previews (task board, progress cards)
TASK_CONTENT_PREVIEW_LEN = 40
# Detail: expanded/detail views (role info card current task)
TASK_CONTENT_DETAIL_LEN = 60


def build_empty_state_card(
    title: str,
    hint_text: str,
    guide_buttons: list[dict] | None = None,
) -> dict:
    """Build a standard empty-state card with hint text and optional guide buttons.

    Used when a query returns no data (e.g. no tasks, no memory, no plans).
    """
    elements: list[dict] = [
        {"tag": "markdown", "content": hint_text},
    ]
    if guide_buttons:
        elements.append({"tag": "hr"})
        elements.append(build_responsive_layout(guide_buttons))
    return build_card_wrapper(
        header_title=title,
        header_template="grey",
        elements=elements,
    )


def build_error_state_card(
    title: str,
    error_msg: str,
) -> dict:
    """Build a standard error-state card.

    Used when an operation fails or the engine is unavailable.
    """
    elements: list[dict] = [
        {"tag": "markdown", "content": f"❌ {error_msg}"},
    ]
    return build_card_wrapper(
        header_title=title,
        header_template="red",
        elements=elements,
    )


def build_usage_hint_card(
    command: str,
    usage_examples: list[str],
) -> dict:
    """Build a standard usage-hint card showing command syntax.

    Used when a command is invoked with missing or invalid arguments.
    """
    lines = [f"**用法：** `{command}`", ""]
    for example in usage_examples:
        lines.append(f"• `{example}`")
    elements: list[dict] = [
        {"tag": "markdown", "content": "\n".join(lines)},
    ]
    return build_card_wrapper(
        header_title="💡 命令提示",
        header_template="grey",
        elements=elements,
    )


def build_callback_button(
    text: str,
    action: str,
    *,
    channel_id: str = "",
    project_id: str = "",
    button_type: str = "default",
    extra_value: dict | None = None,
) -> dict:
    """Build a Feishu card callback button with action routing."""
    value = {"action": action, "channel_id": channel_id}
    if project_id:
        value["project_id"] = project_id
    if extra_value:
        value.update(extra_value)
    return apply_compact_style(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": button_type,
            "value": value,
            "behaviors": [{"type": "callback", "value": value}],
        }
    )


def build_chat_multi_url(chat_id: str) -> dict:
    """Build Feishu multi-platform deep link for a chat."""
    safe_chat_id = quote(str(chat_id or "").strip(), safe="")
    https = f"https://applink.feishu.cn/client/chat/open?openChatId={safe_chat_id}"
    native = f"lark://applink/client/chat/open?openChatId={safe_chat_id}"
    return {
        "url": https,
        "pc_url": https,
        "android_url": native,
        "ios_url": native,
    }


def build_slock_group_jump_button(channel_id: str) -> dict:
    """Build a button that jumps to the Slock group chat."""
    return apply_compact_style(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "进入 Slock 群"},
            "type": "primary",
            "multi_url": build_chat_multi_url(channel_id),
        }
    )


def truncate_dynamic_label(text: str, max_len: int = 20) -> str:
    """Truncate dynamic button label to max_len characters."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def build_collapsible_panel(
    title: str,
    elements: list[dict],
    *,
    expanded: bool = False,
    vertical_spacing: str = "8px",
) -> dict:
    """Build a Feishu collapsible_panel element.

    Args:
        title: Panel header text (supports markdown bold etc.).
        elements: Child elements inside the panel.
        expanded: Whether panel starts expanded.
        vertical_spacing: CSS spacing between child elements.
    """
    return {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {
                "tag": "markdown",
                "content": title,
            },
        },
        "vertical_spacing": vertical_spacing,
        "elements": elements,
    }


_CARD_BYTE_BUDGET = 27 * 1024
_CARD_NODE_BUDGET = 180


def _count_tagged_nodes(obj) -> int:
    """Count dicts with a 'tag' key (Feishu element nodes)."""
    count = 0
    if isinstance(obj, dict):
        if "tag" in obj:
            count += 1
        for v in obj.values():
            count += _count_tagged_nodes(v)
    elif isinstance(obj, list):
        for item in obj:
            count += _count_tagged_nodes(item)
    return count


def _guard_card_payload(card: dict) -> dict:
    """Truncate elements from the end if card exceeds byte/node budget."""
    import json

    elements = card.get("body", {}).get("elements", [])
    if not elements:
        return card

    serialized = json.dumps(card, ensure_ascii=False)
    byte_size = len(serialized.encode("utf-8"))
    node_count = _count_tagged_nodes(card)

    if byte_size <= _CARD_BYTE_BUDGET and node_count <= _CARD_NODE_BUDGET:
        return card

    # Progressively remove elements from the end until within budget
    while len(elements) > 1 and (byte_size > _CARD_BYTE_BUDGET or node_count > _CARD_NODE_BUDGET):
        elements.pop()
        card["body"]["elements"] = elements
        serialized = json.dumps(card, ensure_ascii=False)
        byte_size = len(serialized.encode("utf-8"))
        node_count = _count_tagged_nodes(card)

    # Add truncation notice
    elements.append({"tag": "markdown", "content": "*⚠️ 内容过长，部分已截断*"})
    card["body"]["elements"] = elements
    return card


def build_card_wrapper(
    *,
    header_title: str,
    header_template: str = "indigo",
    header_subtitle: str = "",
    elements: list[dict],
    mobile_optimize: bool = True,
) -> dict:
    """Wrap elements into a complete Feishu Interactive Card 2.0 structure.

    Applies payload size guard (27KB / 180 nodes) before returning.

    Args:
        header_title: Card header title text.
        header_template: Feishu header color template.
        header_subtitle: Optional subtitle text below the title.
        elements: Card body elements.
        mobile_optimize: If True, disables wide_screen_mode for mobile.
    """
    header: dict = {
        "title": {"tag": "plain_text", "content": header_title},
        "template": header_template,
    }
    if header_subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": header_subtitle}

    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": not mobile_optimize},
        "header": header,
        "body": {"elements": elements},
    }
    return _guard_card_payload(card)


def build_column_set_row(
    columns: list[dict],
    *,
    flex_mode: str = "bisect",
    background_style: str = "default",
    horizontal_spacing: str = "default",
    margin: str = "0px",
) -> dict:
    """Build a Feishu column_set row element for multi-column layouts.

    Args:
        columns: List of column dicts. Each should have ``tag: "column"``,
            ``width`` (e.g. "weighted"), ``weight`` (int), and ``elements``.
        flex_mode: How columns distribute space — "bisect", "trisect",
            "flow", or "none".
        background_style: Row background — "default", "grey", etc.
        horizontal_spacing: Spacing between columns.
        margin: CSS margin around the row.
    """
    return {
        "tag": "column_set",
        "flex_mode": flex_mode,
        "background_style": validate_background_style(background_style),
        "horizontal_spacing": horizontal_spacing,
        "margin": margin,
        "columns": columns,
    }


def build_column(
    elements: list[dict],
    *,
    width: str = "weighted",
    weight: int = 1,
    vertical_align: str = "center",
) -> dict:
    """Build a single column element for use inside column_set.

    Args:
        elements: Child elements inside the column.
        width: Column width mode — "weighted" or "auto".
        weight: Relative weight when width is "weighted".
        vertical_align: Vertical alignment — "top", "center", "bottom".
    """
    return {
        "tag": "column",
        "width": width,
        "weight": weight,
        "vertical_align": vertical_align,
        "elements": elements,
    }


def build_mobile_card_row(
    *,
    title_elements: list[dict],
    content_elements: list[dict] | None = None,
    action_elements: list[dict] | None = None,
    background_style: str = "default",
    margin: str = "4px 0px",
) -> dict:
    """Build a mobile-friendly single-column card row.

    Stacks title, content, and action rows vertically for optimal
    mobile display without horizontal overflow.

    Args:
        title_elements: First row elements (e.g. avatar + name + status).
        content_elements: Second row elements (e.g. task + skills + progress).
        action_elements: Third row elements (e.g. buttons).
        background_style: Row background — "default", "grey", etc.
        margin: CSS margin around the container.
    """
    rows: list[dict] = []

    # Title row — always present
    rows.append({
        "tag": "column_set",
        "flex_mode": "flow",
        "background_style": validate_background_style(background_style),
        "margin": "0px",
        "columns": [
            build_column(title_elements, width="weighted", weight=1),
        ],
    })

    # Content row — optional
    if content_elements:
        rows.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "background_style": "default",
            "margin": "0px",
            "columns": [
                build_column(content_elements, width="weighted", weight=1),
            ],
        })

    # Action row — optional
    if action_elements:
        rows.append({
            "tag": "column_set",
            "flex_mode": "flow",
            "background_style": "default",
            "margin": "0px",
            "columns": [
                build_column(action_elements, width="weighted", weight=1),
            ],
        })

    # Wrap in a container div for spacing
    return {
        "tag": "div",
        "elements": rows,
        "margin": margin,
    }
