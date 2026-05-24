"""Tests for skill-based agent routing in SlockEngine._resolve_agent_for_role.

Covers:
1. Neutral score (50.0) returned for agents with no skill profiles
2. Higher-scoring agent selected when multiple match same role
3. Graceful fallback on exception in get_skill_profiles
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.models import AgentIdentity, AgentStatus, SkillProfile


class TestComputeSkillScore:
    """Tests for _compute_skill_score logic."""

    def _make_engine_with_router(self, profiles_map: dict[str, list[SkillProfile]]):
        """Create a minimal engine-like object with mocked router."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        engine._router = MagicMock()
        engine._router.get_skill_profiles = MagicMock(
            side_effect=lambda agent_id: profiles_map.get(agent_id, [])
        )
        return engine

    def test_neutral_score_when_no_profiles(self):
        """Agent with no skill profiles gets neutral 50.0 score."""
        engine = self._make_engine_with_router({})
        score = engine._compute_skill_score("agent-new")
        assert score == 50.0

    def test_average_success_rate_as_score(self):
        """Score is the average success_rate across all profiles."""
        profiles = [
            SkillProfile(tag="python", success_rate=90.0, total_tasks=10),
            SkillProfile(tag="review", success_rate=80.0, total_tasks=5),
        ]
        engine = self._make_engine_with_router({"agent-1": profiles})
        score = engine._compute_skill_score("agent-1")
        assert score == pytest.approx(85.0)

    def test_single_profile_returns_its_rate(self):
        profiles = [SkillProfile(tag="testing", success_rate=72.5, total_tasks=3)]
        engine = self._make_engine_with_router({"agent-2": profiles})
        score = engine._compute_skill_score("agent-2")
        assert score == pytest.approx(72.5)

    def test_exception_returns_neutral_score(self):
        """If get_skill_profiles raises, fallback to 50.0."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        engine._router = MagicMock()
        engine._router.get_skill_profiles = MagicMock(side_effect=RuntimeError("db error"))

        score = engine._compute_skill_score("agent-broken")
        assert score == 50.0


class TestResolveAgentForRole:
    """Tests for _resolve_agent_for_role with skill scoring."""

    def test_selects_highest_scoring_agent(self):
        """When multiple agents have same role, highest skill score wins."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        engine._lock = __import__("threading").RLock()

        # Two agents with role "coder"
        agent_a = AgentIdentity(agent_id="a1", name="Alice", role="coder", agent_type="coco")
        agent_b = AgentIdentity(agent_id="a2", name="Bob", role="coder", agent_type="coco")

        channel = MagicMock()
        channel.agents = ["a1", "a2"]
        engine._channel = channel

        engine._registry = MagicMock()
        engine._registry.get = MagicMock(side_effect=lambda aid: {"a1": agent_a, "a2": agent_b}.get(aid))

        engine._agent_statuses = {"a1": AgentStatus.IDLE, "a2": AgentStatus.IDLE}

        # Bob has better skills
        engine._router = MagicMock()
        engine._router.get_skill_profiles = MagicMock(side_effect=lambda aid: {
            "a1": [SkillProfile(tag="python", success_rate=60.0)],
            "a2": [SkillProfile(tag="python", success_rate=95.0)],
        }.get(aid, []))

        result = engine._resolve_agent_for_role("coder", "ch1")
        assert result is not None
        assert result.agent_id == "a2"  # Bob wins with 95 > 60

    def test_skips_non_idle_agents(self):
        """Non-IDLE agents are excluded from selection regardless of skill."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        engine._lock = __import__("threading").RLock()

        agent_busy = AgentIdentity(agent_id="busy", name="Busy", role="coder", agent_type="coco")
        agent_idle = AgentIdentity(agent_id="idle", name="Idle", role="coder", agent_type="coco")

        channel = MagicMock()
        channel.agents = ["busy", "idle"]
        engine._channel = channel
        engine._registry = MagicMock()
        engine._registry.get = MagicMock(
            side_effect=lambda aid: {"busy": agent_busy, "idle": agent_idle}.get(aid)
        )
        engine._agent_statuses = {"busy": AgentStatus.RUNNING, "idle": AgentStatus.IDLE}
        engine._router = MagicMock()
        engine._router.get_skill_profiles = MagicMock(return_value=[])

        result = engine._resolve_agent_for_role("coder", "ch1")
        assert result is not None
        assert result.agent_id == "idle"

    def test_returns_none_when_no_channel(self):
        """Returns None when engine has no active channel."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        engine._channel = None

        result = engine._resolve_agent_for_role("coder", "ch1")
        assert result is None
