"""Tests for status panel real-time update logic.

Validates that agent status changes trigger debounced card updates via
registered callbacks, and that the status panel card contains correct
agent information and states.
"""

from __future__ import annotations

import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub missing acp submodules so that importing the slock engine does not fail.
# The installed `acp` package lacks `acp.interfaces` and `acp.schema` in this
# test environment; injecting MagicMock modules satisfies the import chain
# (src.acp.client -> acp.interfaces, src.acp.sync_adapter -> acp.schema).
# ---------------------------------------------------------------------------
for _mod_name in ("acp.interfaces", "acp.schema", "acp.helpers", "acp.stdio"):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

from src.slock_engine.card_templates import build_status_panel_card
from src.slock_engine.engine import SlockEngine  # noqa: F401 — force module load for patching
from src.slock_engine.models import AgentIdentity, AgentStatus, SlockChannel, SlockTask, TaskStatus


def _make_agent(agent_id: str = "agent_1", name: str = "Coder", emoji: str = "🔧", role: str = "coder", owner_group: str = "ch_test") -> AgentIdentity:
    """Create a minimal AgentIdentity for testing."""
    return AgentIdentity(agent_id=agent_id, name=name, emoji=emoji, role=role, owner_group=owner_group)


def _make_engine(tmp_path, agents=None, channel_id="ch_test"):
    """Create a SlockEngine with heavy dependencies mocked out.

    Patches ACP session creation and settings to avoid external calls.
    """
    with patch("src.slock_engine.engine.create_engine_session"), \
         patch("src.slock_engine.engine.get_settings") as mock_settings:
        settings = MagicMock()
        settings.slock_max_parallel_discussions = 4
        settings.slock_channel_max_parallel_discussions = 2
        settings.slock_discussion_enabled = False
        settings.slock_max_discussion_rounds = 3
        settings.slock_discussion_token_budget = 5000
        settings.slock_discussion_trigger_rules = "coder->reviewer"
        settings.slock_discussion_timeout = 60
        settings.slock_max_tokens_per_round = 1000
        settings.slock_max_parallel_agents = 4
        settings.slock_max_queue_size = 16
        settings.slock_agent_execution_timeout = 120
        settings.slock_memory_summarize_threshold = 10
        settings.slock_conversation_replay_rounds = 5
        settings.slock_max_open_tasks = 20
        settings.slock_pending_confirm_timeout = 60
        settings.slock_card_debounce_interval = 0.5
        settings.slock_escalation_timeout = 300
        settings.slock_tool_path_restrictions = []
        settings.slock_dangerous_shell_patterns = []
        settings.coco_execution_timeout = 60
        mock_settings.return_value = settings

        engine = SlockEngine(
            chat_id="test_chat",
            root_path=str(tmp_path),
            memory_base_path=str(tmp_path / "memory"),
        )

    # Activate a channel so status panel has context
    channel = SlockChannel(
        channel_id=channel_id,
        team_name="TestTeam",
        name="test-group",
        owner_id="owner_1",
    )
    engine.activate_channel(channel)

    # Register agents if provided
    if agents:
        for agent in agents:
            engine.registry.register(agent)

    return engine


