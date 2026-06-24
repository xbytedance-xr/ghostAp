"""Role card templates for Slock Engine.

Provides:
- build_role_info_card: Complete agent profile (/role info <name>)
- build_role_list_card: Compact agent table (/role list)
"""

from __future__ import annotations

from typing import Optional

from ..models import (
    AgentIdentity,
    AgentStatus,
    SlockMemory,
    SlockTask,
)
from .common import (
    STATUS_ICON_MAP,
    STATUS_LABEL_ZH,
    TASK_CONTENT_DETAIL_LEN,
    TASK_CONTENT_PREVIEW_LEN,
    build_callback_button,
    build_card_wrapper,
    build_collapsible_panel,
    build_mobile_card_row,
    build_responsive_layout,
    truncate_dynamic_label,
)


def build_role_info_card(
    agent: AgentIdentity,
    *,
    status: AgentStatus = AgentStatus.IDLE,
    memory: Optional[SlockMemory] = None,
    skill_profiles: Optional[list[dict]] = None,
    current_task: Optional[SlockTask] = None,
    recent_tasks: Optional[list[SlockTask]] = None,
    channel_id: str = "",
) -> dict:
    """Build a complete agent profile card for /role info <name>.

    Layout:
        Header (emoji + name + role type color)
        → Identity Section (emoji, name, role, type, model)
        → Personality (system_prompt summary ≤100 chars)
        → Skills (tag + success rate list)
        → Memory (L1 key knowledge top 3 items)
        → Tasks (current + recent 3 history)
        → Quick Actions (assign task, view memory, start discussion)

    Args:
        agent: Agent identity to display.
        status: Current agent status.
        memory: Agent's SlockMemory (L1).
        skill_profiles: List of skill profile dicts.
        current_task: Currently active task (if any).
        recent_tasks: Recent completed tasks (max 3 shown).
        channel_id: Channel for action routing.
    """
    skill_profiles = skill_profiles or []
    recent_tasks = recent_tasks or []

    header_title = f"{agent.emoji} {agent.name}"
    header_template = agent.card_color or "indigo"

    elements: list[dict] = []

    # -- Section 1: Identity --
    status_icon = STATUS_ICON_MAP.get(status, "⚪")
    status_value = status.value if hasattr(status, "value") else str(status)
    status_label = STATUS_LABEL_ZH.get(status_value, status_value)

    identity_lines = [
        f"**角色类型**　`{agent.role}`　{status_icon} {status_label}",
        f"**引擎**　`{agent.agent_type}`" + (f"　|　**模型**　`{agent.model_name}`" if agent.model_name else ""),
    ]
    elements.append({"tag": "markdown", "content": "\n".join(identity_lines)})

    elements.append({"tag": "hr"})

    # -- Section 2: Quick Actions --
    action_value = {
        "agent_id": agent.agent_id,
        "agent_name": agent.name,
        "channel_id": channel_id,
    }
    action_buttons = [
        build_callback_button(
            "🧠 查看记忆",
            "slock_agent_show_memory",
            channel_id=channel_id,
            button_type="default",
            extra_value=action_value,
        ),
        build_callback_button(
            "💬 发起讨论",
            "slock_start_discussion",
            channel_id=channel_id,
            button_type="default",
            extra_value=action_value,
        ),
    ]
    elements.extend(build_responsive_layout(action_buttons))

    # -- Section 3: Personality (system_prompt summary + traits) --
    personality_parts: list[str] = []
    if agent.personality_traits:
        traits_text = "　".join(f"`{t}`" for t in agent.personality_traits[:6])
        personality_parts.append(f"**🎭 性格标签**　{traits_text}")
    if agent.system_prompt:
        prompt_summary = agent.system_prompt[:100]
        if len(agent.system_prompt) > 100:
            prompt_summary += "…"
        personality_parts.append(f"**📝 设定**\n{prompt_summary}")
    if personality_parts:
        elements.append({"tag": "markdown", "content": "\n".join(personality_parts)})

    # -- Section 4: Skills --
    if skill_profiles:
        skill_lines: list[str] = []
        sorted_skills = sorted(skill_profiles, key=lambda s: s.get("success_rate", 0), reverse=True)
        for skill in sorted_skills[:8]:
            tag = skill.get("tag", "未知")
            rate = int(skill.get("success_rate", 0))
            total = skill.get("total_tasks", 0)
            skill_lines.append(f"• `{tag}` — 成功率 {rate}% ({total}次)")
        elements.append({
            "tag": "markdown",
            "content": "**🏷️ 技能档案**\n" + "\n".join(skill_lines),
        })

    # -- Section 5: Memory (L1 key knowledge) --
    if memory and memory.key_knowledge:
        knowledge_lines = memory.key_knowledge.strip().split("\n")
        # Take first 3 non-empty lines
        top_items = [line.strip() for line in knowledge_lines if line.strip()][:3]
        if top_items:
            memory_content = "\n".join(f"• {item}" for item in top_items)
            remaining = len([line for line in knowledge_lines if line.strip()]) - len(top_items)
            if remaining > 0:
                memory_content += f"\n*...还有 {remaining} 条*"
            elements.append(
                build_collapsible_panel(
                    "**🧠 记忆摘要**",
                    [{"tag": "markdown", "content": memory_content}],
                    expanded=False,
                )
            )

    # -- Section 6: Tasks --
    task_elements: list[dict] = []

    if current_task:
        task_content = current_task.content[:TASK_CONTENT_DETAIL_LEN]
        if len(current_task.content) > TASK_CONTENT_DETAIL_LEN:
            task_content += "…"
        task_elements.append({
            "tag": "markdown",
            "content": f"🔵 **进行中**　{task_content}",
        })

    if recent_tasks:
        history_lines: list[str] = []
        for task in recent_tasks[:3]:
            content_preview = task.content[:TASK_CONTENT_PREVIEW_LEN]
            if len(task.content) > TASK_CONTENT_PREVIEW_LEN:
                content_preview += "…"
            status_marker = "✅" if task.status.value == "done" else "⬜"
            history_lines.append(f"{status_marker} {content_preview}")
        task_elements.append({
            "tag": "markdown",
            "content": "**历史任务**\n" + "\n".join(history_lines),
        })

    if task_elements:
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": "**📋 任务**"})
        elements.extend(task_elements)

    # -- Section 7: Assign Task Form --
    # NOTE: Feishu does NOT support `form` inside `collapsible_panel` (error 200621).
    # Place form directly in top-level elements.
    assign_form = {
        "tag": "form",
        "name": f"assign_task_{agent.agent_id}",
        "elements": [
            {
                "tag": "input",
                "name": "task_content",
                "placeholder": {"tag": "plain_text", "content": "输入任务描述..."},
                "max_length": 200,
            },
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "📝 分配任务"},
                "type": "primary",
                "action_type": "form_action",
                "name": "assign_submit",
                "value": {
                    "action": "slock_assign_task_to_agent",
                    "agent_id": agent.agent_id,
                    "agent_name": agent.name,
                    "channel_id": channel_id,
                },
            },
        ],
    }
    elements.append({"tag": "hr"})
    elements.append(assign_form)

    return build_card_wrapper(
        header_title=header_title,
        header_template=header_template,
        elements=elements,
    )


