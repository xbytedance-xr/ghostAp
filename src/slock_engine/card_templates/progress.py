"""Collaboration progress card templates for Slock Engine.

Provides progress visualization cards for collaboration plans:
- Overview dashboard showing all active plans
- Detailed single-plan view with step timeline
"""

from __future__ import annotations

from ..models import (
    AgentIdentity,
    CollaborationPlan,
    CollaborationPlanStatus,
    PlanStep,
    PlanStepStatus,
)
from .common import (
    build_callback_button,
    build_card_wrapper,
    build_collapsible_panel,
    build_mobile_card_row,
    build_responsive_layout,
    redact_sensitive,
    truncate_dynamic_label,
)

# ---------------------------------------------------------------------------
# Plan status → header color mapping
# ---------------------------------------------------------------------------

_PLAN_STATUS_HEADER_COLOR: dict[CollaborationPlanStatus, str] = {
    CollaborationPlanStatus.PLANNING: "blue",
    CollaborationPlanStatus.PENDING_APPROVAL: "orange",
    CollaborationPlanStatus.EXECUTING: "blue",
    CollaborationPlanStatus.PAUSED: "blue",
    CollaborationPlanStatus.COMPLETED: "green",
    CollaborationPlanStatus.CANCELLED: "grey",
    CollaborationPlanStatus.FAILED: "red",
}

_PLAN_STATUS_LABEL_ZH: dict[CollaborationPlanStatus, str] = {
    CollaborationPlanStatus.PLANNING: "规划中",
    CollaborationPlanStatus.PENDING_APPROVAL: "待审批",
    CollaborationPlanStatus.EXECUTING: "执行中",
    CollaborationPlanStatus.PAUSED: "已暂停",
    CollaborationPlanStatus.COMPLETED: "已完成",
    CollaborationPlanStatus.CANCELLED: "已取消",
    CollaborationPlanStatus.FAILED: "失败",
}

# Plan status icons for compact overview rows
_PLAN_STATUS_ICONS: dict[CollaborationPlanStatus, str] = {
    CollaborationPlanStatus.PLANNING: "🔵",
    CollaborationPlanStatus.PENDING_APPROVAL: "🟠",
    CollaborationPlanStatus.EXECUTING: "🟣",
    CollaborationPlanStatus.PAUSED: "⏸",
    CollaborationPlanStatus.COMPLETED: "✅",
    CollaborationPlanStatus.CANCELLED: "⚫",
    CollaborationPlanStatus.FAILED: "🔴",
}