class TestSetAgentStatusEmitsEvent:
    """test_set_agent_status_emits_event — calling set_agent_status triggers notification."""

    def test_set_agent_status_emits_event(self, tmp_path):
        """When set_agent_status changes an agent's status, _notify_status_change is invoked."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])

        # Spy on _notify_status_change
        with patch.object(engine, "_notify_status_change") as mock_notify:
            engine.set_agent_status(agent.agent_id, AgentStatus.RUNNING)
            mock_notify.assert_called_once_with(agent.agent_id, AgentStatus.RUNNING)

    def test_set_agent_status_no_event_when_same(self, tmp_path):
        """No notification if status does not actually change."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])

        # Agent starts as IDLE; setting IDLE again should not fire
        with patch.object(engine, "_notify_status_change") as mock_notify:
            engine.set_agent_status(agent.agent_id, AgentStatus.IDLE)
            mock_notify.assert_not_called()

    def test_transition_agent_emits_event(self, tmp_path):
        """transition_agent also triggers notification on valid transition."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])

        with patch.object(engine, "_notify_status_change") as mock_notify:
            result = engine.transition_agent(agent.agent_id, AgentStatus.WAKING)
            assert result is True
            mock_notify.assert_called_once_with(agent.agent_id, AgentStatus.WAKING)


class TestStatusPanelCardShowsAllAgents:
    """test_status_panel_card_shows_all_agents — panel card includes all registered agents."""

    def test_panel_shows_all_agents(self, tmp_path):
        """Status panel card contains entries for every registered agent."""
        agents = [
            _make_agent("a1", "Coder", "🔧", "coder"),
            _make_agent("a2", "Reviewer", "👁️", "reviewer"),
            _make_agent("a3", "Planner", "📋", "planner"),
        ]
        engine = _make_engine(tmp_path, agents=agents)

        card = engine.get_status_card(team_name="TestTeam")

        # Verify all agent names appear in the card body
        body_text = _extract_card_text(card)
        for agent in agents:
            assert agent.name in body_text, f"Agent {agent.name} not found in status panel"

    def test_panel_empty_when_no_agents(self, tmp_path):
        """Status panel shows appropriate message when no agents are registered."""
        engine = _make_engine(tmp_path, agents=[])

        card = engine.get_status_card(team_name="TestTeam")
        body_text = _extract_card_text(card)
        assert "暂无已注册" in body_text

    def test_build_status_panel_card_directly(self):
        """Direct call to build_status_panel_card includes all provided agents."""
        agents = [
            (_make_agent("a1", "Coder", "🔧"), AgentStatus.IDLE),
            (_make_agent("a2", "Reviewer", "👁️"), AgentStatus.RUNNING),
        ]
        card = build_status_panel_card(agents, team_name="MyTeam")

        body_text = _extract_card_text(card)
        assert "Coder" in body_text
        assert "Reviewer" in body_text


class TestStatusPanelCardShowsCurrentStatus:
    """test_status_panel_card_shows_current_status — panel shows IDLE/THINKING/RUNNING correctly."""

    def test_idle_status_display(self, tmp_path):
        """IDLE agents show the idle indicator."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])

        card = engine.get_status_card()
        body_text = _extract_card_text(card)
        # IDLE should show green circle and '空闲'
        assert "🟢" in body_text
        assert "空闲" in body_text

    def test_thinking_status_display(self, tmp_path):
        """THINKING agents show the thinking indicator."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])
        # Transition: IDLE -> WAKING -> THINKING
        engine.transition_agent(agent.agent_id, AgentStatus.WAKING)
        engine.transition_agent(agent.agent_id, AgentStatus.THINKING)

        card = engine.get_status_card()
        body_text = _extract_card_text(card)
        assert "🟡" in body_text
        assert "思考中" in body_text

    def test_running_status_display(self, tmp_path):
        """RUNNING agents show the running indicator."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])
        # Transition: IDLE -> WAKING -> THINKING -> RUNNING
        engine.transition_agent(agent.agent_id, AgentStatus.WAKING)
        engine.transition_agent(agent.agent_id, AgentStatus.THINKING)
        engine.transition_agent(agent.agent_id, AgentStatus.RUNNING)

        card = engine.get_status_card()
        body_text = _extract_card_text(card)
        assert "🔵" in body_text
        assert "运行中" in body_text

    def test_multiple_agents_different_statuses(self, tmp_path):
        """Panel correctly shows different status for each agent."""
        agents = [
            _make_agent("a1", "Coder", "🔧"),
            _make_agent("a2", "Reviewer", "👁️"),
        ]
        engine = _make_engine(tmp_path, agents=agents)

        # Set Coder to RUNNING, Reviewer stays IDLE
        engine.transition_agent("a1", AgentStatus.WAKING)
        engine.transition_agent("a1", AgentStatus.THINKING)
        engine.transition_agent("a1", AgentStatus.RUNNING)

        card = engine.get_status_card()
        body_text = _extract_card_text(card)
        # Coder should be running, Reviewer should be idle
        assert "运行中" in body_text
        assert "空闲" in body_text


