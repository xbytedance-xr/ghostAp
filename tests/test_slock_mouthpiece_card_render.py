"""AC9: Mouthpiece Card rendering verification tests.

Validates: Agent output formatted as Interactive Card with:
- Colored header matching agent_type/role
- emoji + role name as title
- schema 2.0
- column_set layout (no legacy actions)
- No legacy "actions" field at top level
"""

from __future__ import annotations

import pytest

from src.card.render.payload_truncator import count_markdown_table_blocks
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
    def test_card_schema_and_structure(self, mouthpiece, role):
        """Every role produces a valid card with schema 2.0, colored header, and body.elements."""
        agent = _make_agent(role, name="MyAgent", emoji="🎯")
        card = mouthpiece.format_card(agent, "Test content")
        assert card["schema"] == "2.0"
        expected_color = AGENT_ROLE_COLORS[role]
        assert card["header"]["template"] == expected_color
        title = card["header"]["title"]["content"]
        assert "🎯" in title
        assert "MyAgent" in title
        assert "actions" not in card
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

    def test_card_footer_with_model_and_duration(self, mouthpiece):
        """Footer contains model info and duration when provided."""
        agent = _make_agent("coder")
        card = mouthpiece.format_card(agent, "Content", model_info="claude-sonnet-4", duration_s=5.2)
        elements = card["body"]["elements"]
        footer_elements = [e for e in elements if e.get("tag") == "markdown" and e.get("text_size") == "notation"]
        assert len(footer_elements) >= 1
        assert "claude-sonnet-4" in footer_elements[0]["content"]
        assert "5.2s" in footer_elements[0]["content"]


class TestMouthpieceColorCoverage:
    """Verify all agent_type roles have distinct colors."""

    def test_all_roles_have_colors_and_unknown_fallback(self):
        """Every defined role in AGENT_ROLE_COLORS maps to a non-empty string; unknown gets grey."""
        expected_roles = {"coder", "writer", "reviewer", "tester", "planner", "architect", "custom"}
        for role in expected_roles:
            assert role in AGENT_ROLE_COLORS, f"Missing color for role: {role}"
            assert AGENT_ROLE_COLORS[role], f"Empty color for role: {role}"
        assert AGENT_ROLE_COLORS["custom"] == "grey"
        agent = AgentIdentity(name="Unknown", role="totally_unknown_role")
        assert agent.card_color == "grey"


class TestMouthpieceFormatting:
    """Unit tests for slock_engine/mouthpiece.py — message formatting."""

    def _make_agent(self, **kwargs) -> AgentIdentity:
        defaults = {"agent_id": "a1", "name": "Coder", "emoji": "🔧", "role": "coder", "agent_type": "coco"}
        defaults.update(kwargs)
        return AgentIdentity(**defaults)

    def test_format_text(self):
        mp = Mouthpiece()
        agent = self._make_agent(name="Alice", emoji="🤖")
        result = mp.format_text(agent, "Hello team!")
        assert result == "[🤖 Alice] Hello team!"

    def test_format_card_returns_valid_structure(self):
        mp = Mouthpiece()
        agent = self._make_agent()
        card = mp.format_card(agent, "Some content", model_info="gpt-4")
        assert card["schema"] == "2.0"
        assert card["header"]["title"]["content"] == "🔧 Coder"
        assert card["header"]["template"] == "blue"
        body = card["body"]["elements"]
        assert any(e["tag"] == "markdown" and "Some content" in e["content"] for e in body)

    def test_format_card_neutralizes_markdown_tables_over_feishu_limit(self):
        """Agent output cards must not get stuck on Feishu table-count limits."""
        mp = Mouthpiece()
        agent = self._make_agent()
        table = "| A | B |\n|---|---|\n| 1 | 2 |"

        card = mp.format_card(agent, "\n\n".join([table] * 6), model_info="gpt-4")

        body = card["body"]["elements"]
        main_markdown = next(e for e in body if e.get("tag") == "markdown" and "A | B" in e.get("content", ""))
        assert count_markdown_table_blocks(main_markdown["content"]) == 0
        assert main_markdown["content"].count("```text") == 6
        assert any("表格数量超过飞书卡片限制" in e.get("content", "") for e in body)

    def test_format_escalation(self):
        mp = Mouthpiece()
        agent = self._make_agent(name="Helper", emoji="⚠️")
        card = mp.format_escalation(agent, "Need human review")
        assert card["schema"] == "2.0"
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        assert any("Escalation Request" in e["content"] for e in md_elements)
        assert any("Need human review" in e["content"] for e in md_elements)
