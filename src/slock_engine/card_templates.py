"""Card templates for Slock Engine — Agent identity cards and status panels.

Uses CoreBuilder._wrap_card pattern from the existing card system.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

from src.card.shared import apply_compact_style, build_responsive_layout
from src.utils.redact import redact_sensitive

from .models import (
    ABORT_OPTIONS,
    AgentIdentity,
    AgentStatus,
    EscalationLevel,
    EscalationRequest,
    SlockTask,
    TaskStatus,
)

_DISPLAY_TZ = ZoneInfo("Asia/Shanghai")

_STATUS_LABEL_ZH: dict[str, str] = {
    "idle": "空闲",
    "waking": "唤醒中",
    "thinking": "思考中",
    "running": "运行中",
    "checking": "检查中",
    "sending": "发送中",
    "moving": "迁移中",
}

_TASK_STATUS_LABEL_ZH: dict[str, str] = {
    "todo": "待办",
    "in_progress": "进行中",
    "in_review": "审查中",
    "done": "已完成",
}


def build_agent_message_card(
    agent: AgentIdentity,
    content: str,
    *,
    model_info: str = "",
    duration_s: Optional[float] = None,
    channel_id: str = "",
    task_id: str = "",
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
            "tag": "markdown",
            "content": " | ".join(footer_parts),
            "text_size": "notation",
        })

    action_value = {
        "channel_id": channel_id,
        "agent_id": agent.agent_id,
        "agent_name": agent.name,
        "task_id": task_id,
    }
    elements.extend(
        build_responsive_layout(
            [
                _build_callback_button(
                    "@追问",
                    "slock_agent_follow_up",
                    channel_id=channel_id,
                    extra_value=action_value,
                ),
                _build_callback_button(
                    "查看推理",
                    "slock_agent_show_reasoning",
                    channel_id=channel_id,
                    extra_value=action_value,
                ),
                _build_callback_button(
                    "标记完成",
                    "slock_agent_mark_done",
                    channel_id=channel_id,
                    button_type="primary_text",
                    extra_value=action_value,
                ),
            ]
        )
    )

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


def build_welcome_card(*, team_name: str) -> dict:
    """Build a welcome card sent inside the newly created Slock team group."""
    content = (
        f"🎭 **Slock 协作团队「{team_name}」已就绪**\n\n"
        "📌 **快速开始**:\n"
        "• `/new-role <名称>` — 创建虚拟 Agent\n"
        "• `/role list` — 查看所有角色\n"
        "• `/task assign <任务> <角色>` — 分配任务\n"
        "• `/task status` — 查看任务看板\n"
        "• `/slock status` — 查看团队状态\n"
        "• `/slock help` — 查看所有命令"
    )
    elements: list[dict] = [{"tag": "markdown", "content": content}]
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": f"👋 欢迎加入 {team_name}"}, "template": "green"},
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
    current_tasks: Optional[dict[str, SlockTask]] = None,
) -> dict:
    """Build a status panel card showing all agents and their states.

    Uses Feishu column_set components with colored backgrounds for native
    status color-coding: green=IDLE, yellow=THINKING, blue=RUNNING, grey=SENDING.

    Args:
        agents: List of (AgentIdentity, AgentStatus) tuples.
        team_name: Optional team name for the header.
        channel_id: Optional channel identifier.
        current_tasks: Optional agent_id to active task mapping.
    """
    header_title = f"📊 {team_name} Agent 状态" if team_name else "📊 Slock Agent 状态"

    elements: list[dict] = []

    if not agents:
        elements.append({"tag": "markdown", "content": "*当前团队暂无已注册的 Agent。*"})
    else:
        # Build column_set rows — one per agent with colored status indicator
        for agent, status in agents:
            status_icon = _STATUS_ICON_MAP.get(status, "⚪")
            status_label = _STATUS_LABEL_ZH.get(status.value, status.value)
            bg_color = _STATUS_BG_COLOR_MAP.get(status, "grey")
            current_task = (current_tasks or {}).get(agent.agent_id)
            agent_content = f"{agent.emoji} **{agent.name}** — {status_icon}"
            if current_task:
                task_text = current_task.content[:80]
                agent_content += f"\n当前任务: {task_text}"

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
                                "content": agent_content,
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
                    "🔄 刷新",
                    "slock_refresh_status",
                    channel_id=channel_id,
                    button_type="primary_text",
                ),
                _build_callback_button(
                    "⏹ 全部停止",
                    "slock_stop",
                    channel_id=channel_id,
                    button_type="danger",
                ),
            ]
            + [
                _build_callback_button(
                    f"⏹ 停止 {agent.name}",
                    "slock_stop_agent",
                    channel_id=channel_id,
                    button_type="danger",
                    extra_value={"agent_id": agent.agent_id},
                )
                for agent, status in agents
                if status not in (AgentStatus.IDLE,)
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
    channel_id: str = "",
) -> dict:
    """Build a Kanban-style task board card using column_set with colored backgrounds.

    Each TaskStatus occupies one row as a column_set with background_style color-coding:
    TODO=grey, IN_PROGRESS=blue, IN_REVIEW=yellow, DONE=green.
    Inside each row, a two-column layout separates the status header from the task list.
    """
    header_title = f"📋 {team_name} 任务看板" if team_name else "📋 Slock 任务看板"

    elements: list[dict] = []

    # Group tasks by status
    grouped: dict[TaskStatus, list[SlockTask]] = {s: [] for s in TaskStatus}
    for task in tasks:
        grouped[task.status].append(task)

    # Build each status row as a column_set with colored background
    agent_map = {a.agent_id: a for a in agents}

    for status in TaskStatus:
        status_tasks = grouped[status]
        icon = _TASK_STATUS_ICONS.get(status, "⬜")
        bg_color = _TASK_STATUS_BG_COLOR_MAP.get(status, "grey")
        status_title = f"**{icon} {_TASK_STATUS_LABEL_ZH.get(status.value, status.value)}** ({len(status_tasks)})"

        # Build task list content for the right column
        if status_tasks:
            task_lines: list[str] = []
            for task in status_tasks[:5]:  # limit display
                assignee = ""
                if task.claimed_by and task.claimed_by in agent_map:
                    a = agent_map[task.claimed_by]
                    assignee = f" → {a.emoji}{a.name}"
                # Differentiate aborted tasks from normal completion
                if status == TaskStatus.DONE and task.resolved_reason:
                    task_lines.append(f"• ⚠️ ~~{task.content[:60]}~~{assignee}")
                else:
                    task_lines.append(f"• {task.content[:60]}{assignee}")
            task_content = "\n".join(task_lines)
        else:
            task_content = "*暂无*"

        column_set: dict = {
            "tag": "column_set",
            "flex_mode": "bisect",
            "background_style": bg_color,
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 1,
                    "vertical_align": "top",
                    "elements": [
                        {"tag": "markdown", "content": status_title},
                    ],
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 3,
                    "vertical_align": "top",
                    "elements": [
                        {"tag": "markdown", "content": task_content},
                    ],
                },
            ],
        }
        elements.append(column_set)

    # Refresh button
    elements.extend(
        build_responsive_layout(
            [
                _build_callback_button(
                    "🔄 刷新",
                    "slock_refresh_task_board",
                    channel_id=channel_id,
                    button_type="primary_text",
                    extra_value={"chat_id": channel_id},
                ),
            ]
        )
    )

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
# Agent move notification
# ------------------------------------------------------------------


def build_agent_move_departure_card(
    agent: AgentIdentity,
    target_team: str,
) -> dict:
    """Build a departure notification card sent to the SOURCE group.

    Informs all members that an agent has been moved out.  Uses orange header
    to signal a change event (not green/completion).  No jump button — keeps
    the card simple and informational.

    Args:
        agent: The agent identity being moved away.
        target_team: Display name of the target team/group.
    """
    from datetime import datetime

    content = (
        f"{agent.emoji} **{agent.name}** 已迁出至「{target_team}」\n\n"
        "角色记忆与技能画像已随迁，如需协作请前往目标团队。"
    )

    elements: list[dict] = [{"tag": "markdown", "content": content}]

    # Footer note
    footer_parts: list[str] = []
    if agent.agent_type:
        footer_parts.append(agent.agent_type)
    if agent.model_name:
        footer_parts.append(agent.model_name)
    footer_parts.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    elements.append({
        "tag": "markdown",
        "content": " | ".join(footer_parts),
        "text_size": "notation",
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"➡️ 角色迁出: {agent.name}"},
            "template": "orange",
        },
        "body": {"elements": elements},
    }


def build_agent_move_notification_card(
    agent: AgentIdentity,
    source_team: str,
    target_team: str,
    *,
    operator_display: str = "",
) -> dict:
    """Build a notification card for an agent being moved to a new team.

    Sent to the TARGET group to inform members about the newly arrived agent.
    Does not include a jump button (the target group IS the destination).

    Args:
        agent: The agent identity being moved.
        source_team: Display name of the source team/group.
        target_team: Display name of the target team/group.
        operator_display: Display name of the operator who initiated the move.
    """
    operator_line = f"由 {operator_display} 迁移至此" if operator_display else "已迁移至此"
    content = (
        f"{agent.emoji} **{agent.name}** 已从「{source_team}」迁移至本团队\n\n"
        f"• 角色: {agent.role or 'custom'}\n"
        f"• 工具: {agent.agent_type}\n"
        f"• 模型: {agent.model_name or '默认'}\n"
        f"• {operator_line}\n\n"
        "角色定义、关键知识与技能画像已保留；活跃上下文已按跨群策略重置，可立即参与任务分配。"
    )

    elements: list[dict] = [{"tag": "markdown", "content": content}]

    # Footer note: agent_type | model_name | timestamp
    from datetime import datetime

    footer_parts: list[str] = []
    if agent.agent_type:
        footer_parts.append(agent.agent_type)
    if agent.model_name:
        footer_parts.append(agent.model_name)
    footer_parts.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    elements.append({
        "tag": "markdown",
        "content": " | ".join(footer_parts),
        "text_size": "notation",
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"👋 角色加入: {agent.name}"},
            "template": "indigo",
        },
        "body": {"elements": elements},
    }


def build_agent_move_confirm_card(
    agent: AgentIdentity,
    source_team: str,
    target_team: str,
    target_channel_id: str,
) -> dict:
    """Build a confirmation card sent to the SOURCE group after a successful move.

    Includes a jump button pointing to the target group so the operator can
    quickly follow the agent to its new home.

    Args:
        agent: The agent identity that was moved.
        source_team: Display name of the source team/group.
        target_team: Display name of the target team/group.
        target_channel_id: Channel ID of the target group for jump button.
    """
    content = (
        f"✅ 角色 **{agent.emoji} {agent.name}** 已成功移动到团队「{target_team}」\n\n"
        f"• 角色: {agent.role or 'custom'}\n"
        f"• 工具: {agent.agent_type}\n"
        f"• 模型: {agent.model_name or '默认'}\n\n"
        "角色定义与技能画像已保留，活跃上下文已按跨群隐私策略脱敏。\n\n"
        "ℹ️ Active Context 已按跨群策略重置以保护源团队隐私"
    )

    elements: list[dict] = [{"tag": "markdown", "content": content}]

    if target_channel_id:
        elements.extend(
            build_responsive_layout([_build_slock_group_jump_button(target_channel_id)])
        )

    # Footer note: agent_type | model_name | timestamp
    from datetime import datetime

    footer_parts: list[str] = []
    if agent.agent_type:
        footer_parts.append(agent.agent_type)
    if agent.model_name:
        footer_parts.append(agent.model_name)
    footer_parts.append(datetime.now().strftime("%Y-%m-%d %H:%M"))
    elements.append({
        "tag": "markdown",
        "content": " | ".join(footer_parts),
        "text_size": "notation",
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✅ 角色迁移完成: {agent.name}"},
            "template": "green",
        },
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
    AgentStatus.MOVING: "🔶",
}

# Background color mapping for column_set status rows (Feishu card background_style)
_STATUS_BG_COLOR_MAP: dict[AgentStatus, str] = {
    AgentStatus.IDLE: "green",
    AgentStatus.WAKING: "yellow",
    AgentStatus.THINKING: "yellow",
    AgentStatus.RUNNING: "blue",
    AgentStatus.CHECKING: "blue",
    AgentStatus.SENDING: "grey",
    AgentStatus.MOVING: "orange",
}

_TASK_STATUS_ICONS: dict[TaskStatus, str] = {
    TaskStatus.TODO: "⬜",
    TaskStatus.IN_PROGRESS: "🔵",
    TaskStatus.IN_REVIEW: "🟡",
    TaskStatus.DONE: "✅",
}

# Background color mapping for task board Kanban columns (Feishu card background_style)
_TASK_STATUS_BG_COLOR_MAP: dict[TaskStatus, str] = {
    TaskStatus.TODO: "grey",
    TaskStatus.IN_PROGRESS: "blue",
    TaskStatus.IN_REVIEW: "yellow",
    TaskStatus.DONE: "green",
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
    extra_value: dict | None = None,
) -> dict:
    value = {"action": action, "channel_id": channel_id}
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


def build_escalation_card(
    escalation: EscalationRequest,
    *,
    channel_id: str = "",
    timeout_minutes: Optional[int] = None,
) -> dict:
    """Build an escalation card requesting admin intervention.

    Shows the agent name, severity level, reason, and resolution option buttons.
    """
    level_icons = {
        EscalationLevel.WARNING: "⚠️",
        EscalationLevel.BLOCKED: "🚫",
        EscalationLevel.CRITICAL: "🔴",
    }
    level_colors = {
        EscalationLevel.WARNING: "yellow",
        EscalationLevel.BLOCKED: "orange",
        EscalationLevel.CRITICAL: "red",
    }

    icon = level_icons.get(escalation.level, "⚠️")
    header_color = level_colors.get(escalation.level, "orange")
    header_title = f"{icon} 升级告警: {escalation.agent_name or 'Agent'}"

    elements: list[dict] = []

    # Severity and reason (redact sensitive info before rendering)
    safe_reason = redact_sensitive(escalation.reason)
    elements.append({
        "tag": "markdown",
        "content": (
            f"**级别:** {escalation.level.value.upper()}\n"
            f"**代理:** {escalation.agent_name} (`{escalation.agent_id}`)\n"
            f"**原因:** {safe_reason}"
        ),
    })

    # Context details (truncated + redacted)
    if escalation.context:
        context_display = redact_sensitive(escalation.context[:500])
        if len(escalation.context) > 500:
            context_display += "\n..."
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**上下文:**\n```\n{context_display}\n```",
        })

    # Task reference
    if escalation.task_id:
        elements.append({
            "tag": "markdown",
            "content": f"**任务:** `{escalation.task_id}`",
        })

    # Timeout hint
    if timeout_minutes is not None:
        elements.append({
            "tag": "markdown",
            "content": f"⏰ 此升级将在 {timeout_minutes} 分钟后自动中止",
            "text_size": "notation",
        })

    elements.append({"tag": "hr"})

    # Resolution option buttons
    option_buttons: list[dict] = []
    default_options = escalation.options or ["重试", "跳过", "中止"]
    for option in default_options:
        value = {
            "action": "slock_escalation_resolve",
            "escalation_id": escalation.escalation_id,
            "resolution": option,
            "channel_id": channel_id,
        }
        btn_type = "danger" if option in ABORT_OPTIONS else "default"
        option_buttons.append(apply_compact_style({
            "tag": "button",
            "text": {"tag": "plain_text", "content": option},
            "type": btn_type,
            "value": value,
            "behaviors": [{"type": "callback", "value": value}],
        }))

    if option_buttons:
        elements.extend(build_responsive_layout(option_buttons))

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": header_color,
        },
        "body": {"elements": elements},
    }


def build_resolved_escalation_card(
    escalation: EscalationRequest,
    *,
    resolved_by: str = "",
    resolution: str = "",
    resolved_at: Optional[float] = None,
    channel_id: str = "",
) -> dict:
    """Build an escalation card in resolved state — buttons removed, status shown.

    Args:
        escalation: The original EscalationRequest.
        resolved_by: Display name or ID of the operator who resolved it.
        resolution: The chosen resolution (e.g. Retry/Skip/Abort).
        resolved_at: Timestamp of resolution (epoch seconds).
        channel_id: Optional channel identifier.
    """
    import time as _time

    level_icons = {
        EscalationLevel.WARNING: "⚠️",
        EscalationLevel.BLOCKED: "🚫",
        EscalationLevel.CRITICAL: "🔴",
    }

    icon = level_icons.get(escalation.level, "⚠️")
    header_title = f"{icon} 升级告警: {escalation.agent_name or 'Agent'} [已解决]"

    elements: list[dict] = []

    # Original severity and reason
    elements.append({
        "tag": "markdown",
        "content": (
            f"**级别:** {escalation.level.value.upper()}\n"
            f"**代理:** {escalation.agent_name} (`{escalation.agent_id}`)\n"
            f"**原因:** {redact_sensitive(escalation.reason)}"
        ),
    })

    # Context details (truncated) — keep for reference
    if escalation.context:
        context_display = redact_sensitive(escalation.context[:500])
        if len(escalation.context) > 500:
            context_display += "\n..."
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**上下文:**\n```\n{context_display}\n```",
        })

    # Task reference
    if escalation.task_id:
        elements.append({
            "tag": "markdown",
            "content": f"**任务:** `{escalation.task_id}`",
        })

    elements.append({"tag": "hr"})

    # Resolution status (replaces buttons)
    ts = resolved_at or _time.time()
    from datetime import datetime
    time_str = datetime.fromtimestamp(ts, tz=_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    operator_display = resolved_by or "未知"

    elements.append({
        "tag": "markdown",
        "content": f"✅ **已解决:** {resolution}，由 {operator_display} 处理\n📅 {time_str}",
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": "green",
        },
        "body": {"elements": elements},
    }
