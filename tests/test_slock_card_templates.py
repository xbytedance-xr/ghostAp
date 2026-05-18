"""Unit tests for slock_engine/card_templates.py — card building functions."""

from __future__ import annotations

import pytest

from src.slock_engine.card_templates import (
    build_agent_message_card,
    build_status_panel_card,
    build_task_board_card,
)
from src.slock_engine.models import (
    AgentIdentity,
    AgentStatus,
    SlockTask,
    TaskStatus,
)


class TestBuildAgentMessageCard:
    def _make_agent(self, **kwargs) -> AgentIdentity:
        defaults = {"agent_id": "a1", "name": "Coder", "emoji": "🔧", "role": "coder"}
        defaults.update(kwargs)
        return AgentIdentity(**defaults)

    def test_basic_structure(self):
        agent = self._make_agent()
        card = build_agent_message_card(agent, "Hello world")
        assert card["schema"] == "2.0"
        assert card["header"]["title"]["content"] == "🔧 Coder"
        assert card["header"]["template"] == "blue"
        body_elements = card["body"]["elements"]
        assert any(e["tag"] == "markdown" and "Hello world" in e["content"] for e in body_elements)

    def test_footer_with_model_info(self):
        agent = self._make_agent(agent_type="claude", model_name="claude-3")
        card = build_agent_message_card(agent, "msg", model_info="claude-3-opus")
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert len(note_elements) == 1
        note_text = note_elements[0]["elements"][0]["content"]
        assert "claude-3-opus" in note_text

    def test_footer_with_duration_seconds(self):
        agent = self._make_agent()
        card = build_agent_message_card(agent, "msg", duration_s=5.2)
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert len(note_elements) == 1
        assert "5.2s" in note_elements[0]["elements"][0]["content"]

    def test_footer_with_duration_minutes(self):
        agent = self._make_agent()
        card = build_agent_message_card(agent, "msg", duration_s=125.3)
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert "2m" in note_elements[0]["elements"][0]["content"]

    def test_no_footer_when_no_metadata(self):
        agent = self._make_agent(agent_type="", model_name="")
        card = build_agent_message_card(agent, "msg")
        note_elements = [e for e in card["body"]["elements"] if e["tag"] == "note"]
        assert len(note_elements) == 0


class TestBuildStatusPanelCard:
    def test_empty_agents(self):
        card = build_status_panel_card([], team_name="Alpha")
        assert "Alpha" in card["header"]["title"]["content"]
        body = card["body"]["elements"]
        assert any("No agents" in e.get("content", "") for e in body)

    def test_with_agents(self):
        agent = AgentIdentity(agent_id="a1", name="Alice", emoji="🤖", role="coder")
        card = build_status_panel_card([(agent, AgentStatus.IDLE)], team_name="Team")
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        assert any("Alice" in e["content"] for e in md_elements)
        assert any("Idle" in e["content"] for e in md_elements)

    def test_refresh_button_present(self):
        card = build_status_panel_card([])
        body = card["body"]["elements"]
        action_elements = [e for e in body if e["tag"] == "action"]
        assert len(action_elements) == 1
        assert action_elements[0]["actions"][0]["text"]["content"] == "🔄 Refresh"

    def test_default_title_without_team(self):
        card = build_status_panel_card([])
        assert "Slock Agent Status" in card["header"]["title"]["content"]


class TestBuildTaskBoardCard:
    def test_empty_tasks(self):
        card = build_task_board_card([], [], team_name="Dev")
        assert "Dev" in card["header"]["title"]["content"]
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        assert any("empty" in e["content"] for e in md_elements)

    def test_with_tasks(self):
        task = SlockTask(task_id="t1", content="Build feature X", status=TaskStatus.TODO)
        agent = AgentIdentity(agent_id="a1", name="Bob", emoji="🔧")
        card = build_task_board_card([task], [agent])
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        assert any("Build feature X" in e["content"] for e in md_elements)

    def test_task_with_assignee(self):
        task = SlockTask(
            task_id="t1", content="Fix bug", status=TaskStatus.IN_PROGRESS, claimed_by="a1"
        )
        agent = AgentIdentity(agent_id="a1", name="Bob", emoji="🔧")
        card = build_task_board_card([task], [agent])
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        # Should show assignee
        assert any("Bob" in e["content"] for e in md_elements)

    def test_board_groups_by_status(self):
        tasks = [
            SlockTask(task_id="t1", content="A", status=TaskStatus.TODO),
            SlockTask(task_id="t2", content="B", status=TaskStatus.DONE),
        ]
        card = build_task_board_card(tasks, [])
        body = card["body"]["elements"]
        md_elements = [e for e in body if e["tag"] == "markdown"]
        # Should have columns for each status
        assert any("Todo" in e["content"] for e in md_elements)
        assert any("Done" in e["content"] for e in md_elements)
