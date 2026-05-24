"""Dedicated tests for build_role_list_card.

Covers:
- Alternating background rows for visual separation
- Skill tags show top 2 for each agent
- Current task preview truncated to 20 chars
- Detail buttons in collapsible panel
- Personality traits compact rendering (top 2)
"""

from __future__ import annotations

import json

import pytest

from src.slock_engine.card_templates.role import build_role_list_card
from src.slock_engine.models import AgentIdentity, AgentStatus, SlockTask, TaskStatus


def _make_agent(**kwargs) -> AgentIdentity:
    defaults = {"agent_id": "a-1", "name": "Agent1", "emoji": "🤖", "role": "coder"}
    defaults.update(kwargs)
    return AgentIdentity(**defaults)


def _make_task(content: str = "Test task", **kwargs) -> SlockTask:
    defaults = {"task_id": "t-1", "content": content, "status": TaskStatus.IN_PROGRESS}
    defaults.update(kwargs)
    return SlockTask(**defaults)


class TestRoleListCardAlternatingBg:
    """Verify alternating background for visual separation."""

    def test_alternating_bg_styles(self):
        agents = [
            (_make_agent(agent_id=f"a-{i}", name=f"Agent{i}"), AgentStatus.IDLE)
            for i in range(4)
        ]
        card = build_role_list_card(agents=agents)
        serialized = json.dumps(card, ensure_ascii=False)

        # Even rows use "default", odd rows use "grey"
        assert '"background_style": "default"' in serialized
        assert '"background_style": "grey"' in serialized


class TestRoleListCardSkillTags:
    """Skill tags show top 2 per agent in compact form."""

    def test_top2_skills_shown(self):
        agent = _make_agent(agent_id="skill-agent")
        skills = {
            "skill-agent": [
                {"tag": "python", "success_rate": 95.0},
                {"tag": "golang", "success_rate": 88.0},
                {"tag": "rust", "success_rate": 70.0},
            ]
        }
        card = build_role_list_card(
            agents=[(agent, AgentStatus.IDLE)],
            skill_profiles=skills,
        )
        serialized = json.dumps(card, ensure_ascii=False)

        assert "`python`" in serialized
        assert "`golang`" in serialized
        # 3rd skill (rust) should not appear in list view
        assert "`rust`" not in serialized


class TestRoleListCardTaskPreview:
    """Current task truncated to 20 chars with ellipsis."""

    def test_task_preview_truncation(self):
        agent = _make_agent(agent_id="tp-agent")
        long_content = "这是一个非常长的任务内容描述，超过二十个字符"
        task = _make_task(content=long_content, task_id="t-long")

        card = build_role_list_card(
            agents=[(agent, AgentStatus.RUNNING)],
            current_tasks={"tp-agent": task},
        )
        serialized = json.dumps(card, ensure_ascii=False)

        # First 20 chars should appear
        assert long_content[:20] in serialized
        # Ellipsis should be present
        assert "…" in serialized
        # Full content should NOT appear
        assert long_content not in serialized

    def test_short_task_no_ellipsis(self):
        agent = _make_agent(agent_id="short-agent")
        task = _make_task(content="简短任务", task_id="t-short")

        card = build_role_list_card(
            agents=[(agent, AgentStatus.RUNNING)],
            current_tasks={"short-agent": task},
        )
        serialized = json.dumps(card, ensure_ascii=False)

        assert "简短任务" in serialized


class TestRoleListCardDetailPanel:
    """Detail buttons in collapsible panel."""

    def test_detail_buttons_in_collapsible_panel(self):
        agents = [
            (_make_agent(agent_id="d1", name="Alpha"), AgentStatus.IDLE),
            (_make_agent(agent_id="d2", name="Beta"), AgentStatus.RUNNING),
        ]
        card = build_role_list_card(agents=agents, channel_id="ch-detail")
        elements = card["body"]["elements"]

        # Find collapsible panel
        panel = next(
            (e for e in elements if e.get("tag") == "collapsible_panel"),
            None,
        )
        assert panel is not None
        assert panel["expanded"] is False
        assert "查看详情" in panel["header"]["title"]["content"]

    def test_detail_buttons_have_correct_action_type(self):
        agents = [
            (_make_agent(agent_id="btn-1", name="BtnAgent"), AgentStatus.IDLE),
        ]
        card = build_role_list_card(agents=agents, channel_id="ch-btn")
        serialized = json.dumps(card, ensure_ascii=False)

        assert "slock_role_info" in serialized
        assert "btn-1" in serialized


class TestRoleListCardPersonalityTraits:
    """Personality traits show top 2 in compact form."""

    def test_personality_traits_top2(self):
        agent = AgentIdentity(
            agent_id="trait-agent",
            name="TraitBot",
            emoji="🎭",
            role="coder",
            personality_traits=["严谨", "高效", "友善"],
        )
        card = build_role_list_card(agents=[(agent, AgentStatus.IDLE)])
        serialized = json.dumps(card, ensure_ascii=False)

        assert "`严谨`" in serialized
        assert "`高效`" in serialized
        # 3rd trait should not appear in list view
        assert "`友善`" not in serialized


class TestRoleListCardEmpty:
    """Empty agent list produces helpful message."""

    def test_empty_list_shows_create_hint(self):
        card = build_role_list_card(agents=[])
        serialized = json.dumps(card, ensure_ascii=False)

        assert "/new-role" in serialized
        assert "暂无角色" in serialized
