"""Status panel card templates for Slock Engine.

Provides the redesigned /slock status card with clear visual hierarchy:
Header → Summary Table → Collapsible Agent Details → Action Buttons.
"""

from __future__ import annotations

from typing import Optional

from .common import (
    STATUS_BG_STYLE_MAP,
    STATUS_ICON_MAP,
    STATUS_LABEL_ZH,
    TASK_CONTENT_COMPACT_LEN,
    AgentIdentity,
    AgentStatus,
    SlockTask,
    build_callback_button,
    build_card_wrapper,
    build_collapsible_panel,
    build_column,
    build_column_set_row,
    build_mobile_card_row,
    build_responsive_layout,
    redact_sensitive,
    truncate_dynamic_label,
)


def _build_agent_detail_elements(
    agents: list[tuple[AgentIdentity, AgentStatus]],
    current_tasks: dict[str, SlockTask],
    skill_profiles: dict[str, list[dict]],
) -> list[dict]:
    """Build the detail markdown elements for the collapsible panel."""
    detail_elements: list[dict] = []
    for agent, status in agents:
        status_icon = STATUS_ICON_MAP.get(status, "⚪")
        agent_skills = skill_profiles.get(agent.agent_id, [])
        current_task = current_tasks.get(agent.agent_id)

        info_parts = [
            f"**{agent.emoji} {agent.name}**",
            f"角色: `{agent.role}` | 引擎: `{agent.agent_type}`",
            f"状态: {status_icon} {STATUS_LABEL_ZH.get(status.value, status.value)}",
        ]
        if agent.model_name:
            info_parts.append(f"模型: `{agent.model_name}`")
        if current_task:
            info_parts.append(f"当前任务: {redact_sensitive(current_task.content)[:50]}")
        if agent_skills:
            skills_text = ", ".join(
                f"{s.get('tag', '?')}({int(s.get('success_rate', 0))}%)"
                for s in agent_skills[:5]
            )
            info_parts.append(f"技能: {skills_text}")

        detail_elements.append({
            "tag": "markdown",
            "content": "\n".join(info_parts),
        })
        detail_elements.append({"tag": "hr"})

    # Remove trailing hr
    if detail_elements and detail_elements[-1].get("tag") == "hr":
        detail_elements.pop()
    return detail_elements


