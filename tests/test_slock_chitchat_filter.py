"""Tests for chitchat filter preventing skill_profile pollution (Task 5).

Verifies that non-technical messages filtered by `_is_chitchat()` in TaskRouter:
1. Cause `route_message()` to return None immediately
2. Never update any agent's skill_profile (no call to set_skill_profiles,
   record_skill_feedback, or _score_and_assign)

Architecture guarantee:
- route_message() returns None on chitchat BEFORE reaching _score_and_assign()
- engine.process_message() returns None when routing returns None (line 1153-1154)
- _execute_agent() is never called, so record_skill_feedback() is never invoked
- Observer queue and council manager are also never triggered
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.models import AgentIdentity, AgentStatus, SkillProfile
from src.slock_engine.task_router import TaskRouter


def _make_agent(agent_id: str = "agent-001", name: str = "Coder") -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        name=name,
        emoji="🔧",
        agent_type="codex",
        model_name="o3-pro",
        system_prompt="You are a coding assistant.",
        role="coder",
        permissions=["shell", "file_write", "git"],
    )


class TestChitchatFilterBlocksRouting:
    """Chitchat messages must be rejected by route_message and never reach agents."""

    CHITCHAT_MESSAGES = [
        "今天天气不错",
        "你好",
        "hi",
        "hello!",
        "哈哈哈",
        "好的",
        "谢谢",
        "下班了",
        "吃什么",
        "周末去哪",
        "good morning",
        "lol",
        "",
        "   ",
    ]

    TECHNICAL_MESSAGES = [
        "请帮我 review 这段 code",
        "deploy 到 staging 环境",
        "这个 bug 需要 fix",
        "!今天天气不错",  # force prefix bypasses filter
        "@Coder 你好",   # @mention bypasses filter
    ]

    @pytest.mark.parametrize("text", CHITCHAT_MESSAGES)
    def test_route_message_returns_none_for_chitchat(self, text):
        """route_message returns None for chitchat, preventing agent activation."""
        router = TaskRouter()
        agent = _make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        result = router.route_message(text, [agent])
        assert result is None, f"Expected None for chitchat '{text}', got {result}"

    @pytest.mark.parametrize("text", TECHNICAL_MESSAGES)
    def test_route_message_routes_technical_messages(self, text):
        """Technical messages are NOT filtered and reach agents."""
        router = TaskRouter()
        agent = _make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        result = router.route_message(text, [agent])
        assert result is not None, f"Expected routing for technical msg '{text}', got None"


class TestChitchatFilterPreventsSkillPollution:
    """Verify that chitchat never updates any agent's skill_profile."""

    def test_skill_profiles_unchanged_after_chitchat(self):
        """Sending chitchat must leave skill_profiles dict completely untouched."""
        router = TaskRouter()
        agent = _make_agent(agent_id="agent-coder", name="Coder")
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        # Pre-set a known skill profile
        initial_profiles = [
            SkillProfile(tag="code", success_rate=80.0, total_tasks=10, last_active=1000.0),
        ]
        router.set_skill_profiles(agent.agent_id, initial_profiles)

        # Send multiple chitchat messages
        chitchat_messages = [
            "今天天气不错",
            "你好啊",
            "哈哈",
            "周末快乐",
            "good night!",
        ]
        for msg in chitchat_messages:
            result = router.route_message(msg, [agent])
            assert result is None

        # Verify skill profiles are completely unchanged
        with router._lock:
            profiles_after = router._skill_profiles.get(agent.agent_id, [])

        assert len(profiles_after) == 1
        assert profiles_after[0].tag == "code"
        assert profiles_after[0].success_rate == 80.0
        assert profiles_after[0].total_tasks == 10
        assert profiles_after[0].last_active == 1000.0

    def test_score_and_assign_not_called_for_chitchat(self):
        """_score_and_assign must never be called when chitchat is detected."""
        router = TaskRouter()
        agent = _make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        with patch.object(router, "_score_and_assign") as mock_score:
            result = router.route_message("今天天气不错", [agent])
            assert result is None
            mock_score.assert_not_called()

    def test_memory_backend_not_consulted_for_chitchat(self):
        """Memory backend read/write should never be triggered for chitchat."""
        mock_memory = MagicMock()
        mock_memory.read_skill_profiles.return_value = []

        router = TaskRouter(memory_backend=mock_memory)
        agent = _make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        result = router.route_message("你好呀", [agent])
        assert result is None

        # Memory backend must not have been called
        mock_memory.read_skill_profiles.assert_not_called()
        mock_memory.write_skill_profiles.assert_not_called()
        mock_memory.record_skill_feedback.assert_not_called()

    def test_multiple_agents_profiles_untouched_after_chitchat(self):
        """No agent in the pool should have profiles mutated by chitchat."""
        router = TaskRouter()
        agents = [
            _make_agent(agent_id="agent-a", name="Alice"),
            _make_agent(agent_id="agent-b", name="Bob"),
            _make_agent(agent_id="agent-c", name="Charlie"),
        ]
        for a in agents:
            router.set_agent_status(a.agent_id, AgentStatus.IDLE)
            router.set_skill_profiles(a.agent_id, [
                SkillProfile(tag="general", success_rate=50.0, total_tasks=5),
            ])

        # Capture timestamps after initial set
        with router._lock:
            ts_before = dict(router._skill_profile_ts)

        # Fire chitchat
        for msg in ["哈哈", "ok", "嗯", "晚安"]:
            result = router.route_message(msg, agents)
            assert result is None

        # Verify all profiles unchanged
        with router._lock:
            for a in agents:
                profiles = router._skill_profiles.get(a.agent_id, [])
                assert len(profiles) == 1
                assert profiles[0].tag == "general"
                assert profiles[0].success_rate == 50.0
                assert profiles[0].total_tasks == 5
            # Timestamps should not have been updated
            ts_after = dict(router._skill_profile_ts)

        assert ts_before == ts_after

    def test_skip_chitchat_flag_bypasses_filter(self):
        """skip_chitchat=True allows chitchat-like messages to reach scoring."""
        router = TaskRouter()
        agent = _make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        # With skip_chitchat=True, even a chitchat message should route
        result = router.route_message("你好", [agent], skip_chitchat=True)
        assert result is not None
        assert result.agent_id == agent.agent_id


