"""AC9: Mouthpiece Card rendering verification tests.

Validates: Agent output formatted as Interactive Card with:
- Colored header matching agent_type/role
- emoji + role name as title
- schema 2.0
- column_set layout (no legacy actions)
- No legacy "actions" field at top level
"""

from __future__ import annotations

import json

import pytest

from src.slock_engine.models import AGENT_ROLE_COLORS, AgentIdentity
from src.slock_engine.mouthpiece import Mouthpiece


def _make_agent(role: str, name: str = "", emoji: str = "🤖") -> AgentIdentity:
    return AgentIdentity(
        name=name or f"Agent-{role}",
        emoji=emoji,
        agent_type="coco",
        model_name="test-model",
        role=role,
        owner_group="test_chat",
    )


class TestMouthpieceCardSchemaCompliance:
    """All Mouthpiece cards must be CardKit v2 Schema 2.0 compliant."""

    @pytest.fixture
    def mouthpiece(self):
        return Mouthpiece()

    @pytest.mark.parametrize("role", list(AGENT_ROLE_COLORS.keys()))
    def test_card_has_schema_2_0(self, mouthpiece, role):
        """Every role produces a card with schema 2.0."""
        agent = _make_agent(role)
        card = mouthpiece.format_card(agent, "Test content")
        assert card["schema"] == "2.0"

    @pytest.mark.parametrize("role", list(AGENT_ROLE_COLORS.keys()))
    def test_card_has_colored_header(self, mouthpiece, role):
        """Every role produces a card with a colored header template."""
        agent = _make_agent(role)
        card = mouthpiece.format_card(agent, "Test content")
        expected_color = AGENT_ROLE_COLORS[role]
        assert card["header"]["template"] == expected_color

    @pytest.mark.parametrize("role", list(AGENT_ROLE_COLORS.keys()))
    def test_card_header_contains_emoji_and_name(self, mouthpiece, role):
        """Header title contains agent emoji and name."""
        agent = _make_agent(role, name="MyAgent", emoji="🎯")
        card = mouthpiece.format_card(agent, "Content")
        title = card["header"]["title"]["content"]
        assert "🎯" in title
        assert "MyAgent" in title

    @pytest.mark.parametrize("role", list(AGENT_ROLE_COLORS.keys()))
    def test_card_no_legacy_actions_field(self, mouthpiece, role):
        """Card must NOT have top-level 'actions' field (legacy format)."""
        agent = _make_agent(role)
        card = mouthpiece.format_card(agent, "Content")
        assert "actions" not in card, "Legacy 'actions' field found at top level"

    @pytest.mark.parametrize("role", list(AGENT_ROLE_COLORS.keys()))
    def test_card_uses_body_elements(self, mouthpiece, role):
        """Card uses body.elements structure (not legacy elements at top level)."""
        agent = _make_agent(role)
        card = mouthpiece.format_card(agent, "Content")
        assert "body" in card
        assert "elements" in card["body"]

    def test_card_content_appears_in_markdown(self, mouthpiece):
        """The message content appears in a markdown element."""
        agent = _make_agent("coder")
        card = mouthpiece.format_card(agent, "Hello world response")
        elements = card["body"]["elements"]
        markdown_elements = [e for e in elements if e.get("tag") == "markdown"]
        all_content = " ".join(e.get("content", "") for e in markdown_elements)
        assert "Hello world response" in all_content

    def test_card_footer_with_model_info(self, mouthpiece):
        """Footer contains model info when provided."""
        agent = _make_agent("coder")
        card = mouthpiece.format_card(agent, "Content", model_info="claude-sonnet-4")
        elements = card["body"]["elements"]
        footer_elements = [e for e in elements if e.get("tag") == "markdown" and e.get("text_size") == "notation"]
        assert len(footer_elements) >= 1
        assert "claude-sonnet-4" in footer_elements[0]["content"]

    def test_card_footer_with_duration(self, mouthpiece):
        """Footer contains duration when provided."""
        agent = _make_agent("coder")
        card = mouthpiece.format_card(agent, "Content", duration_s=5.2)
        elements = card["body"]["elements"]
        footer_elements = [e for e in elements if e.get("tag") == "markdown" and e.get("text_size") == "notation"]
        assert len(footer_elements) >= 1
        assert "5.2s" in footer_elements[0]["content"]

    def test_card_footer_with_duration_minutes(self, mouthpiece):
        """Footer shows minutes format for long durations."""
        agent = _make_agent("coder")
        card = mouthpiece.format_card(agent, "Content", duration_s=125.0)
        elements = card["body"]["elements"]
        footer_elements = [e for e in elements if e.get("tag") == "markdown" and e.get("text_size") == "notation"]
        assert "2m" in footer_elements[0]["content"]

    def test_card_is_valid_json_serializable(self, mouthpiece):
        """Card dict can be serialized to JSON without errors."""
        agent = _make_agent("architect", emoji="📐")
        card = mouthpiece.format_card(agent, "Architecture review complete")
        json_str = json.dumps(card, ensure_ascii=False)
        assert isinstance(json_str, str)
        parsed = json.loads(json_str)
        assert parsed["schema"] == "2.0"


class TestMouthpieceColorCoverage:
    """Verify all agent_type roles have distinct colors."""

    def test_all_roles_have_colors(self):
        """Every defined role in AGENT_ROLE_COLORS maps to a non-empty string."""
        expected_roles = {"coder", "writer", "reviewer", "tester", "planner", "architect", "custom"}
        for role in expected_roles:
            assert role in AGENT_ROLE_COLORS, f"Missing color for role: {role}"
            assert AGENT_ROLE_COLORS[role], f"Empty color for role: {role}"

    def test_custom_role_has_fallback_color(self):
        """The 'custom' role has a fallback color (grey)."""
        assert AGENT_ROLE_COLORS["custom"] == "grey"

    def test_unknown_role_gets_grey_via_card_color_property(self):
        """AgentIdentity.card_color returns 'grey' for unknown roles."""
        agent = AgentIdentity(name="Unknown", role="totally_unknown_role")
        assert agent.card_color == "grey"


class TestMouthpieceTextFormat:
    """Verify plain-text mouthpiece format."""

    def test_format_text_includes_emoji_and_name(self):
        mouthpiece = Mouthpiece()
        agent = _make_agent("coder", name="Dev-1", emoji="🔧")
        result = mouthpiece.format_text(agent, "Hello")
        assert result == "[🔧 Dev-1] Hello"

    def test_format_thinking(self):
        mouthpiece = Mouthpiece()
        agent = _make_agent("reviewer", name="Rev-1", emoji="🔍")
        result = mouthpiece.format_thinking(agent)
        assert "🔍" in result
        assert "Rev-1" in result
        assert "thinking" in result
