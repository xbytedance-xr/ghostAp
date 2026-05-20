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
    CouncilRun,
    CouncilStatus,
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
    "discussing": "讨论中",
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
    discussion_enabled: bool = True,
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

    # Row 1: core action buttons
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
                    "让TA继续",
                    "slock_agent_continue",
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

    # Row 2: secondary buttons (text style, visually lighter)
    secondary_buttons = [
        _build_callback_button(
            "查看推理",
            "slock_agent_show_reasoning",
            channel_id=channel_id,
            button_type="text",
            extra_value=action_value,
        ),
        _build_callback_button(
            "换个角色",
            "slock_agent_switch_role",
            channel_id=channel_id,
            button_type="text",
            extra_value=action_value,
        ),
    ]
    if discussion_enabled:
        secondary_buttons.append(
            _build_callback_button(
                "开启讨论",
                "slock_start_discussion",
                channel_id=channel_id,
                button_type="text",
                extra_value=action_value,
            )
        )
    elements.extend(build_responsive_layout(secondary_buttons))

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


def build_agent_action_buttons(
    *,
    channel_id: str = "",
    extra_value: dict | None = None,
) -> list[dict]:
    """Create inline action buttons for the agent response card.

    Returns a list of button dicts suitable for build_responsive_layout().

    Buttons:
        - "让TA继续" — action: slock_agent_continue
        - "换个角色" — action: slock_agent_switch_role
        - "开启讨论" — action: slock_start_discussion
    """
    base_value = extra_value or {}
    return [
        _build_callback_button(
            "让TA继续",
            "slock_agent_continue",
            channel_id=channel_id,
            extra_value=base_value,
        ),
        _build_callback_button(
            "换个角色",
            "slock_agent_switch_role",
            channel_id=channel_id,
            extra_value=base_value,
        ),
        _build_callback_button(
            "开启讨论",
            "slock_start_discussion",
            channel_id=channel_id,
            extra_value=base_value,
        ),
    ]


def build_nli_feedback_card(
    intent_description: str,
    channel_id: str,
    intent_params: dict,
) -> dict:
    """Build a confirmation card shown after NLI intent recognition.

    Displays what intent was recognized and offers confirm/cancel buttons.

    Args:
        intent_description: Human-readable description of the recognized intent
            (e.g., "创建一个新角色").
        channel_id: Channel ID for action routing.
        intent_params: Dict of parsed intent parameters to pass back on confirm.
    """
    elements: list[dict] = []

    elements.append({
        "tag": "markdown",
        "content": f"我理解你想：**{intent_description}**",
    })

    action_value = {
        "channel_id": channel_id,
        "intent_params": intent_params,
    }

    elements.extend(
        build_responsive_layout(
            [
                _build_callback_button(
                    "确认执行",
                    "slock_nli_confirm",
                    channel_id=channel_id,
                    button_type="primary",
                    extra_value=action_value,
                ),
                _build_callback_button(
                    "取消",
                    "slock_nli_cancel",
                    channel_id=channel_id,
                    extra_value=action_value,
                ),
            ]
        )
    )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🤔 意图识别"},
            "template": "wathet",
        },
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
        "💬 **直接说就行**:\n"
        "• 「创建一个编码角色」 — 创建虚拟 Agent\n"
        "• 「看看谁在」 — 查看所有角色\n"
        "• 「把代码审查交给 reviewer」 — 分配任务\n"
        "• 「看看任务进度」 — 查看任务看板\n"
        "• 「让 coder 和 reviewer 讨论一下」 — 触发讨论\n\n"
        "---\n"
        "📎 *也支持斜杠命令*: `/new-role`、`/role list`、`/task assign`、`/slock help`"
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
# Discussion cards
# ------------------------------------------------------------------


