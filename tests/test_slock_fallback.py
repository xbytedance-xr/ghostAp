"""Tests for slock routing fallback and execution retry.

Covers:
- RoutingStatus enum and RoutingResult dataclass
- route_message_with_fallback: IDLE-first, then RUNNING fallback, then NO_MATCH
- Backpressure: MAX_PERSIST_QUEUE_SIZE with sync fallback
- _execute_with_retry: agent retry on execution failure (when implemented)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.slock_engine.models import AgentIdentity, AgentStatus
from src.slock_engine.task_router import RoutingResult, RoutingStatus, TaskRouter

# ============================================================
# RoutingStatus / RoutingResult
# ============================================================


class TestRoutingEnums:
    """Basic enum and dataclass contracts."""

    def test_routing_status_values(self):
        assert RoutingStatus.ASSIGNED.value == "assigned"
        assert RoutingStatus.QUEUE_WAIT.value == "queue_wait"
        assert RoutingStatus.NO_MATCH.value == "no_match"

    def test_routing_result_defaults(self):
        r = RoutingResult(status=RoutingStatus.NO_MATCH)
        assert r.agent is None
        assert r.busy_count == 0

    def test_routing_result_with_agent(self):
        agent = MagicMock(spec=AgentIdentity)
        r = RoutingResult(status=RoutingStatus.ASSIGNED, agent=agent, busy_count=0)
        assert r.agent is agent


# ============================================================
# route_message_with_fallback
# ============================================================


class TestRouteMessageWithFallback:
    """Unit tests for TaskRouter.route_message_with_fallback."""

    def _make_router(self):
        return TaskRouter()

    def _make_agent(self, agent_id="a1", role="coder", agent_type="codex"):
        return AgentIdentity(
            agent_id=agent_id,
            name=role,
            emoji="🤖",
            agent_type=agent_type,
            role=role,
            permissions=["shell"],
            owner_group="chat_t",
        )

    def test_no_agents_returns_no_match(self):
        router = self._make_router()
        result = router.route_message_with_fallback("写代码", [])
        assert result.status == RoutingStatus.NO_MATCH
        assert result.agent is None

    def test_chitchat_returns_no_match(self):
        router = self._make_router()
        agent = self._make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)
        result = router.route_message_with_fallback("ok", [agent])
        assert result.status == RoutingStatus.NO_MATCH

    def test_idle_agent_returns_assigned(self):
        router = self._make_router()
        agent = self._make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)
        result = router.route_message_with_fallback("帮我写一个排序函数", [agent])
        assert result.status == RoutingStatus.ASSIGNED
        assert result.agent is agent

    def test_only_running_agents_returns_queue_wait(self):
        router = self._make_router()
        agent = self._make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.RUNNING)
        result = router.route_message_with_fallback("帮我写一个排序函数", [agent])
        assert result.status == RoutingStatus.QUEUE_WAIT
        assert result.busy_count == 1

    def test_multiple_running_reports_busy_count(self):
        router = self._make_router()
        a1 = self._make_agent("a1", "coder")
        a2 = self._make_agent("a2", "reviewer")
        router.set_agent_status(a1.agent_id, AgentStatus.RUNNING)
        router.set_agent_status(a2.agent_id, AgentStatus.RUNNING)
        result = router.route_message_with_fallback("帮我修复bug", [a1, a2])
        assert result.status == RoutingStatus.QUEUE_WAIT
        assert result.busy_count == 2

    def test_mixed_idle_and_running_prefers_idle(self):
        router = self._make_router()
        idle_agent = self._make_agent("idle1", "coder")
        busy_agent = self._make_agent("busy1", "reviewer")
        router.set_agent_status(idle_agent.agent_id, AgentStatus.IDLE)
        router.set_agent_status(busy_agent.agent_id, AgentStatus.RUNNING)
        result = router.route_message_with_fallback("帮我写一个排序函数", [idle_agent, busy_agent])
        assert result.status == RoutingStatus.ASSIGNED
        assert result.agent is idle_agent

    def test_mention_routes_to_named_idle_agent(self):
        router = self._make_router()
        a1 = self._make_agent("a1", "coder")
        a2 = self._make_agent("a2", "reviewer")
        router.set_agent_status(a1.agent_id, AgentStatus.IDLE)
        router.set_agent_status(a2.agent_id, AgentStatus.IDLE)
        result = router.route_message_with_fallback("@reviewer 帮我检查代码质量", [a1, a2])
        assert result.status == RoutingStatus.ASSIGNED
        assert result.agent is a2


# ============================================================
# Backpressure: AgentRegistry persist queue
# ============================================================


class TestPersistBackpressure:
    """Verify AgentRegistry persist queue backpressure behavior."""

    def test_max_persist_queue_size_constant(self):
        from src.slock_engine.agent_registry import AgentRegistry
        assert AgentRegistry.MAX_PERSIST_QUEUE_SIZE == 256

    def test_sync_fallback_when_queue_full(self, tmp_path):
        """When persist queue is full, _persist falls back to synchronous write."""
        from src.slock_engine.agent_registry import AgentRegistry

        registry = AgentRegistry.legacy(base_path=str(tmp_path / "slock"))
        agent = AgentIdentity(
            agent_id="test:default:abc123",
            name="test_agent",
            emoji="🤖",
            agent_type="codex",
            role="coder",
            permissions=["shell"],
            owner_group="chat_bp",
        )

        # Fill queue to capacity
        registry._persist_queue = [MagicMock()] * registry.MAX_PERSIST_QUEUE_SIZE

        # _persist should now write synchronously
        with patch.object(registry, "_write_agent_to_disk") as mock_write:
            with registry._lock:
                request = registry._persist("test", agent, validated_epoch=0)
            assert request is not None
            registry._persist_request(request)
            mock_write.assert_called_once_with(agent)

    def test_normal_persist_queues(self, tmp_path):
        """Under normal conditions, _persist appends to queue."""
        from src.slock_engine.agent_registry import AgentRegistry

        registry = AgentRegistry.legacy(base_path=str(tmp_path / "slock"))
        agent = AgentIdentity(
            agent_id="test:default:xyz789",
            name="normal_agent",
            emoji="🤖",
            agent_type="claude",
            role="reviewer",
            permissions=["shell"],
            owner_group="chat_n",
        )

        initial_len = len(registry._persist_queue)
        with registry._lock:
            request = registry._persist("test", agent, validated_epoch=0)
        assert request is None
        assert len(registry._persist_queue) == initial_len + 1


# ============================================================
# Routing degradation: original route_message vs with_fallback
# ============================================================


class TestRouteMessageOriginal:
    """Ensure original route_message still returns None when no idle agents."""

    def test_no_idle_returns_none(self):
        router = TaskRouter()
        agent = AgentIdentity(
            agent_id="a1", name="coder", emoji="🤖",
            agent_type="codex", role="coder",
            permissions=["shell"], owner_group="chat_x",
        )
        router.set_agent_status(agent.agent_id, AgentStatus.RUNNING)
        result = router.route_message("帮我写代码", [agent])
        assert result is None

    def test_empty_agents_returns_none(self):
        router = TaskRouter()
        result = router.route_message("帮我写代码", [])
        assert result is None
