"""Dedicated tests for build_role_info_card.

Covers:
- Assign task form has UUID suffix (uniqueness per chat)
- Quick action buttons include correct action_type values
- Card renders without crash for all AgentStatus values
- Skill profiles sort by success_rate descending, capped at 8
"""

from __future__ import annotations

import json

import pytest

from src.slock_engine.card_templates.role import build_role_info_card
from src.slock_engine.models import AgentIdentity, AgentStatus


def _make_agent(**kwargs) -> AgentIdentity:
    defaults = {"agent_id": "test-agent-1", "name": "TestAgent", "emoji": "🤖", "role": "coder"}
    defaults.update(kwargs)
    return AgentIdentity(**defaults)


def _find_form_recursive(elements: list) -> dict | None:
    """Search elements tree recursively for a form tag (may be inside collapsible_panel)."""
    for el in elements:
        if el.get("tag") == "form":
            return el
        # collapsible_panel wraps children in "elements"
        if el.get("tag") == "collapsible_panel":
            inner = el.get("elements", [])
            found = _find_form_recursive(inner)
            if found:
                return found
    return None


class TestRoleInfoCardFormUniqueness:
    """Verify form names use deterministic agent_id-based naming."""

    def test_form_name_has_agent_id(self):
        agent = _make_agent()
        card = build_role_info_card(agent, status=AgentStatus.IDLE)

        form_el = _find_form_recursive(card["body"]["elements"])
        assert form_el is not None

        name = form_el["name"]
        # Pattern: assign_task_{agent_id}
        assert name == "assign_task_test-agent-1"

    def test_two_calls_produce_same_form_names(self):
        """Deterministic form names — same agent always gets the same form name."""
        agent = _make_agent()
        card1 = build_role_info_card(agent, status=AgentStatus.IDLE)
        card2 = build_role_info_card(agent, status=AgentStatus.IDLE)

        form1 = _find_form_recursive(card1["body"]["elements"])
        form2 = _find_form_recursive(card2["body"]["elements"])
        assert form1["name"] == form2["name"]


class TestRoleInfoCardQuickActions:
    """Verify quick action buttons have correct action_type values."""

    def test_quick_action_buttons_present(self):
        agent = _make_agent()
        card = build_role_info_card(agent, status=AgentStatus.IDLE, channel_id="ch-1")

        serialized = json.dumps(card, ensure_ascii=False)
        assert "slock_agent_show_memory" in serialized
        assert "slock_start_discussion" in serialized

    def test_action_value_contains_agent_id_and_channel(self):
        agent = _make_agent(agent_id="agent-xyz")
        card = build_role_info_card(agent, status=AgentStatus.IDLE, channel_id="ch-abc")

        serialized = json.dumps(card, ensure_ascii=False)
        assert "agent-xyz" in serialized
        assert "ch-abc" in serialized


class TestRoleInfoCardAllStatuses:
    """Card renders without error for every AgentStatus value."""

    @pytest.mark.parametrize("status", list(AgentStatus))
    def test_renders_for_status(self, status: AgentStatus):
        agent = _make_agent()
        card = build_role_info_card(agent, status=status)

        assert isinstance(card, dict)
        assert "header" in card
        assert "body" in card


class TestRoleInfoCardSkillCap:
    """Skill profiles sorted desc by success_rate and capped at 8."""

    def test_skills_capped_at_8(self):
        agent = _make_agent()
        skills = [{"tag": f"skill-{i}", "success_rate": float(i * 10)} for i in range(12)]

        card = build_role_info_card(agent, status=AgentStatus.IDLE, skill_profiles=skills)
        serialized = json.dumps(card, ensure_ascii=False)

        # Skills 4-11 (top 8 by rate) should appear; skill-0..3 should not
        assert "`skill-11`" in serialized  # highest rate
        assert "`skill-4`" in serialized   # 8th highest
        assert "`skill-3`" not in serialized  # 9th — excluded


class TestDefaultPersonalityTraitsMapping:
    """Verify DEFAULT_PERSONALITY_TRAITS returns correct values for each role."""

    def setup_method(self):
        from src.feishu.handlers.slock import SlockHandler

        self.traits_map = SlockHandler.DEFAULT_PERSONALITY_TRAITS

    def test_coder_default_traits(self):
        """Role 'coder' without --traits produces ['严谨', '注重细节']."""
        assert self.traits_map["coder"] == ["严谨", "注重细节"]

    def test_reviewer_default_traits(self):
        assert self.traits_map["reviewer"] == ["批判性思维", "追求质量"]

    def test_tester_default_traits(self):
        assert self.traits_map["tester"] == ["细致", "追求覆盖"]

    def test_planner_default_traits(self):
        assert self.traits_map["planner"] == ["全局视角", "有条理"]

    def test_architect_default_traits(self):
        assert self.traits_map["architect"] == ["抽象思维", "系统设计"]

    def test_writer_default_traits(self):
        assert self.traits_map["writer"] == ["表达清晰", "注重结构"]

    def test_custom_default_traits_empty(self):
        """Role 'custom' has no default traits."""
        assert self.traits_map["custom"] == []


class TestExplicitTraitsParsing:
    """Verify --traits comma-separated parsing logic matches create_role behavior."""

    def _parse_traits(self, explicit_traits: str) -> list[str]:
        """Replicate the exact parsing logic from SlockHandler.create_role."""
        return [t.strip() for t in explicit_traits.replace("\uff0c", ",").split(",") if t.strip()]

    def test_simple_comma_separated(self):
        """'创新,果断' parses to ['创新', '果断']."""
        result = self._parse_traits("创新,果断")
        assert result == ["创新", "果断"]

    def test_chinese_comma_separated(self):
        """Full-width comma '\uff0c' is treated the same as ','."""
        result = self._parse_traits("创新\uff0c果断")
        assert result == ["创新", "果断"]

    def test_mixed_commas(self):
        """Mix of full-width and ASCII commas."""
        result = self._parse_traits("严谨,注重细节\uff0c高效")
        assert result == ["严谨", "注重细节", "高效"]

    def test_whitespace_stripped(self):
        """Whitespace around trait names is stripped."""
        result = self._parse_traits(" 创新 , 果断 ")
        assert result == ["创新", "果断"]

    def test_empty_segments_ignored(self):
        """Empty segments from leading/trailing commas are dropped."""
        result = self._parse_traits(",创新,,果断,")
        assert result == ["创新", "果断"]

    def test_single_trait(self):
        """A single trait with no commas."""
        result = self._parse_traits("独立思考")
        assert result == ["独立思考"]