def build_discussion_card_from_thread(thread, engine=None) -> dict:
    """Factory: build discussion card directly from a DiscussionThread object.

    Extracts fields from the thread dataclass and delegates to
    build_discussion_card (keyword-only signature).
    """
    # Resolve participant IDs to display names
    participants_display = thread.participants
    if engine:
        registry = getattr(engine, 'registry', None) or getattr(engine, '_registry', None)
        if registry:
            resolved = []
            for pid in thread.participants:
                agent = registry.get_agent(pid) if hasattr(registry, 'get_agent') else None
                if agent:
                    display = f"{getattr(agent, 'emoji', '')} {agent.name}".strip()
                    resolved.append(display)
                else:
                    resolved.append(pid)
            participants_display = resolved

    # Resolve message sender IDs
    messages = []
    for m in thread.messages:
        msg_dict = m.to_dict()
        if engine:
            registry = getattr(engine, 'registry', None) or getattr(engine, '_registry', None)
            if registry:
                agent = registry.get_agent(m.sender_agent_id) if hasattr(registry, 'get_agent') else None
                if agent:
                    msg_dict['sender'] = f"{getattr(agent, 'emoji', '')} {agent.name}".strip()
                    msg_dict['sender_display_name'] = msg_dict['sender']
        messages.append(msg_dict)

    return build_discussion_card(
        thread_id=thread.thread_id,
        participants=participants_display,
        messages=messages,
        current_round=thread.current_round,
        max_rounds=thread.config.max_rounds,
        trigger_reason=thread.trigger_reason,
        channel_id=thread.channel_id,
    )


def build_discussion_summary_card_from_thread(thread, engine=None) -> dict:
    """Factory: build discussion summary card from a completed DiscussionThread.

    Extracts fields from the thread dataclass and delegates to
    build_discussion_summary_card (keyword-only signature).
    """
    # Resolve participant IDs to display names
    participants_display = thread.participants
    if engine:
        registry = getattr(engine, 'registry', None) or getattr(engine, '_registry', None)
        if registry:
            resolved = []
            for pid in thread.participants:
                agent = registry.get_agent(pid) if hasattr(registry, 'get_agent') else None
                if agent:
                    display = f"{getattr(agent, 'emoji', '')} {agent.name}".strip()
                    resolved.append(display)
                else:
                    resolved.append(pid)
            participants_display = resolved

    return build_discussion_summary_card(
        thread_id=thread.thread_id,
        participants=participants_display,
        conclusion=thread.conclusion,
        total_rounds=thread.current_round,
        total_tokens=thread.total_tokens_used,
        status=thread.status.value if hasattr(thread.status, "value") else str(thread.status),
        channel_id=thread.channel_id,
    )


def build_discussion_card(
    *,
    thread_id: str,
    participants: list[str],
    messages: list[dict],
    current_round: int,
    max_rounds: int,
    trigger_reason: str = "",
    channel_id: str = "",
) -> dict:
    """Build a live discussion thread card showing agent-to-agent dialogue.

    Args:
        thread_id: Unique discussion thread identifier.
        participants: List of agent display names in the discussion.
        messages: List of dicts with keys: sender, content, round_num.
        current_round: Current round number.
        max_rounds: Maximum allowed rounds.
        trigger_reason: Why the discussion was triggered.
        channel_id: Channel ID for action buttons.
    """
    header_title = f"💬 Agent 讨论 (轮次 {current_round}/{max_rounds})"
    header_subtitle = f"👥 {' · '.join(participants)}"

    elements: list[dict] = []

    # Participants line
    elements.append({
        "tag": "markdown",
        "content": f"**参与者:** {' ↔ '.join(participants)}",
    })

    # Progress bar: visualize completed vs remaining rounds
    progress_columns = []
    for i in range(1, max_rounds + 1):
        color = "purple" if i <= current_round else "grey"
        progress_columns.append({
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "elements": [{
                "tag": "markdown",
                "content": f"{'●' if i <= current_round else '○'}",
                "text_align": "center",
            }],
            "background_style": color,
        })
    elements.append({
        "tag": "column_set",
        "columns": progress_columns,
        "flex_mode": "stretch",
        "horizontal_spacing": "small",
    })

    if trigger_reason:
        elements.append({
            "tag": "markdown",
            "content": f"**触发原因:** {trigger_reason}",
            "text_size": "notation",
        })

    elements.append({"tag": "hr"})

    # Show last N messages (limit to 5 to keep card compact)
    display_messages = messages[-5:] if len(messages) > 5 else messages
    if len(messages) > 5:
        elements.append({
            "tag": "markdown",
            "content": f"*... 已省略 {len(messages) - 5} 条早期消息*",
            "text_size": "notation",
        })

    for msg in display_messages:
        sender = msg.get("sender", "Agent")
        content = msg.get("content", "")[:200]
        round_num = msg.get("round_num", "?")
        elements.append({
            "tag": "markdown",
            "content": f"**{sender}** (R{round_num}):\n{content}",
        })

    # Action buttons: expand full / stop discussion
    elements.append({"tag": "hr"})
    elements.extend(
        build_responsive_layout(
            [
                _build_callback_button(
                    "📖 展开全部",
                    "slock_discussion_expand",
                    channel_id=channel_id,
                    extra_value={"thread_id": thread_id},
                ),
                _build_callback_button(
                    "⏹ 停止讨论",
                    "slock_discussion_stop",
                    channel_id=channel_id,
                    button_type="danger",
                    extra_value={"thread_id": thread_id},
                ),
            ]
        )
    )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "subtitle": {"tag": "plain_text", "content": header_subtitle},
            "template": "purple",
        },
        "body": {"elements": elements},
    }


