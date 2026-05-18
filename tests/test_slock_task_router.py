"""Unit tests for slock_engine/task_router.py — routing + TaskClaim."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from src.slock_engine.models import AgentIdentity, AgentStatus, SkillProfile
from src.slock_engine.task_router import TaskClaim, TaskRouter


class TestTaskClaim:
    def test_claim_success(self):
        tc = TaskClaim()
        assert tc.claim("t1", "a1") is True

    def test_claim_already_held_by_other(self):
        tc = TaskClaim()
        tc.claim("t1", "a1")
        assert tc.claim("t1", "a2") is False

    def test_claim_idempotent_same_agent(self):
        tc = TaskClaim()
        tc.claim("t1", "a1")
        assert tc.claim("t1", "a1") is True

    def test_claim_expired_allows_reclaim(self):
        tc = TaskClaim(default_ttl=0.01)
        tc.claim("t1", "a1")
        time.sleep(0.02)
        assert tc.claim("t1", "a2") is True

    def test_release(self):
        tc = TaskClaim()
        tc.claim("t1", "a1")
        assert tc.release("t1") is True
        assert tc.is_claimed("t1") is False

    def test_release_with_agent_check(self):
        tc = TaskClaim()
        tc.claim("t1", "a1")
        # Wrong agent can't release
        assert tc.release("t1", "a2") is False
        # Correct agent can
        assert tc.release("t1", "a1") is True

    def test_release_nonexistent(self):
        tc = TaskClaim()
        assert tc.release("t_none") is False

    def test_get_holder(self):
        tc = TaskClaim()
        tc.claim("t1", "a1")
        assert tc.get_holder("t1") == "a1"

    def test_get_holder_expired(self):
        tc = TaskClaim(default_ttl=0.01)
        tc.claim("t1", "a1")
        time.sleep(0.02)
        assert tc.get_holder("t1") is None

    def test_is_claimed(self):
        tc = TaskClaim()
        assert tc.is_claimed("t1") is False
        tc.claim("t1", "a1")
        assert tc.is_claimed("t1") is True

    def test_force_assign(self):
        tc = TaskClaim()
        tc.claim("t1", "a1")
        tc.force_assign("t1", "a2")
        assert tc.get_holder("t1") == "a2"


class TestTaskRouter:
    def _make_agent(self, agent_id: str, name: str, role: str = "coder") -> AgentIdentity:
        return AgentIdentity(agent_id=agent_id, name=name, role=role, owner_group="g1")

    def test_route_message_no_agents(self):
        router = TaskRouter()
        assert router.route_message("hello", []) is None

    def test_route_by_mention(self):
        router = TaskRouter()
        agents = [
            self._make_agent("a1", "Alice"),
            self._make_agent("a2", "Bob"),
        ]
        result = router.route_message("@Alice please help", agents)
        assert result is not None
        assert result.agent_id == "a1"

    def test_route_mention_case_insensitive(self):
        router = TaskRouter()
        agents = [self._make_agent("a1", "Charlie")]
        result = router.route_message("@charlie do this", agents)
        assert result is not None
        assert result.agent_id == "a1"

    def test_route_no_mention_falls_to_scoring(self):
        router = TaskRouter()
        agents = [self._make_agent("a1", "Alice")]
        # No @mention, should still route via scoring
        result = router.route_message("implement the feature", agents)
        assert result is not None

    def test_skill_scoring_prefers_idle(self):
        router = TaskRouter()
        a1 = self._make_agent("a1", "Alice")
        a2 = self._make_agent("a2", "Bob")
        router.set_agent_status("a1", AgentStatus.RUNNING)
        router.set_agent_status("a2", AgentStatus.IDLE)
        result = router.route_message("code something", [a1, a2])
        # Idle agent gets higher availability score
        assert result is not None
        assert result.agent_id == "a2"

    def test_skill_scoring_with_profiles(self):
        router = TaskRouter()
        a1 = self._make_agent("a1", "Reviewer")
        a2 = self._make_agent("a2", "Coder")
        router.set_skill_profiles("a1", [SkillProfile(tag="review", success_rate=90.0)])
        router.set_skill_profiles("a2", [SkillProfile(tag="code", success_rate=90.0)])
        # Message about reviewing should prefer a1
        result = router.route_message("please review this PR", [a1, a2])
        assert result is not None
        assert result.agent_id == "a1"

    def test_get_set_agent_status(self):
        router = TaskRouter()
        assert router.get_agent_status("a1") == AgentStatus.IDLE
        router.set_agent_status("a1", AgentStatus.RUNNING)
        assert router.get_agent_status("a1") == AgentStatus.RUNNING

    def test_extract_skill_keywords_code(self):
        router = TaskRouter()
        keywords = router._extract_skill_keywords("implement the function")
        assert "code" in keywords

    def test_extract_skill_keywords_review(self):
        router = TaskRouter()
        keywords = router._extract_skill_keywords("review this change")
        assert "review" in keywords

    def test_extract_skill_keywords_default(self):
        router = TaskRouter()
        keywords = router._extract_skill_keywords("something unrelated xyz")
        assert keywords == ["code"]  # default fallback