def build_role_list_card(
    agents: list[tuple[AgentIdentity, AgentStatus]],
    *,
    team_name: str = "",
    channel_id: str = "",
    current_tasks: Optional[dict[str, SlockTask]] = None,
    skill_profiles: Optional[dict[str, list[dict]]] = None,
) -> dict:
    """Build a compact agent list card for /role list.

    Each agent row: emoji | name | status badge | current task (20 chars) | skill tags.
    Supports clicking to view full profile via callback button.

    Args:
        agents: List of (AgentIdentity, AgentStatus) tuples.
        team_name: Optional team name.
        channel_id: Channel for action routing.
        current_tasks: Optional agent_id → active task mapping.
        skill_profiles: Optional agent_id → skill profiles mapping.
    """
    header_title = f"👥 {team_name} 角色列表" if team_name else "👥 角色列表"
    current_tasks = current_tasks or {}
    skill_profiles = skill_profiles or {}

    elements: list[dict] = []

    if not agents:
        elements.append({"tag": "markdown", "content": "*当前团队暂无角色。使用 `/new-role` 创建角色。*"})
    else:
        elements.append({
            "tag": "markdown",
            "content": f"共 **{len(agents)}** 个角色",
            "text_size": "notation",
        })
        elements.append({"tag": "hr"})

        for idx, (agent, status) in enumerate(agents):
            status_icon = STATUS_ICON_MAP.get(status, "⚪")
            current_task = current_tasks.get(agent.agent_id)

            # Task preview (truncated 20 chars)
            task_text = ""
            if current_task:
                content = current_task.content
                task_text = content[:20] + ("…" if len(content) > 20 else "")

            # Skill tags (top 2 for compactness)
            agent_skills = skill_profiles.get(agent.agent_id, [])
            skill_text = ""
            if agent_skills:
                top = sorted(agent_skills, key=lambda s: s.get("success_rate", 0), reverse=True)[:2]
                skill_text = " ".join(f"`{s.get('tag', '?')}`" for s in top)

            # Personality traits (compact)
            traits_text = ""
            if agent.personality_traits:
                traits_text = " ".join(f"`{t}`" for t in agent.personality_traits[:2])

            # Mobile-friendly row: title (name+status) | content (task+skills+traits)
            title_els = [{"tag": "markdown", "content": f"{agent.emoji} **{agent.name}**　{status_icon}"}]
            content_parts: list[str] = []
            if task_text:
                content_parts.append(f"📋 {task_text}")
            if skill_text:
                content_parts.append(skill_text)
            if traits_text:
                content_parts.append(traits_text)
            content_els = [{"tag": "markdown", "content": " ".join(content_parts) or "—"}] if content_parts else None

            # Alternating background for visual separation
            row_bg = "default" if idx % 2 == 0 else "grey"

            elements.append(build_mobile_card_row(
                title_elements=title_els,
                content_elements=content_els,
                background_style=row_bg,
            ))

        # View detail buttons (collapsible)
        if len(agents) > 0:
            elements.append({"tag": "hr"})
            detail_buttons = []
            for agent, _ in agents:
                label = truncate_dynamic_label(f"📄 {agent.name}", max_len=16)
                detail_buttons.append(
                    build_callback_button(
                        label,
                        "slock_role_info",
                        channel_id=channel_id,
                        button_type="default",
                        extra_value={"agent_id": agent.agent_id, "agent_name": agent.name},
                    )
                )
            elements.append(
                build_collapsible_panel(
                    "**🔍 查看详情**",
                    build_responsive_layout(detail_buttons),
                    expanded=False,
                )
            )

    return build_card_wrapper(
        header_title=header_title,
        header_template="indigo",
        elements=elements,
    )