class TestChitchatFilterEdgeCases:
    """Edge cases for chitchat detection and skill_profile safety."""

    def test_empty_agents_list_returns_none_without_profile_check(self):
        """Empty agent list returns None before chitchat check runs."""
        router = TaskRouter()
        result = router.route_message("今天天气不错", [])
        assert result is None

    def test_force_prefix_bypasses_chitchat(self):
        """Messages with ! prefix bypass chitchat filter."""
        router = TaskRouter()
        agent = _make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        result = router.route_message("!今天天气不错", [agent])
        assert result is not None

    def test_at_mention_bypasses_chitchat(self):
        """@mention is never filtered as chitchat."""
        router = TaskRouter()
        agent = _make_agent(agent_id="agent-coder", name="Coder")
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        result = router.route_message("@Coder 你好", [agent])
        assert result is not None
        assert result.agent_id == agent.agent_id

    def test_non_idle_agents_not_routed_even_for_technical(self):
        """Only IDLE agents are considered; RUNNING agents are excluded."""
        router = TaskRouter()
        agent = _make_agent()
        router.set_agent_status(agent.agent_id, AgentStatus.RUNNING)

        result = router.route_message("请帮我 fix 这个 bug", [agent])
        assert result is None


class TestChitchatSkillProfileUpdateBehavior:
    """Tests for update_skill_profile_for_task CHITCHAT guard."""

    def test_chitchat_skips_skill_profile_update(self):
        """When classified as CHITCHAT, skill_profile is NOT updated."""
        router = TaskRouter()
        agent = _make_agent(agent_id="agent-coder", name="Coder")
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        mock_memory = MagicMock()
        # Pre-set profiles so we can verify they remain unchanged
        initial_profiles = [
            SkillProfile(tag="code", success_rate=90.0, total_tasks=20, last_active=500.0),
        ]
        router.set_skill_profiles(agent.agent_id, initial_profiles)

        # Call update_skill_profile_for_task with a chitchat message
        result = router.update_skill_profile_for_task(
            agent.agent_id, "今天天气不错", mock_memory
        )

        # Should return None without calling memory backend
        assert result is None
        mock_memory.record_skill_feedback.assert_not_called()

        # Verify profiles are unchanged
        with router._lock:
            profiles = router._skill_profiles.get(agent.agent_id, [])
        assert len(profiles) == 1
        assert profiles[0].tag == "code"
        assert profiles[0].success_rate == 90.0
        assert profiles[0].total_tasks == 20

    def test_non_chitchat_updates_skill_profile(self):
        """Normal (non-CHITCHAT) messages DO update skill_profile."""
        router = TaskRouter()
        agent = _make_agent(agent_id="agent-coder", name="Coder")
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        mock_memory = MagicMock()
        updated_profiles = [
            SkillProfile(tag="code", success_rate=95.0, total_tasks=21, last_active=600.0),
        ]
        mock_memory.record_skill_feedback.return_value = updated_profiles

        # Call update_skill_profile_for_task with a technical message
        result = router.update_skill_profile_for_task(
            agent.agent_id, "请帮我 fix 这个 bug", mock_memory, quality_score=95.0
        )

        # Should return updated profiles
        assert result is not None
        assert result == updated_profiles
        mock_memory.record_skill_feedback.assert_called_once_with(
            agent.agent_id, ["code"], quality_score=95.0
        )

        # Verify profiles are updated in the router
        with router._lock:
            profiles = router._skill_profiles.get(agent.agent_id, [])
        assert len(profiles) == 1
        assert profiles[0].tag == "code"
        assert profiles[0].success_rate == 95.0
        assert profiles[0].total_tasks == 21

    def test_chitchat_still_routes_to_agent(self):
        """CHITCHAT messages are still delivered to agent (just no profile update).

        When skip_chitchat=True or via route_mention, chitchat-like messages
        still reach agents. Only update_skill_profile_for_task blocks the
        skill_profile write.
        """
        router = TaskRouter()
        agent = _make_agent(agent_id="agent-coder", name="Coder")
        router.set_agent_status(agent.agent_id, AgentStatus.IDLE)

        chitchat_msg = "你好"

        # Verify this IS classified as chitchat (route_message returns None)
        normal_result = router.route_message(chitchat_msg, [agent])
        assert normal_result is None

        # But with skip_chitchat=True, the message still reaches the agent
        forced_result = router.route_message(chitchat_msg, [agent], skip_chitchat=True)
        assert forced_result is not None
        assert forced_result.agent_id == agent.agent_id

        # And update_skill_profile_for_task still blocks the profile update
        mock_memory = MagicMock()
        profile_result = router.update_skill_profile_for_task(
            agent.agent_id, chitchat_msg, mock_memory
        )
        assert profile_result is None
        mock_memory.record_skill_feedback.assert_not_called()
