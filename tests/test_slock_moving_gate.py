"""Tests for MOVING gate — agents in MOVING or non-IDLE states cannot be executed.

Validates acceptance criteria:
AC1: A MOVING agent is never executed (try_lock_for_move blocks execute).
AC2: Router returns None when all agents are non-IDLE.
AC3: @mention routing respects MOVING state (IDLE filter excludes before mention matching).
AC4: _execute_agent early-returns when transition_agent(WAKING) returns False.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from src.slock_engine.models import AgentIdentity, AgentStatus
from src.slock_engine.task_router import TaskRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    agent_id: str = "agent-001",
    name: str = "TestBot",
    owner_group: str = "test-group",
) -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        name=name,
        emoji="🤖",
        agent_type="coco",
        model_name="test-model",
        system_prompt="You are a test agent.",
        role="coder",
        permissions=["shell", "file_write"],
        owner_group=owner_group,
        member_groups=[owner_group],
    )


# ===========================================================================
# AC1: TestMovingAgentBlocksExecution
# ===========================================================================


class TestMovingAgentBlocksExecution:
    """AC1: A MOVING agent is never executed via the engine.execute() path.

    After try_lock_for_move() sets an agent to MOVING, calling execute()
    must NOT run the ACP session, and the agent must remain MOVING.
    """

    @patch("src.slock_engine.engine.get_settings")
    @patch("src.slock_engine.engine.create_engine_session")
    def test_moving_agent_not_executed(self, mock_create_session, mock_settings, tmp_path):
        """Register an agent, lock it for move, call execute().
        Assert _run_acp_session was never called AND agent status is still MOVING."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.models import SlockChannel

        # Configure settings mock
        settings = MagicMock()
        settings.slock_max_parallel_agents = 4
        settings.slock_max_queue_size = 16
        settings.slock_escalation_timeout = 300
        settings.slock_agent_execution_timeout = 120
        settings.coco_execution_timeout = 60
        mock_settings.return_value = settings

        base = str(tmp_path / "moving_gate_test")
        engine = SlockEngine(
            chat_id="test-chat",
            root_path=str(tmp_path / "workspace"),
            memory_base_path=base,
        )

        # Activate channel
        channel = SlockChannel(
            channel_id="test-chat",
            name="TestChannel",
            team_name="TestTeam",
            owner_id="owner-001",
        )
        engine.activate_channel(channel)

        # Register an agent
        agent = _make_agent("gate-agent-001", name="GateBot", owner_group="test-chat")
        engine._registry.register(agent)

        # Lock for move: IDLE -> MOVING
        locked = engine.try_lock_for_move("gate-agent-001")
        assert locked is True
        assert engine.get_agent_status("gate-agent-001") == AgentStatus.MOVING

        # Patch _run_acp_session to track if it gets called
        engine._run_acp_session = MagicMock(return_value="should not be called")

        # Execute a message - the router will filter out the MOVING agent
        result = engine.execute("Hello GateBot, do something", sender_id="user-001")

        # ACP session was never invoked
        engine._run_acp_session.assert_not_called()

        # Agent status is still MOVING (not reset by execute)
        assert engine.get_agent_status("gate-agent-001") == AgentStatus.MOVING

        # Result is None (no agent was available)
        assert result is None


# ===========================================================================
# AC2: TestRouterReturnsNoneWhenAllBusy
# ===========================================================================


class TestRouterReturnsNoneWhenAllBusy:
    """AC2: Router returns None when all agents are in non-IDLE states.

    The IDLE hard filter at the top of route_message() excludes all
    non-IDLE agents before any scoring or mention matching.
    """

    def test_all_agents_non_idle_returns_none(self):
        """Create router with 2 agents (RUNNING, MOVING). route_message returns None."""
        router = TaskRouter()

        agent_a = _make_agent("busy-001", name="RunnerBot")
        agent_b = _make_agent("busy-002", name="MoverBot")

        # Set both agents to non-IDLE states
        router.set_agent_status("busy-001", AgentStatus.RUNNING)
        router.set_agent_status("busy-002", AgentStatus.MOVING)

        result = router.route_message("Please help me with code", [agent_a, agent_b])
        assert result is None

    def test_one_idle_one_busy_returns_idle(self):
        """Create router with 2 agents: one IDLE, one MOVING. Returns the IDLE agent."""
        router = TaskRouter()

        agent_idle = _make_agent("idle-001", name="IdleBot")
        agent_moving = _make_agent("moving-001", name="MovingBot")

        # One IDLE (default), one MOVING
        router.set_agent_status("moving-001", AgentStatus.MOVING)
        # idle-001 defaults to IDLE (not explicitly set)

        result = router.route_message("Help me review this code", [agent_idle, agent_moving])
        assert result is not None
        assert result.agent_id == "idle-001"


