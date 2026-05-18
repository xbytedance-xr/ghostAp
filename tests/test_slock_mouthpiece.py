"""Unit tests for slock_engine/mouthpiece.py — message formatting."""

from __future__ import annotations

import pytest

from src.slock_engine.models import AgentIdentity
from src.slock_engine.mouthpiece import Mouthpiece


class TestMouthpiece:
    def _make_agent(self, **kwargs) -> AgentIdentity:
        defaults = {"agent_id": "a1", "name": "Coder", "emoji": "🔧", "role": "coder", "agent_type": "coco"}
        defaults.update(kwargs)
        return AgentIdentity(**defaults)

    def test_format_text(self):
        mp = Mouthpiece()
        agent = self._make_agent(name="Alice", emoji="🤖")
        result = mp.format_text(agent, "Hello team!")
        assert result == "[🤖 Alice] Hello team!"

    def test_format_thinking(self):
        mp = Mouthpiece()
        agent = self._make_agent(name="Bob", emoji="🔧")
        result = mp.format_thinking(agent)
        assert result == "[🔧 Bob] 💭 thinking..."

    def test_format_card_returns_valid_structure(self):
        mp = Mouthpiece()
        agent = self._make_agent()
        card = mp.format_card(agent, "Some content", model_info="gpt-4")
        assert card["schema"] == "2.0"
        assert card["header"]["title"]["content"] == "🔧 Coder"
        assert card["header"]["template"] == "blue"
        body = card["body"]["elements"]
        assert any(e["tag"] == "markdown" and "Some content" in e["content"] for e in body)

    def test_format_card_with_duration(self):
        mp = Mouthpiece()
        agent = self._make_agent()
        card = mp.format_card(agent, "Done", duration_s=3.5)
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert len(note_elements) == 1
        assert "3.5s" in note_elements[0]["elements"][0]["content"]

    def test_format_escalation(self):
        mp = Mouthpiece()
        agent = self._make_agent(name="Helper", emoji="⚠️")
        card = mp.format_escalation(agent, "Need human review")
        assert card["schema"] == "2.0"
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        assert any("Escalation Request" in e["content"] for e in md_elements)
        assert any("Need human review" in e["content"] for e in md_elements)