def build_status_panel_card(
    agents: list[tuple[AgentIdentity, AgentStatus]],
    team_name: str = "",
    channel_id: str = "",
    current_tasks: Optional[dict[str, SlockTask]] = None,
    skill_profiles: Optional[dict[str, list[dict]]] = None,
    tasks_summary: dict | None = None,
) -> dict:
    """Build a redesigned status panel card with clear visual hierarchy.

    Layout:
        Header (team name + agent count)
        → Summary Table (emoji|name|status|task|skills per agent)
        → Collapsible Details (expanded agent info)
        → Action Buttons (refresh, stop all, per-agent stop)

    Args:
        agents: List of (AgentIdentity, AgentStatus) tuples.
        team_name: Optional team name for the header.
        channel_id: Optional channel identifier.
        current_tasks: Optional agent_id → active SlockTask mapping.
        skill_profiles: Optional agent_id → list of skill profile dicts.
        tasks_summary: Optional dict with keys: total, todo, in_progress, in_review, done.
    """
    header_title = f"📊 {team_name}" if team_name else "📊 Slock 团队状态"
    agent_count = len(agents)
    current_tasks = current_tasks or {}
    skill_profiles = skill_profiles or {}

    elements: list[dict] = []

    # -- Section 1: Team Summary --
    if not agents:
        elements.append({"tag": "markdown", "content": "*当前团队暂无已注册的 Agent。*"})
    else:
        # Overview line
        active_count = sum(1 for _, s in agents if s != AgentStatus.IDLE)
        summary_line = f"**{agent_count}** 个角色"
        if active_count > 0:
            summary_line += f"　|　🔵 **{active_count}** 活跃中"
        else:
            summary_line += "　|　🟢 全部空闲"
        elements.append({"tag": "markdown", "content": summary_line})

        elements.append({"tag": "hr"})

        # -- Task Summary (optional) --
        if tasks_summary is not None and tasks_summary.get("total", 0) > 0:
            task_line = (
                f"📋 任务: 共{tasks_summary['total']}个"
                f" | ⬜ 待办{tasks_summary.get('todo', 0)}"
                f" | 🔵 进行中{tasks_summary.get('in_progress', 0)}"
                f"\n🟡 审查中{tasks_summary.get('in_review', 0)}"
                f" | ✅ 已完成{tasks_summary.get('done', 0)}"
            )
            elements.append({"tag": "markdown", "content": task_line})
            elements.append({"tag": "hr"})

        # -- Section 2: Agent Summary Table (mobile-friendly rows) --
        for agent, status in agents:
            status_icon = STATUS_ICON_MAP.get(status, "⚪")
            status_label = STATUS_LABEL_ZH.get(status.value, status.value)
            current_task = current_tasks.get(agent.agent_id)

            # Task preview (truncated to 20 chars)
            task_text = ""
            if current_task:
                content = redact_sensitive(current_task.content)
                task_text = content[:TASK_CONTENT_COMPACT_LEN] + ("…" if len(content) > TASK_CONTENT_COMPACT_LEN else "")

            # Skill tags (top 3)
            agent_skills = skill_profiles.get(agent.agent_id, [])
            skill_tags = ""
            if agent_skills:
                top_skills = sorted(agent_skills, key=lambda s: s.get("success_rate", 0), reverse=True)[:3]
                skill_tags = " ".join(f"`{s.get('tag', '?')}`" for s in top_skills)

            # Title row: emoji + name + status
            title_els = [{"tag": "markdown", "content": f"{agent.emoji} **{agent.name}**　{status_icon} {status_label}"}]

            # Content row: task + skills
            content_parts: list[str] = []
            if task_text:
                content_parts.append(f"📋 {task_text}")
            if skill_tags:
                content_parts.append(skill_tags)
            content_els = [{"tag": "markdown", "content": " ".join(content_parts)}] if content_parts else None

            elements.append(build_mobile_card_row(
                title_elements=title_els,
                content_elements=content_els,
                background_style=STATUS_BG_STYLE_MAP.get(status, "default"),
            ))

        # -- Section 3: Collapsible Details --
        detail_elements: list[dict] = _build_agent_detail_elements(
            agents, current_tasks, skill_profiles
        )
        if detail_elements:
            elements.append(
                build_collapsible_panel(
                    "**📋 详细信息**",
                    detail_elements,
                    expanded=False,
                )
            )

    # -- Section 4: Action Buttons --
    elements.append({"tag": "hr"})

    # Refresh + Stop All in same row
    refresh_btn = build_callback_button(
        "🔄 刷新状态",
        "slock_refresh_status",
        channel_id=channel_id,
        button_type="primary_text",
    )
    stop_all_btn = build_callback_button(
        "⏹ 全部停止",
        "slock_stop_all",
        channel_id=channel_id,
        button_type="danger",
    )
    # Add confirm dialog to stop button
    stop_all_btn["confirm"] = {
        "title": {"tag": "plain_text", "content": "确认停止"},
        "text": {"tag": "plain_text", "content": "将停止当前群组内所有活跃任务，确定继续？"},
    }

    elements.extend(build_responsive_layout([refresh_btn, stop_all_btn]))

    # Individual stop buttons for non-idle agents (in collapsible panel)
    non_idle = [(a, s) for a, s in agents if s != AgentStatus.IDLE]
    if non_idle:
        stop_buttons: list[dict] = []
        for agent, _ in non_idle:
            label = truncate_dynamic_label(f"⏹ 停止 {agent.name}", max_len=18)
            stop_buttons.append(
                build_callback_button(
                    label,
                    "slock_stop_agent",
                    channel_id=channel_id,
                    button_type="danger",
                    extra_value={"agent_id": agent.agent_id},
                )
            )
        elements.append(
            build_collapsible_panel(
                "**⚠️ 单独停止**",
                build_responsive_layout(stop_buttons),
                expanded=False,
            )
        )

    return build_card_wrapper(
        header_title=header_title,
        header_template="indigo",
        elements=elements,
    )