# ===========================================================================
# AC3: TestMentionRoutingRespectsMoving
# ===========================================================================


class TestMentionRoutingRespectsMoving:
    """AC3: @mention routing respects MOVING state.

    The IDLE filter excludes agents before mention matching, so even a
    direct @mention of a MOVING agent returns None.
    """

    def test_mention_moving_agent_returns_none(self):
        """Set agent to MOVING, send '@AgentName' message. Returns None."""
        router = TaskRouter()

        agent = _make_agent("mention-001", name="MentionBot")
        router.set_agent_status("mention-001", AgentStatus.MOVING)

        # Message directly mentions the agent by name
        result = router.route_message("@MentionBot please review this", [agent])
        assert result is None

    def test_mention_idle_agent_succeeds(self):
        """Same setup but agent is IDLE. Returns the agent."""
        router = TaskRouter()

        agent = _make_agent("mention-002", name="MentionBot")
        # Agent is IDLE by default (not explicitly set, defaults to IDLE)

        result = router.route_message("@MentionBot please review this", [agent])
        assert result is not None
        assert result.agent_id == "mention-002"
        assert result.name == "MentionBot"


# ===========================================================================
# AC4: TestExecuteAgentEarlyReturn
# ===========================================================================


class TestExecuteAgentEarlyReturn:
    """AC4: _execute_agent early-returns when transition_agent(WAKING) returns False.

    When an agent cannot transition from its current state to WAKING
    (e.g., it is already MOVING or RUNNING), _execute_agent must:
    - NOT call _memory.read_agent_memory
    - NOT call _build_agent_prompt
    - NOT call _run_acp_session
    - Return None
    """

    @patch("src.slock_engine.engine.get_settings")
    def test_transition_false_skips_execution(self, mock_settings, tmp_path):
        """Patch transition_agent to return False. Call _execute_agent().
        Assert no memory read, no prompt build, no ACP session. Returns None."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.models import SlockChannel

        # Configure settings mock
        settings = MagicMock()
        settings.slock_max_parallel_agents = 4
        settings.slock_max_queue_size = 16
        settings.slock_escalation_timeout = 300
        settings.slock_agent_execution_timeout = 120
        settings.coco_execution_timeout = 60
        mock_settings.return_value = settings

        base = str(tmp_path / "early_return_test")
        engine = SlockEngine(
            chat_id="test-chat-er",
            root_path=str(tmp_path / "workspace"),
            memory_base_path=base,
        )

        # Activate channel
        channel = SlockChannel(
            channel_id="test-chat-er",
            name="EarlyReturnChannel",
            team_name="ERTeam",
            owner_id="owner-002",
        )
        engine.activate_channel(channel)

        agent = _make_agent("er-agent-001", name="EarlyReturnBot", owner_group="test-chat-er")
        engine._registry.register(agent)

        # Set agent to a non-IDLE state so transition_agent(WAKING) will fail
        engine.set_agent_status("er-agent-001", AgentStatus.MOVING)

        # Patch internal methods to verify they are NOT called
        engine._memory.read_agent_memory = MagicMock(return_value=None)
        engine._build_agent_prompt = MagicMock(return_value="should not be called")
        engine._run_acp_session = MagicMock(return_value="should not be called")

        # Call _execute_agent directly
        result = engine._execute_agent(agent, "Hello, do something", None)

        # Verify early return
        assert result is None

        # Verify none of the downstream methods were called
        engine._memory.read_agent_memory.assert_not_called()
        engine._build_agent_prompt.assert_not_called()
        engine._run_acp_session.assert_not_called()