# Step-level status icons
_STEP_STATUS_ICONS: dict[PlanStepStatus, str] = {
    PlanStepStatus.TODO: "⬜",
    PlanStepStatus.IN_PROGRESS: "🔵",
    PlanStepStatus.DONE: "✅",
    PlanStepStatus.SKIPPED: "⏭",
    PlanStepStatus.TIMED_OUT: "⏰",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_native_progress(pct: int) -> dict:
    """Build a Feishu native progress element.

    Args:
        pct: Progress percentage (0-100).

    Returns:
        A dict representing a Feishu progress component.
    """
    pct = max(0, min(100, pct))
    color = "green" if pct == 100 else ("blue" if pct >= 50 else "grey")
    return {
        "tag": "progress",
        "percent": pct,
        "status": color,
        "show_text": True,
    }


def _resolve_agent_display(
    agent_id: str,
    agent_map: dict[str, AgentIdentity],
) -> str:
    """Resolve agent_id to a display string (emoji + name)."""
    if not agent_id:
        return ""
    agent = agent_map.get(agent_id)
    if agent:
        return f"{agent.emoji}{agent.name}"
    return agent_id[:8]


# ---------------------------------------------------------------------------
# Card 1: Progress Overview (multi-plan dashboard)
# ---------------------------------------------------------------------------


def build_progress_overview_card(
    plans: list[CollaborationPlan],
    agents: list[AgentIdentity],
    *,
    team_name: str = "",
    channel_id: str = "",
    highlight_plan_id: str = "",
) -> dict:
    """Build an overview progress dashboard for all active plans.

    Shows a compact row per plan with status icon, task_id, progress bar,
    and current step role. Max 5 plans displayed; overflow noted.

    Args:
        plans: All collaboration plans to display.
        agents: All agents for resolving agent_id to display info.
        team_name: Optional team name for header context.
        channel_id: Channel for action button routing.
        highlight_plan_id: If set, visually emphasizes this plan row.
    """
    {a.agent_id: a for a in agents}

    elements: list[dict] = []

    # Summary line
    total = len(plans)
    executing = sum(1 for p in plans if p.status == CollaborationPlanStatus.EXECUTING)
    pending = sum(1 for p in plans if p.status == CollaborationPlanStatus.PENDING_APPROVAL)
    summary_parts = [f"共 **{total}** 个计划"]
    if executing:
        summary_parts.append(f"🟣 执行中: **{executing}**")
    if pending:
        summary_parts.append(f"🟠 待审批: **{pending}**")
    elements.append({"tag": "markdown", "content": " | ".join(summary_parts)})

    elements.append({"tag": "hr"})

    # Plan rows (max 5)
    display_plans = plans[:5]
    overflow = total - len(display_plans)

    for plan in display_plans:
        status_icon = _PLAN_STATUS_ICONS.get(plan.status, "⚪")
        _task_content = getattr(plan, 'task_content', '') or ''
        task_label = _task_content[:50] if _task_content else plan.task_id[:12]

        # Current step role
        current_step = plan.current_step
        step_info = ""
        if current_step:
            step_info = f" → `{current_step.role}`"

        # Mobile-friendly row: title (status + task + step) | content (progress) | action (button)
        title_els = [{"tag": "markdown", "content": f"{status_icon} `{task_label}`{step_info}"}]
        content_els = [_build_native_progress(plan.progress_pct)]
        action_els = [build_callback_button(
            "详情",
            "slock_show_plan_detail",
            channel_id=channel_id,
            button_type="default",
            extra_value={"plan_id": plan.plan_id},
        )]
        bg = "card_primary" if plan.plan_id == highlight_plan_id else "default"
        elements.append(build_mobile_card_row(
            title_elements=title_els,
            content_elements=content_els,
            action_elements=action_els,
            background_style=bg,
        ))

    if overflow > 0:
        elements.append({
            "tag": "markdown",
            "content": f"*还有 {overflow} 个计划…*",
            "text_size": "notation",
        })

    # Action buttons
    elements.append({"tag": "hr"})
    action_buttons = [
        build_callback_button(
            "🔄 刷新",
            "slock_progress_refresh",
            channel_id=channel_id,
            button_type="default",
        ),
    ]
    elements.extend(build_responsive_layout(action_buttons))

    return build_card_wrapper(
        header_title="📊 协作进度总览",
        header_template="indigo",
        elements=elements,
    )


# ---------------------------------------------------------------------------
# Card 2: Single Collaboration Plan Detail
# ---------------------------------------------------------------------------


def build_collaboration_plan_card(
    plan: CollaborationPlan,
    agents: list[AgentIdentity],
    *,
    channel_id: str = "",
    show_actions: bool = True,
) -> dict:
    """Build a detailed view card for a single collaboration plan.

    Shows step timeline, progress bar, status badge, and contextual
    action buttons based on plan status.

    Args:
        plan: The collaboration plan to render.
        agents: All agents for resolving agent_id to display info.
        channel_id: Channel for action button routing.
        show_actions: Whether to show action buttons.
    """
    agent_map: dict[str, AgentIdentity] = {a.agent_id: a for a in agents}

    # Header
    template_label = plan.chain_template or "自定义"
    header_title = f"📋 协作计划 — {template_label}"
    header_color = _PLAN_STATUS_HEADER_COLOR.get(plan.status, "indigo")

    elements: list[dict] = []

    # Status badge
    status_label = _PLAN_STATUS_LABEL_ZH.get(plan.status, plan.status.value)
    status_icon = _PLAN_STATUS_ICONS.get(plan.status, "⚪")
    elements.append({
        "tag": "markdown",
        "content": (
            f"状态: {status_icon} **{status_label}** | 任务: "
            f"`{(getattr(plan, 'task_content', '') or '')[:50] or plan.task_id[:8]}`"
            f" ({plan.task_id[:8]})"
        ),
    })

    # Progress (native component)
    elements.append(_build_native_progress(plan.progress_pct))

    elements.append({"tag": "hr"})

    # Steps timeline
    current_step = plan.current_step
    sorted_steps = sorted(plan.steps, key=lambda s: s.order)

    step_elements: list[dict] = []
    for step in sorted_steps:
        step_line = _format_step_row(step, agent_map, is_current=(step is current_step))
        step_elements.append({"tag": "markdown", "content": step_line})

    # Use collapsible panel if >5 steps
    if len(sorted_steps) > 5:
        elements.append(
            build_collapsible_panel(
                f"**📝 执行步骤** ({len(sorted_steps)} 步)",
                step_elements,
                expanded=False,
            )
        )
    else:
        elements.append({
            "tag": "markdown",
            "content": f"**📝 执行步骤** ({len(sorted_steps)} 步)",
        })
        elements.extend(step_elements)

    # Action buttons
    if show_actions:
        buttons: list[dict] = []

        if plan.status == CollaborationPlanStatus.PENDING_APPROVAL:
            buttons.append(
                build_callback_button(
                    "✅ 批准执行",
                    "slock_plan_approve",
                    channel_id=channel_id,
                    button_type="primary",
                    extra_value={"plan_id": plan.plan_id},
                )
            )
            buttons.append(
                build_callback_button(
                    "❌ 取消计划",
                    "slock_plan_cancel",
                    channel_id=channel_id,
                    button_type="danger",
                    extra_value={"plan_id": plan.plan_id},
                )
            )
        elif plan.status == CollaborationPlanStatus.EXECUTING:
            buttons.append(
                build_callback_button(
                    "⏸ 暂停",
                    "slock_pause_plan",
                    channel_id=channel_id,
                    button_type="default",
                    extra_value={"plan_id": plan.plan_id},
                )
            )
            buttons.append(
                build_callback_button(
                    "🖐 人工介入",
                    "slock_user_intervention",
                    channel_id=channel_id,
                    button_type="default",
                    extra_value={"plan_id": plan.plan_id},
                )
            )
        elif plan.status == CollaborationPlanStatus.PAUSED:
            buttons.append(
                build_callback_button(
                    "▶️ 恢复",
                    "slock_resume_plan",
                    channel_id=channel_id,
                    button_type="primary",
                    extra_value={"plan_id": plan.plan_id},
                )
            )

        if buttons:
            elements.append({"tag": "hr"})
            elements.extend(build_responsive_layout(buttons))

    # -- Discussion / Supplement Info Panel --
    if show_actions and plan.status in (
        CollaborationPlanStatus.EXECUTING,
        CollaborationPlanStatus.PAUSED,
    ):
        elements.append(
            build_collapsible_panel(
                "**💬 讨论 / 补充信息**",
                [
                    {
                        "tag": "form",
                        "name": f"plan_supplement_{plan.plan_id}",
                        "elements": [
                            {
                                "tag": "input",
                                "name": "supplement_content",
                                "placeholder": {
                                    "tag": "plain_text",
                                    "content": "输入补充信息或讨论内容...",
                                },
                                "max_length": 500,
                            },
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "📨 发送"},
                                "type": "primary",
                                "action_type": "form_action",
                                "name": "supplement_submit",
                                "value": {
                                    "action": "slock_plan_supplement",
                                    "plan_id": plan.plan_id,
                                    "channel_id": channel_id,
                                },
                            },
                        ],
                    },
                ],
                expanded=False,
            )
        )

    return build_card_wrapper(
        header_title=header_title,
        header_template=header_color,
        elements=elements,
    )


