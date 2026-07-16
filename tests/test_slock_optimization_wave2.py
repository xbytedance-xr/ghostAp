"""Unit tests for Slock Engine optimization wave 2 (Tasks 33-37).

Covers:
- Task 33: Discussion cooldown and depth limit
- Task 35: UX card templates (command panel, error suggestion, confirm/cancel,
           council detail, status refresh, crash recovery)
- Task 36: Three-level lock system in MemoryManager
- Task 37: AgentRegistry enhancements (duplicate detection, lazy load, cleanup)
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from src.slock_engine.agent_registry import AgentRegistry, DuplicateAgentNameError
from src.slock_engine.card_templates import (
    build_command_panel_card,
    build_confirm_cancel_card,
    build_council_detail_card,
    build_crash_recovery_card,
    build_error_suggestion_card,
    build_status_refresh_card,
)
from src.slock_engine.discussion_manager import DiscussionManager
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import (
    AgentIdentity,
    DiscussionConfig,
    SlockMemory,
    SlockTask,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Task 33: Discussion Cooldown & Depth Limit
# ---------------------------------------------------------------------------


class TestDiscussionCooldownDepth:
    """Tests for cooldown and depth limit in DiscussionManager."""

    def _make_dm(self) -> DiscussionManager:
        return DiscussionManager(engine=None, memory_manager=None, config=DiscussionConfig())

    def test_not_on_cooldown_initially(self):
        dm = self._make_dm()
        assert dm._is_on_cooldown("agent_x") is False

    def test_on_cooldown_after_participation(self):
        dm = self._make_dm()
        dm._record_discussion_participation("agent_x")
        assert dm._is_on_cooldown("agent_x") is True

    def test_cooldown_expires(self):
        dm = self._make_dm()
        # Simulate participation 61 seconds ago (cooldown is 60s)
        dm._last_discussion_time["agent_x"] = time.time() - 61
        assert dm._is_on_cooldown("agent_x") is False

    def test_depth_limit_none_parent_always_ok(self):
        dm = self._make_dm()
        assert dm._check_depth_limit(None) is True

    def test_depth_limit_under_max(self):
        dm = self._make_dm()
        dm._discussion_depth["thread_a"] = 2
        assert dm._check_depth_limit("thread_a") is True

    def test_depth_limit_at_max(self):
        dm = self._make_dm()
        dm._discussion_depth["thread_a"] = 3
        assert dm._check_depth_limit("thread_a") is False

    def test_increment_depth_from_zero(self):
        dm = self._make_dm()
        dm._increment_depth("thread_new", None)
        assert dm._discussion_depth["thread_new"] == 1

    def test_increment_depth_inherits_parent(self):
        dm = self._make_dm()
        dm._discussion_depth["parent_thread"] = 2
        dm._increment_depth("child_thread", "parent_thread")
        assert dm._discussion_depth["child_thread"] == 3

    def test_should_trigger_suppressed_on_cooldown(self):
        dm = self._make_dm()
        agent = AgentIdentity(agent_id="a1", name="Coder", role="coder", owner_group="g1")
        # Put agent on cooldown
        dm._record_discussion_participation("a1")
        result = dm.should_trigger_discussion(agent, "some content needs review")
        assert result is None

    def test_bind_to_task(self):
        dm = self._make_dm()
        dm.bind_to_task("thread_1", "task_abc")
        assert dm.get_bound_task("thread_1") == "task_abc"

    def test_get_bound_task_unbound(self):
        dm = self._make_dm()
        assert dm.get_bound_task("nonexistent") is None

    def test_unbind_task(self):
        dm = self._make_dm()
        dm.bind_to_task("thread_1", "task_abc")
        dm.unbind_task("thread_1")
        assert dm.get_bound_task("thread_1") is None



# ---------------------------------------------------------------------------
# Task 35: UX Card Templates
# ---------------------------------------------------------------------------


class TestUXCards:
    """Tests for new card templates in card_templates.py."""

    def test_command_panel_card_structure(self):
        card = build_command_panel_card()
        assert card["schema"] == "2.0"
        assert "header" in card
        assert "body" in card
        elements = card["body"]["elements"]
        assert len(elements) > 0
        # Verify expected commands are mentioned in the card body
        all_content = json.dumps(card)
        assert "/team" in all_content
        assert "/role" in all_content

    def test_error_suggestion_card_shows_input(self):
        card = build_error_suggestion_card(
            user_input="/foob",
            suggestions=["/foo", "/bar"],
        )
        all_content = json.dumps(card)
        assert "/foob" in all_content

    def test_error_suggestion_card_truncates_long_input(self):
        long_input = "x" * 100
        card = build_error_suggestion_card(
            user_input=long_input,
            suggestions=["suggestion1"],
        )
        all_content = json.dumps(card)
        # The input should be truncated to 50 chars + "..."
        assert "..." in all_content
        # Full 100-char input should NOT appear
        assert long_input not in all_content

    def test_confirm_cancel_card_has_buttons(self):
        card = build_confirm_cancel_card(
            title="Confirm Action",
            description="Are you sure?",
        )
        all_content = json.dumps(card)
        # Should contain confirm and cancel actions
        assert "slock_confirm" in all_content
        assert "slock_cancel" in all_content

    def test_council_detail_card_shows_opinions(self):
        opinions = [
            {
                "agent_name": "Coder",
                "emoji": "\U0001f527",
                "role": "coder",
                "opinion_text": "I think we should refactor first.",
            },
            {
                "agent_name": "Reviewer",
                "emoji": "\U0001f50d",
                "role": "reviewer",
                "opinion_text": "Tests need to pass before merge.",
            },
        ]
        card = build_council_detail_card(
            topic="Should we refactor?",
            opinions=opinions,
        )
        all_content = json.dumps(card)
        assert "Coder" in all_content
        assert "Reviewer" in all_content
        assert "refactor first" in all_content
        assert "Tests need to pass" in all_content

    def test_status_refresh_card_shows_agents(self):
        agents = [
            {"name": "Alice", "emoji": "\U0001f916", "status": "idle", "role": "coder"},
            {"name": "Bob", "emoji": "\U0001f50d", "status": "running", "role": "reviewer"},
        ]
        tasks_summary = {"total": 5, "todo": 2, "in_progress": 2, "done": 1}
        card = build_status_refresh_card(agents=agents, tasks_summary=tasks_summary)
        all_content = json.dumps(card)
        assert "Alice" in all_content
        assert "Bob" in all_content
        assert card["schema"] == "2.0"

    def test_crash_recovery_card_shows_tasks(self):
        tasks = [
            SlockTask(
                task_id="abc12345-long-id",
                content="Fix bug in auth",
                status=TaskStatus.TODO,
            )
        ]
        card = build_crash_recovery_card(recovered_tasks=tasks)
        all_content = json.dumps(card)
        assert "abc12345" in all_content
        assert "Fix bug in auth" in all_content
        assert card["schema"] == "2.0"

    def test_command_panel_card_has_council_command(self):
        card = build_command_panel_card()
        all_content = json.dumps(card)
        assert "/council" in all_content


# ---------------------------------------------------------------------------
# Task 36: Three-Level Locks in MemoryManager
# ---------------------------------------------------------------------------


class TestThreeLevelLocks:
    """Tests for the three-level lock system in MemoryManager."""

    def test_agent_lock_same_id_returns_same_lock(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        lock1 = mm._get_agent_lock("agent_1")
        lock2 = mm._get_agent_lock("agent_1")
        assert lock1 is lock2

    def test_agent_lock_different_id_returns_different_locks(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        lock1 = mm._get_agent_lock("agent_1")
        lock2 = mm._get_agent_lock("agent_2")
        assert lock1 is not lock2

    def test_channel_lock_same_id_returns_same_lock(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        lock1 = mm._get_channel_lock("ch_1")
        lock2 = mm._get_channel_lock("ch_1")
        assert lock1 is lock2

    def test_channel_lock_creation_thread_safe(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        results: list[threading.Lock] = []
        barrier = threading.Barrier(10)

        def get_lock():
            barrier.wait()
            lock = mm._get_channel_lock("shared_channel")
            results.append(lock)

        threads = [threading.Thread(target=get_lock) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All 10 threads should have received the same Lock instance
        assert len(results) == 10
        assert all(r is results[0] for r in results)

    def test_different_agents_can_write_concurrently(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        # Initialize agent workspaces
        mm.initialize_agent_workspace("agent_a")
        mm.initialize_agent_workspace("agent_b")

        success = {"agent_a": False, "agent_b": False}
        barrier = threading.Barrier(2)

        def write_memory(agent_id: str):
            barrier.wait()
            memory = SlockMemory(
                role="test",
                key_knowledge="knowledge",
                active_context=f"context for {agent_id}",
            )
            mm.write_agent_memory(agent_id, memory)
            success[agent_id] = True

        t1 = threading.Thread(target=write_memory, args=("agent_a",))
        t2 = threading.Thread(target=write_memory, args=("agent_b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert success["agent_a"] is True
        assert success["agent_b"] is True

        # Verify both memories were written correctly
        mem_a = mm.read_agent_memory("agent_a")
        mem_b = mm.read_agent_memory("agent_b")
        assert "agent_a" in mem_a.active_context
        assert "agent_b" in mem_b.active_context


# ---------------------------------------------------------------------------
# Task 37: AgentRegistry Enhancements
# ---------------------------------------------------------------------------


class TestAgentRegistryEnhancements:
    """Tests for new registry features: duplicate detection, lazy load, cleanup."""

    def _make_agent(self, **kwargs) -> AgentIdentity:
        defaults = {"agent_id": "a1", "name": "Alice", "owner_group": "g1"}
        defaults.update(kwargs)
        return AgentIdentity(**defaults)

    def test_duplicate_name_same_channel_raises(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Alice", owner_group="g1"))
        # Wait for background persist to complete
        if reg._persist_thread:
            reg._persist_thread.join(timeout=2)

        with pytest.raises(DuplicateAgentNameError):
            reg.register(self._make_agent(agent_id="a2", name="Alice", owner_group="g1"))

    def test_duplicate_name_different_channel_ok(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Alice", owner_group="g1"))
        if reg._persist_thread:
            reg._persist_thread.join(timeout=2)

        # Different channel should be fine
        agent = reg.register(self._make_agent(agent_id="a2", name="Alice", owner_group="g2"))
        assert agent.agent_id == "a2"

    def test_duplicate_name_case_insensitive(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Alice", owner_group="g1"))
        if reg._persist_thread:
            reg._persist_thread.join(timeout=2)

        with pytest.raises(DuplicateAgentNameError):
            reg.register(self._make_agent(agent_id="a3", name="alice", owner_group="g1"))

    def test_register_rejects_casefold_collision_in_incoming_member_group(self, tmp_path):
        """Every incoming membership channel participates in name admission."""
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Straße", owner_group="g1"))

        with pytest.raises(DuplicateAgentNameError, match="g1"):
            reg.register(
                self._make_agent(
                    agent_id="a2",
                    name="STRASSE",
                    owner_group="g2",
                    member_groups=["g2", "g1"],
                )
            )

    def test_update_rejects_casefold_name_collision(self, tmp_path):
        """Updating an identity cannot bypass per-channel name uniqueness."""
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Maße", owner_group="g1"))
        reg.register(self._make_agent(agent_id="a2", name="Bob", owner_group="g1"))

        with pytest.raises(DuplicateAgentNameError, match="g1"):
            reg.update(self._make_agent(agent_id="a2", name="MASSE", owner_group="g1"))

    def test_unscoped_ambiguous_name_lookup_fails_closed(self, tmp_path):
        """Allowed cross-channel duplicates cannot resolve by first-match globally."""
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        reg.register(self._make_agent(agent_id="a1", name="Alice", owner_group="g1"))
        reg.register(self._make_agent(agent_id="a2", name="alice", owner_group="g2"))

        with pytest.raises(LookupError, match="ambiguous"):
            reg.find_by_name("ALICE")

    def test_lazy_load_from_disk(self, tmp_path):
        # Register agent with first registry instance
        reg1 = AgentRegistry.legacy(base_path=str(tmp_path))
        agent = self._make_agent(agent_id="lazy_agent", name="LazyBob", owner_group="g1")
        reg1.register(agent)
        # Ensure background persist completes
        if reg1._persist_thread:
            reg1._persist_thread.join(timeout=2)

        # Create a new fresh registry pointing to the same path
        reg2 = AgentRegistry.legacy(base_path=str(tmp_path))
        # The new registry has NOT loaded all agents yet (_loaded = False)
        # Calling get should trigger on-demand single load
        found = reg2.get("lazy_agent")
        assert found is not None
        assert found.name == "LazyBob"

    def test_remove_cleans_directory(self, tmp_path):
        reg = AgentRegistry.legacy(base_path=str(tmp_path))
        agent = self._make_agent(agent_id="cleanup_agent", name="Cleanup", owner_group="g1")
        reg.register(agent)
        # Ensure background persist completes
        if reg._persist_thread:
            reg._persist_thread.join(timeout=2)

        # Verify the identity file exists
        import os
        identity_file = os.path.join(str(tmp_path), "agents", "cleanup_agent", "identity.json")
        assert os.path.exists(identity_file)

        # Remove the agent
        result = reg.remove("cleanup_agent")
        assert result is True

        # Verify directory is cleaned up (removed because it's empty)
        agent_dir = os.path.join(str(tmp_path), "agents", "cleanup_agent")
        assert not os.path.exists(agent_dir)