def build_discussion_summary_card(
    *,
    thread_id: str,
    participants: list[str],
    conclusion: str,
    total_rounds: int,
    total_tokens: int = 0,
    status: str = "converged",
    channel_id: str = "",
) -> dict:
    """Build a discussion conclusion card after a discussion ends.

    Args:
        thread_id: Unique discussion thread identifier.
        participants: List of agent display names involved.
        conclusion: The final summarized conclusion.
        total_rounds: Total number of rounds completed.
        total_tokens: Total tokens consumed by the discussion.
        status: Final status (converged, timeout, budget_exhausted, manually_stopped).
        channel_id: Channel ID for action buttons.
    """
    status_labels = {
        "converged": "✅ 达成共识",
        "timeout": "⏰ 超时结束",
        "budget_exhausted": "💰 预算耗尽",
        "manually_stopped": "⏹ 手动终止",
    }
    status_display = status_labels.get(status, status)
    header_title = f"💬 讨论结论 — {status_display}"

    elements: list[dict] = []

    # Participants and stats
    elements.append({
        "tag": "markdown",
        "content": (
            f"**参与者:** {' ↔ '.join(participants)}\n"
            f"**总轮次:** {total_rounds}"
            + (f" · **Token:** {total_tokens:,}" if total_tokens else "")
        ),
    })

    elements.append({"tag": "hr"})

    # Conclusion content
    elements.append({
        "tag": "markdown",
        "content": f"**结论:**\n{conclusion[:500]}",
    })

    # Footer
    elements.append({
        "tag": "markdown",
        "content": f"`thread: {thread_id[:12]}...`",
        "text_size": "notation",
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": "green" if status == "converged" else "grey",
        },
        "body": {"elements": elements},
    }


# ------------------------------------------------------------------
# Council cards
# ------------------------------------------------------------------


def build_council_card(run: CouncilRun, *, channel_id: str = "") -> dict:
    """Build a staged Slock Council card."""
    status_label = _COUNCIL_STATUS_LABEL_ZH.get(run.status, run.status.value)
    header_template = "green" if run.status == CouncilStatus.COMPLETED else "red" if run.status == CouncilStatus.FAILED else "indigo"
    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": f"**议题:** {redact_sensitive(run.question)[:300]}",
        }
    ]

    if run.error:
        elements.append({"tag": "markdown", "content": f"**错误:** {redact_sensitive(run.error)[:500]}"})

    elements.append({"tag": "hr"})
    elements.append(_build_council_stage_block("1", "独立意见", _format_council_responses(run)))
    elements.append(_build_council_stage_block("2", "匿名互评", _format_council_reviews(run)))
    elements.append(_build_council_stage_block("3", "主席综合", _format_council_final(run)))

    elements.append({
        "tag": "markdown",
        "content": f"`council: {run.run_id[:12]}...` · {status_label}",
        "text_size": "notation",
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"🧭 Slock Council — {status_label}"},
            "template": header_template,
        },
        "body": {"elements": elements},
    }


def _build_council_stage_block(index: str, title: str, content: str) -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "bisect",
        "background_style": "grey",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "markdown", "content": f"**阶段 {index}**\n{title}"}],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 3,
                "vertical_align": "top",
                "elements": [{"tag": "markdown", "content": content or "*等待中*"}],
            },
        ],
    }


def _format_council_responses(run: CouncilRun) -> str:
    if not run.responses:
        return "*等待 Agent 独立作答*"
    lines: list[str] = []
    for response in run.responses[:6]:
        content = response.content or response.error or "(空)"
        lines.append(
            f"• **{response.label}** · {response.agent_name or response.agent_id[:8]}: "
            f"{redact_sensitive(content)[:240]}"
        )
    return "\n".join(lines)


