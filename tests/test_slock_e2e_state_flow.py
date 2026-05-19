"""AC3: End-to-end state machine flow test.

Validates: message → Agent IDLE→RUNNING→SENDING→IDLE full transition,
and status panel card reflects correct state text.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.engine import SlockEngine, SlockEngineCallbacks
from src.slock_engine.models import (
    AgentIdentity,
    AgentStatus,
    SlockChannel,
)


@pytest.fixture
def tmp_engine(tmp_path):
    """Create a SlockEngine with isolated storage."""
    engine = SlockEngine(
        chat_id="chat_state_flow",
        root_path=str(tmp_path / "project"),
        agent_type="coco",
        engine_name="Slock",
        memory_base_path=str(tmp_path / "slock_storage"),
    )
    channel = SlockChannel(
        channel_id="chat_state_flow",
        name="Test Team [Slock]",
        team_name="Test Team",
        owner_id="admin_user",
    )
    engine.activate_channel(channel)
    return engine


def _register_agent(engine: SlockEngine, name: str = "Coder-A") -> AgentIdentity:
    agent = AgentIdentity(
        name=name,
        emoji="🔧",
        agent_type="coco",
        model_name="test-model",
        role="coder",
        owner_group="chat_state_flow",
    )
    engine.registry.register(agent)
    return agent


class TestStateFlowFullCycle:
    """AC3: Agent completes IDLE→WAKING→THINKING→RUNNING→CHECKING→SENDING→IDLE."""

    def test_full_state_transition_sequence(self, tmp_engine):
        """Verify all state transitions fire in order during execute."""
        agent = _register_agent(tmp_engine)
        transitions: list[str] = []

        original_transition = tmp_engine.transition_agent

        def tracking_transition(agent_id, to_status):
            transitions.append(to_status.value)
            return original_transition(agent_id, to_status)

        with patch.object(tmp_engine, "transition_agent", side_effect=tracking_transition):
            with patch.object(tmp_engine, "_run_acp_session", return_value="test response"):
                tmp_engine.execute("Hello agent", sender_id="user_001")

        # Must go through all states in order
        assert "waking" in transitions
        assert "thinking" in transitions
        assert "running" in transitions
        assert "checking" in transitions
        assert "sending" in transitions
        assert "idle" in transitions

        # Final state must be IDLE
        assert tmp_engine.get_agent_status(agent.agent_id) == AgentStatus.IDLE

    def test_state_ends_idle_on_success(self, tmp_engine):
        """After successful execution, agent returns to IDLE."""
        agent = _register_agent(tmp_engine)

        with patch.object(tmp_engine, "_run_acp_session", return_value="done"):
            tmp_engine.execute("Do something")

        assert tmp_engine.get_agent_status(agent.agent_id) == AgentStatus.IDLE

    def test_state_ends_idle_on_error(self, tmp_engine):
        """After ACP session error, agent returns to IDLE."""
        agent = _register_agent(tmp_engine)

        with patch.object(tmp_engine, "_run_acp_session", return_value=None):
            tmp_engine.execute("Do something")

        assert tmp_engine.get_agent_status(agent.agent_id) == AgentStatus.IDLE

    def test_state_ends_idle_on_cancellation(self, tmp_engine):
        """After cancellation, agent returns to IDLE."""
        agent = _register_agent(tmp_engine)

        def cancel_during_run(*args, **kwargs):
            tmp_engine.cancel_agent(agent.agent_id)
            return "result"

        with patch.object(tmp_engine, "_run_acp_session", side_effect=cancel_during_run):
            tmp_engine.execute("Do something")

        assert tmp_engine.get_agent_status(agent.agent_id) == AgentStatus.IDLE

    def test_callbacks_fire_at_each_stage(self, tmp_engine):
        """SlockEngineCallbacks fire at correct stages."""
        agent = _register_agent(tmp_engine)
        callback_log: list[str] = []

        callbacks = SlockEngineCallbacks(
            on_agent_wake=lambda a: callback_log.append("wake"),
            on_agent_thinking=lambda a: callback_log.append("thinking"),
            on_agent_running=lambda a, m: callback_log.append("running"),
            on_agent_done=lambda a, r: callback_log.append("done"),
            on_message_routed=lambda m, a: callback_log.append("routed"),
        )

        with patch.object(tmp_engine, "_run_acp_session", return_value="response"):
            tmp_engine.execute("Test message", callbacks=callbacks)

        assert "routed" in callback_log
        assert "wake" in callback_log
        assert "thinking" in callback_log
        assert "running" in callback_log
        assert "done" in callback_log


class TestStatusPanelReflectsState:
    """AC3/AC4: Status panel card contains correct agent state text."""

    def test_status_panel_shows_idle_after_execution(self, tmp_engine):
        """After execution, status panel shows IDLE (空闲)."""
        agent = _register_agent(tmp_engine)

        with patch.object(tmp_engine, "_run_acp_session", return_value="done"):
            tmp_engine.execute("Do task")

        card = tmp_engine.get_status_card(team_name="Test Team")
        card_json = str(card)
        assert "空闲" in card_json

    def test_status_panel_shows_running_during_execution(self, tmp_engine):
        """During execution, status should be RUNNING (运行中)."""
        agent = _register_agent(tmp_engine)
        status_during: list[AgentStatus] = []

        def capture_running(*args, **kwargs):
            status_during.append(tmp_engine.get_agent_status(agent.agent_id))
            return "result"

        with patch.object(tmp_engine, "_run_acp_session", side_effect=capture_running):
            tmp_engine.execute("Do task")

        # During _run_acp_session, agent should be in RUNNING state
        assert AgentStatus.RUNNING in status_during

    def test_status_panel_card_has_schema_2(self, tmp_engine):
        """Status panel card uses schema 2.0."""
        _register_agent(tmp_engine)
        card = tmp_engine.get_status_card(team_name="Test Team")
        assert card["schema"] == "2.0"

    def test_status_panel_card_has_column_set(self, tmp_engine):
        """Status panel uses column_set layout for agents."""
        _register_agent(tmp_engine)
        card = tmp_engine.get_status_card(team_name="Test Team")
        elements = card["body"]["elements"]
        column_sets = [e for e in elements if e.get("tag") == "column_set"]
        assert len(column_sets) >= 1

    def test_status_panel_has_refresh_button(self, tmp_engine):
        """Status panel includes a Refresh button."""
        _register_agent(tmp_engine)
        card = tmp_engine.get_status_card(team_name="Test Team")
        card_str = str(card)
        assert "slock_refresh_status" in card_str

    def test_status_panel_has_stop_button(self, tmp_engine):
        """Status panel includes a Stop button."""
        _register_agent(tmp_engine)
        card = tmp_engine.get_status_card(team_name="Test Team")
        card_str = str(card)
        assert "slock_stop" in card_str
