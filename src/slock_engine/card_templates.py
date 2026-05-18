"""Card templates for Slock Engine — Agent identity cards and status panels.

Uses CoreBuilder._wrap_card pattern from the existing card system.
"""

from __future__ import annotations

from typing import Optional

from .models import AGENT_ROLE_COLORS, AgentIdentity, AgentStatus, SlockTask, TaskStatus


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


def build_status_panel_card(
    agents: list[tuple[AgentIdentity, AgentStatus]],
    team_name: str = "",
    channel_id: str = "",
) -> dict:
    """Build a status panel card showing all agents and their states.

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
        # Build agent status rows
        rows: list[str] = []
        for agent, status in agents:
            status_icon = _STATUS_ICON_MAP.get(status, "⚪")
            status_label = status.value.capitalize()
            rows.append(f"{agent.emoji} **{agent.name}** — {status_icon} {status_label}")

        elements.append({"tag": "markdown", "content": "\n".join(rows)})

    # Action button for refresh
    elements.append({
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "🔄 Refresh"},
                "type": "primary_text",
                "value": {"action": "slock_refresh_status", "channel_id": channel_id},
            }
        ],
    })

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

_TASK_STATUS_ICONS: dict[TaskStatus, str] = {
    TaskStatus.TODO: "⬜",
    TaskStatus.IN_PROGRESS: "🔵",
    TaskStatus.IN_REVIEW: "🟡",
    TaskStatus.DONE: "✅",
}