def _format_step_row(
    step: PlanStep,
    agent_map: dict[str, AgentIdentity],
    *,
    is_current: bool = False,
) -> str:
    """Format a single plan step as a timeline row.

    Args:
        step: The PlanStep to format.
        agent_map: Agent lookup dictionary.
        is_current: Whether this is the currently active step.
    """
    status_icon = _STEP_STATUS_ICONS.get(step.status, "⬜")
    role_text = step.role or "未分配"

    # Agent display
    agent_display = _resolve_agent_display(step.agent_id, agent_map)
    agent_part = f"　{agent_display}" if agent_display else ""

    # Description (truncated to 50 chars, sanitized)
    safe_desc = redact_sensitive(step.description)
    desc = safe_desc[:50]
    if len(safe_desc) > 50:
        desc += "…"

    # Dependencies note
    deps_note = ""
    if step.depends_on:
        deps_note = f"　⤷ 依赖: {len(step.depends_on)} 步"

    # Build the row — bold if current
    if is_current:
        row = f"{status_icon} **`{role_text}`**{agent_part}　{desc}"
    else:
        row = f"{status_icon} `{role_text}`{agent_part}　{desc}"

    if deps_note:
        row += f"\n　　{deps_note}"

    # Show reason for abnormal completion
    if step.status in (PlanStepStatus.TIMED_OUT, PlanStepStatus.SKIPPED):
        reason = getattr(step, 'resolved_reason', '') or getattr(step, 'reason', '')
        if reason:
            row += f"\n　　⚠ {reason}"

    return row


# ---------------------------------------------------------------------------
# Card 3: Task Overview (single-plan summary with agent statuses)
# ---------------------------------------------------------------------------


