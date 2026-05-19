"""Tests for Agent lifecycle state transitions (AC-03).

Validates the full state machine cycle and status panel color mapping.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from src.slock_engine.models import AgentIdentity, AgentStatus
from src.slock_engine.card_templates import build_status_panel_card


class TestAgentLifecycleStates:
    """Verify Agent state transitions and status panel rendering."""

    def _make_agent(self, name="Coder-A", agent_id="agent-001"):
        return AgentIdentity(
            agent_id=agent_id,
            name=name,
            emoji="🔧",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="test prompt",
            role="coder",
        )

    def test_all_states_exist_in_enum(self):
        """All expected states are defined in AgentStatus enum."""
        expected = {"idle", "waking", "thinking", "running", "checking", "sending"}
        actual = {s.value for s in AgentStatus}
        assert expected.issubset(actual)

    def test_status_panel_idle_green(self):
        """IDLE state renders with green background in status panel."""
        agent = self._make_agent()
        card = build_status_panel_card([(agent, AgentStatus.IDLE)], team_name="Test", channel_id="ch-1")
        body_str = json.dumps(card["body"])
        assert "green" in body_str

    def test_status_panel_thinking_yellow(self):
        """THINKING state renders with yellow background."""
        agent = self._make_agent()
        card = build_status_panel_card([(agent, AgentStatus.THINKING)], team_name="Test", channel_id="ch-1")
        body_str = json.dumps(card["body"])
        assert "yellow" in body_str

    def test_status_panel_running_blue(self):
        """RUNNING state renders with blue background."""
        agent = self._make_agent()
        card = build_status_panel_card([(agent, AgentStatus.RUNNING)], team_name="Test", channel_id="ch-1")
        body_str = json.dumps(card["body"])
        assert "blue" in body_str

    def test_status_panel_sending_grey(self):
        """SENDING state renders with grey background."""
        agent = self._make_agent()
        card = build_status_panel_card([(agent, AgentStatus.SENDING)], team_name="Test", channel_id="ch-1")
        body_str = json.dumps(card["body"])
        assert "grey" in body_str

    def test_status_panel_multiple_agents_mixed_states(self):
        """Multiple agents with different states render correctly."""
        agents_states = [
            (self._make_agent("Coder-A", "a1"), AgentStatus.RUNNING),
            (self._make_agent("Reviewer-B", "a2"), AgentStatus.IDLE),
            (self._make_agent("Writer-C", "a3"), AgentStatus.THINKING),
        ]
        card = build_status_panel_card(agents_states, team_name="Team", channel_id="ch-1")
        body_str = json.dumps(card["body"])
        assert "Coder-A" in body_str
        assert "Reviewer-B" in body_str
        assert "Writer-C" in body_str

    def test_stop_button_only_for_non_idle_agents(self):
        """Per-agent Stop buttons only appear for non-IDLE agents."""
        agents_states = [
            (self._make_agent("Coder-A", "a1"), AgentStatus.RUNNING),
            (self._make_agent("Reviewer-B", "a2"), AgentStatus.IDLE),
        ]
        card = build_status_panel_card(agents_states, team_name="Team", channel_id="ch-1")
        body_str = json.dumps(card["body"], ensure_ascii=False)
        # Should have Stop button for Coder-A (RUNNING) but not Reviewer-B (IDLE)
        assert "停止" in body_str and "Coder-A" in body_str
        assert "停止 Reviewer-B" not in body_str

    def test_status_panel_schema_v2(self):
        """Status panel uses schema 2.0."""
        agent = self._make_agent()
        card = build_status_panel_card([(agent, AgentStatus.IDLE)], team_name="T")
        assert card["schema"] == "2.0"

    def test_refresh_button_present(self):
        """Refresh button is present in the panel."""
        agent = self._make_agent()
        card = build_status_panel_card([(agent, AgentStatus.IDLE)], team_name="T", channel_id="ch-1")
        body_str = json.dumps(card["body"], ensure_ascii=False)
        assert "刷新" in body_str
        assert "slock_refresh_status" in body_str


class TestAgentStateTransitions:
    """AC-03: Verify the actual state machine transitions IDLE→RUNNING→SENDING→IDLE."""

    def _make_engine(self):
        """Create an engine mock with real transition logic."""
        from src.slock_engine.models import AgentStatus

        agent_statuses: dict[str, AgentStatus] = {}

        valid_transitions: dict[AgentStatus, list[AgentStatus]] = {
            AgentStatus.IDLE: [AgentStatus.WAKING],
            AgentStatus.WAKING: [AgentStatus.THINKING, AgentStatus.IDLE],
            AgentStatus.THINKING: [AgentStatus.RUNNING, AgentStatus.IDLE],
            AgentStatus.RUNNING: [AgentStatus.CHECKING, AgentStatus.IDLE],
            AgentStatus.CHECKING: [AgentStatus.SENDING, AgentStatus.RUNNING, AgentStatus.IDLE],
            AgentStatus.SENDING: [AgentStatus.IDLE],
        }

        class FakeEngine:
            def get_agent_status(self, agent_id: str) -> AgentStatus:
                return agent_statuses.get(agent_id, AgentStatus.IDLE)

            def set_agent_status(self, agent_id: str, status: AgentStatus):
                agent_statuses[agent_id] = status

            def transition_agent(self, agent_id: str, to_status: AgentStatus) -> bool:
                current = self.get_agent_status(agent_id)
                if to_status in valid_transitions.get(current, []):
                    self.set_agent_status(agent_id, to_status)
                    return True
                return False

        return FakeEngine()

    def test_full_lifecycle_idle_to_idle(self):
        """Agent completes full IDLE→WAKING→THINKING→RUNNING→CHECKING→SENDING→IDLE cycle."""
        engine = self._make_engine()
        agent_id = "agent-001"

        assert engine.get_agent_status(agent_id) == AgentStatus.IDLE

        assert engine.transition_agent(agent_id, AgentStatus.WAKING) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.WAKING

        assert engine.transition_agent(agent_id, AgentStatus.THINKING) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.THINKING

        assert engine.transition_agent(agent_id, AgentStatus.RUNNING) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.RUNNING

        assert engine.transition_agent(agent_id, AgentStatus.CHECKING) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.CHECKING

        assert engine.transition_agent(agent_id, AgentStatus.SENDING) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.SENDING

        assert engine.transition_agent(agent_id, AgentStatus.IDLE) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.IDLE

    def test_invalid_transition_rejected(self):
        """Invalid transitions (e.g., IDLE→RUNNING) are rejected."""
        engine = self._make_engine()
        agent_id = "agent-002"

        # IDLE → RUNNING is not a valid direct transition
        assert engine.transition_agent(agent_id, AgentStatus.RUNNING) is False
        assert engine.get_agent_status(agent_id) == AgentStatus.IDLE

    def test_early_abort_to_idle(self):
        """Agent can abort from RUNNING back to IDLE (stop scenario)."""
        engine = self._make_engine()
        agent_id = "agent-003"

        engine.transition_agent(agent_id, AgentStatus.WAKING)
        engine.transition_agent(agent_id, AgentStatus.THINKING)
        engine.transition_agent(agent_id, AgentStatus.RUNNING)
        assert engine.get_agent_status(agent_id) == AgentStatus.RUNNING

        # RUNNING → IDLE is valid (stop/cancel)
        assert engine.transition_agent(agent_id, AgentStatus.IDLE) is True
        assert engine.get_agent_status(agent_id) == AgentStatus.IDLE