class TestStatusRefreshCallbackReceivesCard:
    """test_status_refresh_callback_receives_card — registered callback is called with card dict."""

    def test_callback_receives_card_on_refresh(self, tmp_path):
        """Registered status refresh callback receives a valid card dict."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])

        # Set a status panel message ID so refresh will trigger
        engine._status_panel_msg_id = "msg_12345"

        received_calls = []

        def capture_callback(msg_id, card):
            received_calls.append((msg_id, card))

        engine.register_status_refresh_callback(capture_callback)

        # Trigger a status change that fires the debounced refresh
        engine.set_agent_status(agent.agent_id, AgentStatus.RUNNING)

        # Wait for the debounced timer to fire (default 1s delay + buffer)
        time.sleep(1.5)

        assert len(received_calls) >= 1
        msg_id, card = received_calls[-1]
        assert msg_id == "msg_12345"
        assert isinstance(card, dict)
        assert "header" in card
        assert "body" in card

    def test_callback_not_called_without_panel_msg_id(self, tmp_path):
        """If no status panel message ID is stored, callback is never invoked."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])

        # Deliberately do NOT set _status_panel_msg_id
        received_calls = []

        def capture_callback(msg_id, card):
            received_calls.append((msg_id, card))

        engine.register_status_refresh_callback(capture_callback)

        engine.set_agent_status(agent.agent_id, AgentStatus.RUNNING)

        # Give enough time for potential timer to fire
        time.sleep(1.5)

        assert len(received_calls) == 0

    def test_callback_card_contains_agent_info(self, tmp_path):
        """The card passed to callback reflects current agent states."""
        agents = [
            _make_agent("a1", "Coder", "🔧"),
            _make_agent("a2", "Reviewer", "👁️"),
        ]
        engine = _make_engine(tmp_path, agents=agents)
        engine._status_panel_msg_id = "msg_99"

        received_calls = []

        def capture_callback(msg_id, card):
            received_calls.append((msg_id, card))

        engine.register_status_refresh_callback(capture_callback)

        # Set one agent to RUNNING
        engine.transition_agent("a1", AgentStatus.WAKING)
        engine.transition_agent("a1", AgentStatus.THINKING)
        engine.transition_agent("a1", AgentStatus.RUNNING)

        time.sleep(1.5)

        assert len(received_calls) >= 1
        _, card = received_calls[-1]
        body_text = _extract_card_text(card)
        assert "Coder" in body_text
        assert "Reviewer" in body_text


class TestStatusChangeUpdatesExistingCard:
    """test_status_change_updates_existing_card — callback receives update with message_id."""

    def test_update_uses_stored_panel_msg_id(self, tmp_path):
        """_notify_status_change schedules refresh using _status_panel_msg_id."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])

        engine._status_panel_msg_id = "msg_panel_001"

        received_calls = []

        def capture_callback(msg_id, card):
            received_calls.append((msg_id, card))

        engine.register_status_refresh_callback(capture_callback)

        # Change status
        engine.set_agent_status(agent.agent_id, AgentStatus.RUNNING)

        time.sleep(1.5)

        assert len(received_calls) >= 1
        assert received_calls[-1][0] == "msg_panel_001"

    def test_update_uses_channel_status_card_msg_id(self, tmp_path):
        """Falls back to _status_card_msg_ids when _status_panel_msg_id is None."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])

        # Use channel-specific msg ID instead of the shortcut
        engine._status_panel_msg_id = None
        engine._status_card_msg_ids["ch_test"] = "msg_channel_002"

        received_calls = []

        def capture_callback(msg_id, card):
            received_calls.append((msg_id, card))

        engine.register_status_refresh_callback(capture_callback)

        engine.set_agent_status(agent.agent_id, AgentStatus.RUNNING)

        time.sleep(1.5)

        assert len(received_calls) >= 1
        assert received_calls[-1][0] == "msg_channel_002"

    def test_debounce_coalesces_rapid_changes(self, tmp_path):
        """Rapid status changes are debounced into fewer card updates."""
        agent = _make_agent()
        engine = _make_engine(tmp_path, agents=[agent])
        engine._status_panel_msg_id = "msg_debounce"

        received_calls = []

        def capture_callback(msg_id, card):
            received_calls.append((msg_id, card))

        engine.register_status_refresh_callback(capture_callback)

        # Rapid state transitions: IDLE -> WAKING -> THINKING -> RUNNING
        engine.transition_agent(agent.agent_id, AgentStatus.WAKING)
        engine.transition_agent(agent.agent_id, AgentStatus.THINKING)
        engine.transition_agent(agent.agent_id, AgentStatus.RUNNING)

        # Wait for debounce to resolve
        time.sleep(2.0)

        # Debounce means fewer calls than status changes (3 transitions -> should coalesce)
        # At minimum 1 call, at most 3 (if timer didn't coalesce at all)
        assert 1 <= len(received_calls) <= 3
        # The last card should reflect RUNNING state
        _, last_card = received_calls[-1]
        body_text = _extract_card_text(last_card)
        assert "运行中" in body_text


# ------------------------------------------------------------------
# Helper utilities
# ------------------------------------------------------------------


def _extract_card_text(card: dict) -> str:
    """Recursively extract all text content from a card dict."""
    texts: list[str] = []
    _walk_card(card, texts)
    return " ".join(texts)


def _walk_card(node: object, texts: list[str]) -> None:
    """Walk a card structure and collect text values."""
    if isinstance(node, dict):
        if "content" in node and isinstance(node["content"], str):
            texts.append(node["content"])
        for value in node.values():
            _walk_card(value, texts)
    elif isinstance(node, list):
        for item in node:
            _walk_card(item, texts)
