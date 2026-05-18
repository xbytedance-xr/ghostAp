"""Card templates for Slock Engine — Agent identity cards and status panels.

Uses CoreBuilder._wrap_card pattern from the existing card system.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote

from src.card.shared import apply_compact_style, build_responsive_layout

from .models import AgentIdentity, AgentStatus, SlockTask, TaskStatus


def build_agent_message_card(
    agent: AgentIdentity,
    content: str,
    *,
    model_info: str = "",
    duration_s: Optional[float] = None,
) -> dict:
    """Build an Interactive Card for an agent's message (mouthpiece output).

    Args:
        agent: The agent identity sending the message.
        content: Markdown content of the message.
        model_info: Optional model identifier for footer.
        duration_s: Optional processing duration in seconds.
    """
    header_title = agent.display_name
    header_template = agent.card_color

    elements: list[dict] = []

    # Main content
    elements.append({"tag": "markdown", "content": content})

    # Footer note with metadata
    footer_parts: list[str] = []
    if agent.agent_type:
        footer_parts.append(agent.agent_type)
    if model_info:
        footer_parts.append(model_info)
    elif agent.model_name:
        footer_parts.append(agent.model_name)
    if duration_s is not None:
        if duration_s < 60:
            footer_parts.append(f"{duration_s:.1f}s")
        else:
            minutes = int(duration_s // 60)
            secs = duration_s % 60
            footer_parts.append(f"{minutes}m{secs:.0f}s")

    if footer_parts:
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "plain_text", "content": " | ".join(footer_parts)}
            ],
        })

    header: dict = {
        "title": {"tag": "plain_text", "content": header_title},
        "template": header_template,
    }

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": header,
        "body": {"elements": elements},
    }


def build_team_created_card(
    *,
    team_name: str,
    group_name: str,
    channel_id: str,
) -> dict:
    """Build the /new-team confirmation card with a direct group jump button."""
    content = (
        f"✅ 团队 **{team_name}** 已创建\n\n"
        f"已建立专属协作群「{group_name}」并激活 Slock 运行时\n"
        "• 事件监听: ✓\n"
        "• Agent 调度器: ✓\n"
        "• 工作区目录: ✓\n\n"
        "请前往新群开始协作"
    )
    elements: list[dict] = [{"tag": "markdown", "content": content}]
    elements.extend(build_responsive_layout([_build_slock_group_jump_button(channel_id)]))
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🎭 Slock 协作群已创建"}, "template": "indigo"},
        "body": {"elements": elements},
    }


def build_team_list_card(teams: list[dict]) -> dict:
    """Build a Slock team directory card with jump buttons."""
    elements: list[dict] = []
    for team in teams:
        team_name = str(team.get("team_name") or team.get("name") or team.get("channel_id") or "")
        channel_id = str(team.get("channel_id") or "")
        agent_count = int(team.get("agent_count") or 0)
        task_count = int(team.get("task_count") or 0)
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"**{team_name}**\n"
                    f"{agent_count} 个 Agent · {task_count} 个任务 · 频道: `{channel_id}`"
                ),
            }
        )
        if channel_id:
            elements.extend(build_responsive_layout([_build_slock_group_jump_button(channel_id)]))
        elements.append({"tag": "hr"})

    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🎭 Slock 协作群"}, "template": "indigo"},
        "body": {"elements": elements},
    }


def build_status_panel_card(
    agents: list[tuple[AgentIdentity, AgentStatus]],
    team_name: str = "",
    channel_id: str = "",
) -> dict:
    """Build a status panel card showing all agents and their states.

    Uses Feishu column_set components with colored backgrounds for native
    status color-coding: green=IDLE, yellow=THINKING, blue=RUNNING, grey=SENDING.

    Args:
        agents: List of (AgentIdentity, AgentStatus) tuples.
        team_name: Optional team name for the header.
        channel_id: Optional channel identifier.
    """
    header_title = f"📊 {team_name} Agent Status" if team_name else "📊 Slock Agent Status"

    elements: list[dict] = []

    if not agents:
        elements.append({"tag": "markdown", "content": "*No agents registered in this team.*"})
    else:
        # Build column_set rows — one per agent with colored status indicator
        for agent, status in agents:
            status_icon = _STATUS_ICON_MAP.get(status, "⚪")
            status_label = status.value.capitalize()
            bg_color = _STATUS_BG_COLOR_MAP.get(status, "grey")

            column_set: dict = {
                "tag": "column_set",
                "flex_mode": "bisect",
                "background_style": bg_color,
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 3,
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": f"{agent.emoji} **{agent.name}** — {status_icon}",
                            }
                        ],
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "vertical_align": "center",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": f"**{status_label}**",
                                "text_align": "right",
                            }
                        ],
                    },
                ],
            }
            elements.append(column_set)

    elements.extend(
        build_responsive_layout(
            [
                _build_callback_button(
                    "🔄 Refresh",
                    "slock_refresh_status",
                    channel_id=channel_id,
                    button_type="primary_text",
                ),
                _build_callback_button(
                    "⏹ Stop",
                    "slock_stop",
                    channel_id=channel_id,
                    button_type="danger",
                ),
            ]
        )
    )

    header: dict = {
        "title": {"tag": "plain_text", "content": header_title},
        "template": "indigo",
    }

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": header,
        "body": {"elements": elements},
    }


def build_task_board_card(
    tasks: list[SlockTask],
    agents: list[AgentIdentity],
    team_name: str = "",
) -> dict:
    """Build a Kanban-style task board card."""
    header_title = f"📋 {team_name} Task Board" if team_name else "📋 Slock Task Board"

    elements: list[dict] = []

    # Group tasks by status
    grouped: dict[TaskStatus, list[SlockTask]] = {s: [] for s in TaskStatus}
    for task in tasks:
        grouped[task.status].append(task)

    # Build each column
    agent_map = {a.agent_id: a for a in agents}

    for status in TaskStatus:
        status_tasks = grouped[status]
        icon = _TASK_STATUS_ICONS.get(status, "⬜")
        section = f"**{icon} {status.value.replace('_', ' ').title()}** ({len(status_tasks)})"

        if status_tasks:
            for task in status_tasks[:5]:  # limit display
                assignee = ""
                if task.claimed_by and task.claimed_by in agent_map:
                    a = agent_map[task.claimed_by]
                    assignee = f" → {a.emoji}{a.name}"
                section += f"\n• {task.content[:60]}{assignee}"
        else:
            section += "\n*empty*"

        elements.append({"tag": "markdown", "content": section})
        elements.append({"tag": "hr"})

    # Remove trailing hr
    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    header: dict = {
        "title": {"tag": "plain_text", "content": header_title},
        "template": "wathet",
    }

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": header,
        "body": {"elements": elements},
    }


# ------------------------------------------------------------------
# Internal constants
# ------------------------------------------------------------------

_STATUS_ICON_MAP: dict[AgentStatus, str] = {
    AgentStatus.IDLE: "🟢",
    AgentStatus.WAKING: "🟡",
    AgentStatus.THINKING: "🟡",
    AgentStatus.RUNNING: "🔵",
    AgentStatus.CHECKING: "🔵",
    AgentStatus.SENDING: "⚪",
}

# Background color mapping for column_set status rows (Feishu card background_style)
_STATUS_BG_COLOR_MAP: dict[AgentStatus, str] = {
    AgentStatus.IDLE: "green",
    AgentStatus.WAKING: "yellow",
    AgentStatus.THINKING: "yellow",
    AgentStatus.RUNNING: "blue",
    AgentStatus.CHECKING: "blue",
    AgentStatus.SENDING: "grey",
}

_TASK_STATUS_ICONS: dict[TaskStatus, str] = {
    TaskStatus.TODO: "⬜",
    TaskStatus.IN_PROGRESS: "🔵",
    TaskStatus.IN_REVIEW: "🟡",
    TaskStatus.DONE: "✅",
}


def _build_slock_group_jump_button(channel_id: str) -> dict:
    return apply_compact_style(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": "进入 Slock 群"},
            "type": "primary",
            "multi_url": _build_chat_multi_url(channel_id),
        }
    )


def _build_chat_multi_url(chat_id: str) -> dict:
    safe_chat_id = quote(str(chat_id or "").strip(), safe="")
    https = f"https://applink.feishu.cn/client/chat/open?openChatId={safe_chat_id}"
    native = f"lark://applink/client/chat/open?openChatId={safe_chat_id}"
    return {
        "url": https,
        "pc_url": https,
        "android_url": native,
        "ios_url": native,
    }


def _build_callback_button(
    text: str,
    action: str,
    *,
    channel_id: str = "",
    button_type: str = "default",
) -> dict:
    value = {"action": action, "channel_id": channel_id}
    return apply_compact_style(
        {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": button_type,
            "value": value,
            "behaviors": [{"type": "callback", "value": value}],
        }
    )
