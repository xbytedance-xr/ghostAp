"""Unit tests for slock_engine/task_router.py — routing + TaskClaim."""

from __future__ import annotations

import time

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

    def test_route_mention_supports_feishu_markup_and_spaced_names(self):
        """Feishu mention text can include display names with spaces in markup."""
        router = TaskRouter()
        agents = [
            self._make_agent("a1", "Coder Alpha"),
            self._make_agent("a2", "Reviewer"),
        ]
        result = router.route_message('<at user_id="ou_1">Coder Alpha</at> please fix it', agents)
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

    def test_rank_agents_for_claim_competition_orders_all_idle_candidates(self):
        """Task assignment can broadcast a claim opportunity in score order."""
        router = TaskRouter()
        reviewer = self._make_agent("a1", "Reviewer")
        coder = self._make_agent("a2", "Coder")
        router.set_skill_profiles("a1", [SkillProfile(tag="review", success_rate=90.0)])
        router.set_skill_profiles("a2", [SkillProfile(tag="code", success_rate=90.0)])

        ranked = router.rank_agents_for_claim("please review this PR", [coder, reviewer])

        assert [agent.agent_id for agent in ranked] == ["a1", "a2"]

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

    def test_extract_skill_keywords_debug(self):
        """Long-term evolution profiles keep debug as its own durable skill tag."""
        router = TaskRouter()
        keywords = router._extract_skill_keywords("排查线上 timeout 故障并定位 root cause")
        assert "debug" in keywords

    def test_extract_skill_keywords_multi_skill(self):
        """A single task can update multiple skill dimensions for evolution."""
        router = TaskRouter()
        keywords = router._extract_skill_keywords("设计架构并补充测试文档")
        assert {"design", "test", "docs"}.issubset(set(keywords))

    def test_extract_skill_keywords_default(self):
        router = TaskRouter()
        keywords = router._extract_skill_keywords("something unrelated xyz")
        assert keywords == ["general"]  # default fallback


class TestChitchatFilter:
    """Tests for CHITCHAT filtering in TaskRouter (Task 18)."""

    def test_empty_message_is_chitchat(self):
        router = TaskRouter()
        assert router._is_chitchat("") is True
        assert router._is_chitchat("   ") is True

    def test_greetings_are_chitchat(self):
        router = TaskRouter()
        for msg in ("你好", "Hi", "hello!", "早上好", "Hey"):
            assert router._is_chitchat(msg) is True, f"Expected chitchat: {msg}"

    def test_acknowledgments_are_chitchat(self):
        router = TaskRouter()
        for msg in ("ok", "好的", "收到", "thanks", "明白"):
            assert router._is_chitchat(msg) is True, f"Expected chitchat: {msg}"

    def test_short_messages_are_chitchat(self):
        router = TaskRouter()
        assert router._is_chitchat("嗯") is True
        assert router._is_chitchat("哦") is True

    def test_technical_messages_not_chitchat(self):
        router = TaskRouter()
        for msg in ("帮我写一个排序算法", "review the login flow", "运行测试用例"):
            assert router._is_chitchat(msg) is False, f"Not chitchat: {msg}"

    def test_route_message_filters_chitchat(self):
        router = TaskRouter()
        agent = AgentIdentity(agent_id="a1", name="Coder", role="coder")
        router.set_agent_status("a1", AgentStatus.IDLE)
        # Chitchat should return None
        assert router.route_message("你好", [agent]) is None
        # Real task should route
        result = router.route_message("帮我写一个排序算法", [agent])
        assert result is not None