def _format_council_reviews(run: CouncilRun) -> str:
    if run.aggregate_rankings:
        return "\n".join(
            f"• #{idx + 1} **{item.label}** · {item.agent_name or item.agent_id[:8]} "
            f"(avg {item.average_rank:.2f}, score {item.quality_score:.1f})"
            for idx, item in enumerate(run.aggregate_rankings[:6])
        )
    if run.reviews:
        return "\n".join(
            f"• {review.reviewer_name or review.reviewer_agent_id[:8]}: "
            f"{', '.join(review.parsed_ranking) or '未解析'}"
            for review in run.reviews[:6]
        )
    return "*等待匿名互评*"


def _format_council_final(run: CouncilRun) -> str:
    if run.final_response:
        return redact_sensitive(run.final_response)[:1200]
    return "*等待主席综合*"


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
    AgentStatus.DISCUSSING: "💬",
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
    AgentStatus.DISCUSSING: "purple",
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

_COUNCIL_STATUS_LABEL_ZH: dict[CouncilStatus, str] = {
    CouncilStatus.STARTING: "准备中",
    CouncilStatus.STAGE1_RUNNING: "独立作答中",
    CouncilStatus.STAGE1_DONE: "独立意见完成",
    CouncilStatus.STAGE2_RUNNING: "匿名互评中",
    CouncilStatus.STAGE2_DONE: "匿名互评完成",
    CouncilStatus.STAGE3_RUNNING: "主席综合中",
    CouncilStatus.COMPLETED: "已完成",
    CouncilStatus.FAILED: "失败",
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


def build_crash_recovery_card(
    recovered_tasks: list[SlockTask],
    *,
    channel_id: str = "",
) -> dict:
    """Build a notification card for tasks recovered after crash/restart.

    Shows the list of tasks that were downgraded from IN_PROGRESS/IN_REVIEW
    back to TODO during channel activation.
    """
    elements: list[dict] = []

    elements.append({
        "tag": "markdown",
        "content": "系统重启后发现以下任务处于中间状态，已自动降级为 **待办**：",
    })

    # Task list
    task_lines = []
    for task in recovered_tasks[:20]:  # Cap display at 20
        task_id_short = task.task_id[:8]
        content_preview = task.content[:60] + ("..." if len(task.content) > 60 else "")
        task_lines.append(f"• `{task_id_short}` {content_preview}")

    if len(recovered_tasks) > 20:
        task_lines.append(f"• ... 还有 {len(recovered_tasks) - 20} 个任务")

    elements.append({
        "tag": "markdown",
        "content": "\n".join(task_lines),
    })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "markdown",
        "content": "💡 这些任务已重置为待办状态，可通过 `/task` 命令重新分配或触发执行。",
        "text_size": "notation",
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "⚡ 系统恢复通知"},
            "template": "orange",
        },
        "body": {"elements": elements},
    }


# ---------------------------------------------------------------------------
# Task 14-18: UX Card Templates
# ---------------------------------------------------------------------------


def build_command_panel_card(*, channel_id: str = "") -> dict:
    """Build a command panel showing available slock management commands."""
    elements: list[dict] = []

    command_groups = [
        (
            "Team 团队管理",
            [
                ("`/team`", "查看当前团队状态"),
                ("`/new-team`", "创建新的 Agent 团队"),
            ],
        ),
        (
            "Role 角色管理",
            [
                ("`/role`", "查看角色列表"),
                ("`/new-role`", "创建自定义角色"),
            ],
        ),
        (
            "Task 任务管理",
            [
                ("`/task`", "查看任务面板与分配"),
            ],
        ),
        (
            "Council 评审",
            [
                ("`/council`", "发起 Council 多角色评审"),
            ],
        ),
    ]

    for group_title, commands in command_groups:
        lines = [f"**{group_title}**"]
        for cmd, desc in commands:
            lines.append(f"  {cmd} — {desc}")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "\U0001f4cb Slock 命令面板"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def build_error_suggestion_card(
    user_input: str,
    suggestions: list[str],
    *,
    channel_id: str = "",
) -> dict:
    """Build an error card with correction suggestions when intent routing fails."""
    elements: list[dict] = []

    # Truncate and redact user input for display
    display_input = redact_sensitive(user_input)
    if len(display_input) > 50:
        display_input = display_input[:50] + "..."

    elements.append({
        "tag": "markdown",
        "content": f"无法识别您的输入：`{display_input}`",
    })

    # Show up to 5 suggestions
    if suggestions:
        suggestion_lines = ["**您是否想要：**"]
        for suggestion in suggestions[:5]:
            suggestion_lines.append(f"• {suggestion}")
        elements.append({
            "tag": "markdown",
            "content": "\n".join(suggestion_lines),
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "markdown",
        "content": "💡 也可以输入 /help 查看所有可用命令",
        "text_size": "notation",
    })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "❓ 无法识别指令"},
            "template": "red",
        },
        "body": {"elements": elements},
    }


