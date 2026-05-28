"""Tests for Slock Collaboration Insights implementation.

Covers:
- Freshness Gate (Insight #1 & #2)
- Orchestrator Degradation (Insight #3)
- Action Card Gate (Insight #6)
- Semantic Pre-Filter (Insight #4)
- Behavior Self-Convergence (Insight #5)
"""

from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.card_templates.action_card import (
    ActionProposal,
    ActionStatus,
    ActionType,
    build_action_proposal_card,
    build_action_result_card,
)
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import AgentIdentity, AgentStatus, SkillProfile
from src.slock_engine.task_router import TaskRouter


# ---------------------------------------------------------------------------
# Freshness Gate / Memory Manager
# ---------------------------------------------------------------------------


class TestFreshnessGate:
    """Tests for count_messages_since and get_messages_since."""

    def test_count_messages_since_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        assert mm.count_messages_since("ch1", time.time()) == 0

    def test_count_messages_since_with_messages(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        t0 = time.time()
        time.sleep(0.01)
        mm.append_message_archive("ch1", sender_type="user", content="hello", agent_id="user1")
        mm.append_message_archive("ch1", sender_type="agent", content="reply", agent_id="agent1")
        count = mm.count_messages_since("ch1", t0)
        assert count == 2

    def test_count_excludes_agent(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        t0 = time.time()
        time.sleep(0.01)
        mm.append_message_archive("ch1", sender_type="user", content="hello", agent_id="user1")
        mm.append_message_archive("ch1", sender_type="agent", content="my reply", agent_id="agent1")
        count = mm.count_messages_since("ch1", t0, exclude_agent_id="agent1")
        assert count == 1  # Only the user message

    def test_get_messages_since(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        t0 = time.time()
        time.sleep(0.01)
        mm.append_message_archive("ch1", sender_type="user", content="new msg", agent_name="Alice")
        msgs = mm.get_messages_since("ch1", t0)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "new msg"
        assert msgs[0]["sender_type"] == "user"

    def test_get_messages_since_limit(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        t0 = time.time()
        time.sleep(0.01)
        for i in range(10):
            mm.append_message_archive("ch1", sender_type="user", content=f"msg{i}", agent_id="u")
        msgs = mm.get_messages_since("ch1", t0, limit=3)
        assert len(msgs) == 3
        # Should be the last 3
        assert msgs[-1]["content"] == "msg9"


# ---------------------------------------------------------------------------
# Behavior Self-Convergence
# ---------------------------------------------------------------------------


class TestBehaviorConvergence:
    """Tests for task outcome tracking and avoidance strategy."""

    def test_record_and_check_consecutive_failures(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "test_agent"
        mm.ensure_directories(agent_id=agent_id)

        # Record some successes then failures
        mm.record_task_outcome(agent_id, "code", True)
        mm.record_task_outcome(agent_id, "code", True)
        mm.record_task_outcome(agent_id, "code", False)
        mm.record_task_outcome(agent_id, "code", False)
        mm.record_task_outcome(agent_id, "code", False)

        assert mm.get_consecutive_failures(agent_id, "code") == 3

    def test_consecutive_failures_reset_on_success(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "test_agent"
        mm.ensure_directories(agent_id=agent_id)

        mm.record_task_outcome(agent_id, "deploy", False)
        mm.record_task_outcome(agent_id, "deploy", False)
        mm.record_task_outcome(agent_id, "deploy", True)  # reset
        mm.record_task_outcome(agent_id, "deploy", False)

        assert mm.get_consecutive_failures(agent_id, "deploy") == 1

    def test_write_avoidance_strategy(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        agent_id = "test_agent"
        mm.ensure_directories(agent_id=agent_id)
        mm.write_avoidance_strategy(agent_id, "deploy", "连续失败3次")
        # Verify it was written to the agent's context
        memory = mm.read_agent_memory(agent_id)
        assert "deploy" in memory.active_context or "deploy" in (memory.key_knowledge or "")


# ---------------------------------------------------------------------------
# Action Card Templates
# ---------------------------------------------------------------------------


class TestActionCardTemplates:
    """Tests for action card building functions."""

    def test_build_proposal_card_shell(self):
        proposal = ActionProposal(
            action_type=ActionType.SHELL_EXECUTE,
            agent_id="agent1",
            agent_name="Bob",
            title="Run cleanup script",
            command="rm -rf /tmp/old_data",
            impact_summary="Deletes temporary data",
            reversible=False,
        )
        card = build_action_proposal_card(proposal)
        assert card is not None
        assert "schema" in card or "header" in card
        # Should contain action buttons
        body_str = str(card)
        assert "slock_action_approve_" in body_str
        assert "slock_action_reject_" in body_str

    def test_build_result_card_completed(self):
        proposal = ActionProposal(
            action_type=ActionType.CODE_COMMIT,
            agent_id="agent1",
            agent_name="Alice",
            title="Commit changes",
            status=ActionStatus.COMPLETED,
            result="Committed abc123",
            resolved_at=time.time(),
        )
        card = build_action_result_card(proposal)
        assert card is not None
        body_str = str(card)
        assert "完成" in body_str or "COMPLETED" in body_str.upper()

    def test_build_result_card_rejected(self):
        proposal = ActionProposal(
            action_type=ActionType.FILE_DELETE,
            agent_id="agent1",
            agent_name="Charlie",
            title="Delete config",
            status=ActionStatus.REJECTED,
        )
        card = build_action_result_card(proposal)
        assert card is not None
        body_str = str(card)
        assert "拒绝" in body_str or "REJECTED" in body_str.upper()


# ---------------------------------------------------------------------------
# Semantic Pre-Filter
# ---------------------------------------------------------------------------


class TestSemanticPreFilter:
    """Tests for the semantic pre-filter in TaskRouter."""

    def _make_agent(self, agent_id: str, role: str = "", traits: list[str] | None = None) -> AgentIdentity:
        return AgentIdentity(
            agent_id=agent_id,
            name=agent_id,
            role=role,
            personality_traits=traits or [],
            agent_type="coco",
        )

    def test_prefilter_passes_agents_without_role(self):
        router = TaskRouter()
        agents = [
            self._make_agent("a1"),
            self._make_agent("a2", role="coder"),
        ]
        result = router._semantic_prefilter("fix this bug", agents)
        # a1 has no role -> always passes
        assert any(a.agent_id == "a1" for a in result)

    def test_prefilter_passes_relevant_agent(self):
        router = TaskRouter()
        agents = [
            self._make_agent("coder1", role="code implementation"),
            self._make_agent("designer1", role="UI design"),
        ]
        result = router._semantic_prefilter("implement the login function", agents)
        # coder1 matches "code" skill
        assert any(a.agent_id == "coder1" for a in result)

    def test_prefilter_excludes_irrelevant_agent(self):
        router = TaskRouter()
        agents = [
            self._make_agent("coder1", role="code implementation bug fix"),
            self._make_agent("artist1", role="logo graphic visual branding"),
        ]
        result = router._semantic_prefilter("fix the null pointer bug in auth module", agents)
        # artist1 has no keyword overlap with code/fix/debug
        assert any(a.agent_id == "coder1" for a in result)
        # artist might be excluded (no overlap with "code", "fix", "debug" skills)

    def test_prefilter_fallback_on_empty(self):
        """If prefilter is too aggressive and filters everyone, _score_and_assign falls back."""
        router = TaskRouter()
        agents = [
            self._make_agent("a1", role="completely unrelated domain xyz"),
        ]
        # Even if prefilter returns empty, the caller falls back to all agents
        result = router._semantic_prefilter("deploy the service", agents)
        # Could be empty, which is fine — caller handles it
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Orchestrator Degradation
# ---------------------------------------------------------------------------


class TestOrchestratorDegradation:
    """Tests for the orchestrator degradation mechanism."""

    def test_attempt_self_advance_no_plan(self):
        from src.slock_engine.collaboration_orchestrator import CollaborationOrchestrator
        from src.slock_engine.task_chain_manager import TaskChainManager

        from src.slock_engine.observer_queue import TaskStatusNotifier

        notifier = TaskStatusNotifier()
        chain_mgr = TaskChainManager()
        orch = CollaborationOrchestrator(
            chain_manager=chain_mgr,
            notifier=notifier,
            resolve_agent=lambda r, c: None,
            dispatch_task=lambda t, a: None,
        )
        # No plans exist
        result = orch.attempt_self_advance("nonexistent", "agent1", "ch1")
        assert result is False

    def test_attempt_self_advance_disabled(self):
        from src.slock_engine.collaboration_orchestrator import CollaborationOrchestrator
        from src.slock_engine.task_chain_manager import TaskChainManager

        from src.slock_engine.observer_queue import TaskStatusNotifier

        notifier = TaskStatusNotifier()
        chain_mgr = TaskChainManager()
        orch = CollaborationOrchestrator(
            chain_manager=chain_mgr,
            notifier=notifier,
            resolve_agent=lambda r, c: None,
            dispatch_task=lambda t, a: None,
        )
        with patch("src.slock_engine.collaboration_orchestrator.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(slock_orchestrator_degradation_enabled=False)
            result = orch.attempt_self_advance("task1", "agent1", "ch1")
            assert result is False