def build_task_overview_card(
    plan: "CollaborationPlan",
    agents: list[tuple["AgentIdentity", str]],  # (agent, status_str)
    *,
    channel_id: str = "",
    latest_output_summary: str = "",
    discussion_entries: list[dict] | None = None,  # Each: {speaker, content, timestamp}
    timeline_events: list[dict] | None = None,  # Each: {event_type, agent_id, timestamp, detail}
) -> dict:
    """Build a task overview card for a single collaboration plan.

    Combines progress visualization, agent status roster, latest output,
    discussion history, timeline events, and a supplement input form.

    Args:
        plan: The collaboration plan to render.
        agents: List of (AgentIdentity, status_str) tuples for each agent.
        channel_id: Channel for action button routing.
        latest_output_summary: Optional latest output text to display.
        discussion_entries: Optional list of discussion dicts with keys:
            speaker, content, timestamp.
        timeline_events: Optional list of timeline event dicts with keys:
            event_type, agent_id, timestamp, detail.

    Returns:
        A Feishu Interactive Card 2.0 dict.
    """
    task_content = getattr(plan, "task_content", "") or ""
    subtitle = task_content[:30]
    if len(task_content) > 30:
        subtitle += "…"

    elements: list[dict] = []

    # --- Subtitle ---
    if subtitle:
        elements.append({"tag": "markdown", "content": f"**{subtitle}**"})

    # --- Progress bar ---
    elements.append(_build_native_progress(plan.progress_pct))

    # --- Current status: status label + chain_template ---
    status_label = _PLAN_STATUS_LABEL_ZH.get(plan.status, plan.status.value)
    status_icon = _PLAN_STATUS_ICONS.get(plan.status, "⚪")
    chain_template = plan.chain_template or "自定义"
    elements.append({
        "tag": "markdown",
        "content": f"状态: {status_icon} **{status_label}** | 链路: `{chain_template}`",
    })

    elements.append({"tag": "hr"})

    # --- Agent status section ---
    if agents:
        elements.append({"tag": "markdown", "content": "**👥 角色状态**"})
        for agent, status_str in agents:
            agent_display = f"{agent.emoji}{agent.name}" if agent.emoji else agent.name
            badge_text = truncate_dynamic_label(status_str, max_len=16)
            title_els = [
                {"tag": "markdown", "content": f"{agent_display}　`{badge_text}`"},
            ]
            elements.append(build_mobile_card_row(
                title_elements=title_els,
                background_style="default",
            ))

    # --- Latest output summary (collapsible) ---
    if latest_output_summary:
        elements.append(
            build_collapsible_panel(
                "**📄 最新产出**",
                [{"tag": "markdown", "content": latest_output_summary}],
                expanded=False,
            )
        )

    # --- Discussion entries (collapsible, latest 5 rounds) ---
    if discussion_entries:
        # Limit to latest 5 entries to prevent payload bloat
        visible_entries = discussion_entries[-5:]
        discussion_els: list[dict] = []
        for entry in visible_entries:
            speaker = entry.get("speaker", "")
            content = entry.get("content", "")
            content_truncated = content[:80]
            if len(content) > 80:
                content_truncated += "…"
            discussion_els.append({
                "tag": "markdown",
                "content": f"**{speaker}**: {content_truncated}",
            })
        if len(discussion_entries) > 5:
            discussion_els.insert(0, {
                "tag": "markdown",
                "content": f"<font color='grey'>...还有 {len(discussion_entries) - 5} 条更早的讨论</font>",
            })
        elements.append(
            build_collapsible_panel(
                f"**💬 角色讨论** ({len(discussion_entries)})",
                discussion_els,
                expanded=False,
            )
        )

    # --- Task timeline events (collapsible, latest 10) ---
    if timeline_events:
        from datetime import datetime, timezone

        visible_events = sorted(timeline_events, key=lambda e: e.get("timestamp", 0), reverse=True)[:10]
        timeline_els: list[dict] = []
        for evt in visible_events:
            evt_type = evt.get("event_type", "")
            detail = evt.get("detail", "")
            ts = evt.get("timestamp", 0)
            time_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S") if ts else ""
            evt_icons = {"claimed": "🟢", "started": "🔵", "completed": "✅", "rejected": "❌",
                         "force_completed": "⚠️", "blocked": "🚫"}
            icon = evt_icons.get(evt_type, "⚪")
            line = f"{icon} `{time_str}` {detail[:60]}" if detail else f"{icon} `{time_str}` {evt_type}"
            timeline_els.append({"tag": "markdown", "content": line})
        elements.append(
            build_collapsible_panel(
                f"**📜 任务时间线** ({len(timeline_events)})",
                timeline_els,
                expanded=False,
            )
        )

    elements.append({"tag": "hr"})

    # --- Action section: supplement form ---
    elements.append({
        "tag": "form",
        "name": f"task_overview_supplement_{plan.plan_id}",
        "elements": [
            {
                "tag": "input",
                "name": "supplement_content",
                "placeholder": {
                    "tag": "plain_text",
                    "content": "输入补充信息...",
                },
                "max_length": 500,
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📝 补充信息"},
                "type": "primary",
                "action_type": "form_action",
                "name": "supplement_submit",
                "value": {
                    "action": "slock_plan_supplement",
                    "plan_id": plan.plan_id,
                    "channel_id": channel_id,
                },
            },
        ],
    })

    return build_card_wrapper(
        header_title="📊 任务总览",
        header_template="indigo",
        elements=elements,
    )
