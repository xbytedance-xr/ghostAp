"""Task board card templates for Slock Engine.

Provides the redesigned /task list and /task status cards with Kanban view:
Todo → In Progress → In Review → Done.
"""

from __future__ import annotations

from datetime import datetime

from .common import (
    DISPLAY_TZ,
    TASK_CONTENT_PREVIEW_LEN,
    TASK_STATUS_ICONS,
    TASK_STATUS_LABEL_ZH,
    build_callback_button,
    build_card_wrapper,
    build_collapsible_panel,
    build_column,
    build_column_set_row,
    build_responsive_layout,
)
from ..models import (
    AgentIdentity,
    SlockTask,
    TaskStatus,
)


def build_task_board_card(
    tasks: list[SlockTask],
    agents: list[AgentIdentity],
    team_name: str = "",
    channel_id: str = "",
    summary_mode: bool = False,
) -> dict:
    """Build a Kanban-style task board card.

    Layout:
        Header (team name + task count summary)
        → Kanban Columns (Todo | In Progress | In Review | Done)
        → Each task shows: responsible agent emoji, collaborators, progress, output summary
        → Action Buttons (new task, refresh)

    Args:
        tasks: All tasks in the channel.
        agents: All agents for resolving agent_id → display info.
        team_name: Optional team name.
        channel_id: Channel for action routing.
        summary_mode: If True, show only counts per column (for status panel embedding).
    """
    header_title = f"📋 {team_name} 任务看板" if team_name else "📋 任务看板"

    # Build agent lookup
    agent_map: dict[str, AgentIdentity] = {a.agent_id: a for a in agents}

    # Group tasks by status
    columns: dict[TaskStatus, list[SlockTask]] = {
        TaskStatus.TODO: [],
        TaskStatus.IN_PROGRESS: [],
        TaskStatus.IN_REVIEW: [],
        TaskStatus.DONE: [],
    }
    for task in tasks:
        if task.status in columns:
            columns[task.status].append(task)

    elements: list[dict] = []

    # -- Summary line (column_set: one column per status) --
    total = len(tasks)
    counts = {s: len(ts) for s, ts in columns.items()}

    elements.append({
        "tag": "markdown",
        "content": f"共 **{total}** 个任务",
    })

    summary_columns = []
    for status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW, TaskStatus.DONE):
        icon = TASK_STATUS_ICONS.get(status, "⬜")
        label = TASK_STATUS_LABEL_ZH.get(status.value, status.value)
        summary_columns.append(
            build_column(
                [{"tag": "markdown", "content": f"{icon} {label}\n**{counts[status]}**"}],
                weight=1,
            )
        )
    # Vertical rows avoid narrow-screen truncation and legacy bisect layouts.
    for summary_column in summary_columns:
        elements.append(build_column_set_row([summary_column], flex_mode="none"))

    if summary_mode:
        # In summary mode, just show counts and a link button
        elements.append({"tag": "hr"})
        elements.extend(
            build_responsive_layout([
                build_callback_button(
                    "📋 查看完整看板",
                    "slock_show_task_board",
                    channel_id=channel_id,
                    button_type="primary",
                ),
            ])
        )
        return build_card_wrapper(
            header_title=header_title,
            header_template="wathet",
            elements=elements,
        )

    # -- Kanban Columns --
    for status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW, TaskStatus.DONE):
        column_tasks = columns[status]
        icon = TASK_STATUS_ICONS.get(status, "⬜")
        label = TASK_STATUS_LABEL_ZH.get(status.value, status.value)

        if not column_tasks:
            continue

        column_elements: list[dict] = []
        # Limit displayed tasks per column (avoid card bloat)
        display_tasks = column_tasks[:8]
        overflow = len(column_tasks) - len(display_tasks)

        for task in display_tasks:
            task_entry = _build_task_entry(task, agent_map)
            column_elements.append(task_entry)

        if overflow > 0:
            column_elements.append({
                "tag": "markdown",
                "content": f"*...还有 {overflow} 个任务*",
                "text_size": "notation",
            })

        # Use collapsible panel for each column
        is_active_column = status in (TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW)
        elements.append(
            build_collapsible_panel(
                f"**{icon} {label}** ({len(column_tasks)})",
                column_elements,
                expanded=is_active_column,
            )
        )

    # -- Action Buttons --
    elements.append({"tag": "hr"})
    action_buttons = [
        build_callback_button(
            "➕ 新建任务",
            "slock_new_task",
            channel_id=channel_id,
            button_type="primary",
        ),
        build_callback_button(
            "🔄 刷新",
            "slock_refresh_task_board",
            channel_id=channel_id,
            button_type="default",
        ),
        build_callback_button(
            "🚀 自动分配",
            "slock_dispatch_tasks",
            channel_id=channel_id,
            button_type="default",
        ),
    ]
    elements.extend(build_responsive_layout(action_buttons))

    return build_card_wrapper(
        header_title=header_title,
        header_template="wathet",
        elements=elements,
    )


def _build_task_entry(task: SlockTask, agent_map: dict[str, AgentIdentity]) -> dict:
    """Build a single task entry wrapped in a grey background container.

    Each entry contains:
        1. The formatted task row (agent, content, collaborators, progress).
        2. An annotation line: first 8 chars of task_id + creation time.

    Returns a column_set element with grey background.
    """
    task_line = _format_task_row(task, agent_map)

    # Build annotation: task_id prefix + creation time
    task_id_short = task.task_id[:8] if task.task_id else "--------"
    created_time = datetime.fromtimestamp(task.created_at, tz=DISPLAY_TZ).strftime(
        "%m-%d %H:%M"
    )
    annotation = f"{task_id_short} · {created_time}"

    entry_elements = [
        {"tag": "markdown", "content": task_line},
        {
            "tag": "markdown",
            "content": annotation,
            "text_size": "notation",
            "text_align": "right",
        },
    ]

    return build_column_set_row(
        [build_column(entry_elements, weight=1)],
        flex_mode="none",
        background_style="grey",
        margin="4px 0px",
    )


def _format_task_row(task: SlockTask, agent_map: dict[str, AgentIdentity]) -> str:
    """Format a single task as a compact markdown row.

    Format: [agent_emoji] task_content (50 chars) | collaborators | progress
    """
    # Responsible agent
    agent_emoji = "👤"
    agent_name = ""
    if task.claimed_by:
        agent = agent_map.get(task.claimed_by)
        if agent:
            agent_emoji = agent.emoji
            agent_name = agent.name

    # Task content (truncated)
    content = task.content[:TASK_CONTENT_PREVIEW_LEN]
    if len(task.content) > TASK_CONTENT_PREVIEW_LEN:
        content += "…"

    # Collaborators
    collab_text = ""
    if task.collaborators:
        collab_emojis = []
        for cid in task.collaborators[:3]:
            ca = agent_map.get(cid)
            if ca:
                collab_emojis.append(ca.emoji)
        if collab_emojis:
            collab_text = f"　👥 {''.join(collab_emojis)}"

    # Progress
    progress_text = ""
    if task.progress_pct > 0:
        progress_text = f"　⏳ {task.progress_pct}%"

    # Build row
    parts = [f"{agent_emoji}"]
    if agent_name:
        parts.append(f"**{agent_name}**:")
    parts.append(content)

    row = " ".join(parts)
    if collab_text:
        row += collab_text
    if progress_text:
        row += progress_text

    # Predecessor breadcrumb
    if task.predecessor_agent_name:
        row += f"\n　　↪️ 前序: {task.predecessor_agent_name}"

    return row