def build_confirm_cancel_card(
    title: str,
    description: str,
    *,
    confirm_action: str = "slock_confirm",
    cancel_action: str = "slock_cancel",
    channel_id: str = "",
    extra_value: dict | None = None,
) -> dict:
    """Build a reusable confirmation dialog card."""
    elements: list[dict] = []

    elements.append({"tag": "markdown", "content": description})

    # Confirm + Cancel buttons
    elements.extend(
        build_responsive_layout(
            [
                _build_callback_button(
                    "确认",
                    confirm_action,
                    channel_id=channel_id,
                    button_type="primary",
                    extra_value=extra_value,
                ),
                _build_callback_button(
                    "取消",
                    cancel_action,
                    channel_id=channel_id,
                    button_type="default",
                    extra_value=extra_value,
                ),
            ]
        )
    )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "orange",
        },
        "body": {"elements": elements},
    }


def build_council_detail_card(
    topic: str,
    opinions: list[dict],
    *,
    final_summary: str = "",
    channel_id: str = "",
) -> dict:
    """Build an expanded council review detail card.

    Args:
        topic: The council review topic.
        opinions: List of dicts with keys: agent_name, emoji, role, opinion_text.
        final_summary: Optional synthesis summary.
    """
    elements: list[dict] = []

    # Topic
    elements.append({"tag": "markdown", "content": f"**议题：** {topic}"})
    elements.append({"tag": "hr"})

    # Each opinion as a collapsible section
    for idx, opinion in enumerate(opinions):
        agent_name = opinion.get("agent_name", "Agent")
        emoji = opinion.get("emoji", "\U0001f916")
        role = opinion.get("role", "")
        opinion_text = opinion.get("opinion_text", "")

        content = f"**{emoji} {agent_name}** ({role})\n{opinion_text}"
        elements.append({"tag": "markdown", "content": content})

        # Add hr separator between opinions (not after the last one)
        if idx < len(opinions) - 1:
            elements.append({"tag": "hr"})

    # Final summary section
    if final_summary:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"**\U0001f4dd 综合评估**\n{final_summary}",
        })

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "\U0001f3db\ufe0f Council 评审详情"},
            "template": "purple",
        },
        "body": {"elements": elements},
    }


def build_status_refresh_card(
    agents: list[dict],
    tasks_summary: dict,
    *,
    channel_id: str = "",
) -> dict:
    """Build a team status card with refresh button.

    Args:
        agents: List of dicts with keys: name, emoji, status, role.
        tasks_summary: Dict with keys: total, todo, in_progress, done.
    """
    elements: list[dict] = []

    # Agent list
    if agents:
        agent_lines = ["**Agent 状态**"]
        for agent in agents:
            emoji = agent.get("emoji", "\U0001f916")
            name = agent.get("name", "Agent")
            status = agent.get("status", "idle")
            role = agent.get("role", "")
            status_label = _STATUS_LABEL_ZH.get(status, status)
            line = f"  {emoji} **{name}** — `{status_label}`"
            if role:
                line += f" ({role})"
            agent_lines.append(line)
        elements.append({"tag": "markdown", "content": "\n".join(agent_lines)})

    elements.append({"tag": "hr"})

    # Task summary
    total = tasks_summary.get("total", 0)
    todo = tasks_summary.get("todo", 0)
    in_progress = tasks_summary.get("in_progress", 0)
    done = tasks_summary.get("done", 0)

    summary_content = (
        f"**任务概览**\n"
        f"  总计: **{total}** | 待办: **{todo}** | "
        f"进行中: **{in_progress}** | 已完成: **{done}**"
    )
    elements.append({"tag": "markdown", "content": summary_content})

    # Refresh button
    elements.extend(
        build_responsive_layout(
            [
                _build_callback_button(
                    "\U0001f504 刷新",
                    "slock_refresh_status",
                    channel_id=channel_id,
                ),
            ]
        )
    )

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "\U0001f4ca 团队状态"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }