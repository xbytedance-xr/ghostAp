"""Unit tests for slock_engine/memory_manager.py — three-layer memory system."""

from __future__ import annotations

import os

from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import SlockMemory


class TestMemoryManagerL1:
    """L1: Agent private memory."""

    def test_read_nonexistent_returns_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        result = mm.read_agent_memory("agent-unknown")
        assert result.role == ""
        assert result.key_knowledge == ""
        assert result.active_context == ""

    def test_write_and_read_round_trip(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mem = SlockMemory(role="Coder", key_knowledge="Python", active_context="Building API")
        mm.write_agent_memory("a1", mem)
        restored = mm.read_agent_memory("a1")
        assert restored.role == "Coder"
        assert restored.key_knowledge == "Python"
        assert restored.active_context == "Building API"

    def test_update_agent_context_appends(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mem = SlockMemory(active_context="First entry")
        mm.write_agent_memory("a1", mem)
        mm.update_agent_context("a1", "Second entry")
        restored = mm.read_agent_memory("a1")
        assert "First entry" in restored.active_context
        assert "Second entry" in restored.active_context

    def test_update_agent_context_on_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.update_agent_context("a2", "New context")
        restored = mm.read_agent_memory("a2")
        assert restored.active_context == "New context"

    def test_agent_memory_path(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        path = mm._agent_memory_path("agent-42")
        assert path.endswith(os.path.join("agents", "agent-42", "MEMORY.md"))

    def test_initialize_agent_workspace_creates_notes_and_task_dirs(self, tmp_path):
        """Each Agent gets the workspace/notes structure described in the Slock spec."""
        mm = MemoryManager(base_path=str(tmp_path))

        paths = mm.initialize_agent_workspace("agent-42")

        assert paths["memory_path"].endswith(os.path.join("agents", "agent-42", "MEMORY.md"))
        assert paths["notes_path"].endswith(os.path.join("agents", "agent-42", "NOTES.md"))
        assert paths["workspace_path"].endswith(os.path.join("agents", "agent-42", "workspace"))
        assert os.path.isfile(paths["notes_path"])
        assert os.path.isdir(os.path.join(paths["workspace_path"], "current-task"))
        assert os.path.isdir(os.path.join(paths["workspace_path"], "history"))

    def test_write_and_read_agent_reasoning_snapshot(self, tmp_path):
        """Agent reply cards can show a persisted execution summary instead of a placeholder."""
        mm = MemoryManager(base_path=str(tmp_path))

        snapshot_path = mm.write_agent_reasoning_snapshot(
            "agent-42",
            "task-1",
            prompt_summary="Review login bug",
            result_summary="Found missing test",
            tool_name="codex",
            model_name="gpt-5",
        )
        restored = mm.read_agent_reasoning_snapshot("agent-42", "task-1")

        assert snapshot_path.endswith(os.path.join("agents", "agent-42", "reasoning", "task-1.json"))
        assert restored["prompt_summary"] == "Review login bug"
        assert restored["result_summary"] == "Found missing test"
        assert restored["tool_name"] == "codex"


class TestMemoryManagerL2:
    """L2: Group shared memory."""

    def test_read_nonexistent_returns_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        assert mm.read_group_memory("ch-unknown") == ""

    def test_write_and_read(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("ch1", "Shared knowledge base")
        assert mm.read_group_memory("ch1") == "Shared knowledge base"

    def test_append_group_memory(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("ch1", "Line 1")
        mm.append_group_memory("ch1", "Line 2")
        content = mm.read_group_memory("ch1")
        assert "Line 1" in content
        assert "Line 2" in content

    def test_append_to_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.append_group_memory("ch2", "First entry")
        assert mm.read_group_memory("ch2") == "First entry"


class TestMemoryManagerL3:
    """L3: Global knowledge base."""

    def test_read_nonexistent_returns_empty(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        assert mm.read_global_wiki() == ""

    def test_write_and_read(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_global_wiki("Global knowledge")
        assert mm.read_global_wiki() == "Global knowledge"

    def test_append_global_wiki(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_global_wiki("Entry A")
        mm.append_global_wiki("Entry B")
        content = mm.read_global_wiki()
        assert "Entry A" in content
        assert "Entry B" in content


class TestMemoryManagerIsolation:
    """Verify layer isolation — groups don't leak into each other."""

    def test_different_groups_isolated(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("g1", "Group 1 data")
        mm.write_group_memory("g2", "Group 2 data")
        assert mm.read_group_memory("g1") == "Group 1 data"
        assert mm.read_group_memory("g2") == "Group 2 data"

    def test_ensure_directories_creates_structure(self, tmp_path):
        mm = MemoryManager(base_path=str(tmp_path))
        mm.ensure_directories(agent_id="a1", channel_id="ch1")
        assert os.path.isdir(os.path.join(str(tmp_path), "agents", "a1"))
        assert os.path.isdir(os.path.join(str(tmp_path), "groups", "ch1", "tasks"))
        assert os.path.isdir(os.path.join(str(tmp_path), "global"))

    def test_cross_chat_id_access_returns_empty(self, tmp_path):
        """AC-10: L2 shared memory of chat_id_A is not accessible via chat_id_B."""
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("chat_id_A", "Secret data for group A")
        result_b = mm.read_group_memory("chat_id_B")
        assert result_b == ""
        assert mm.read_group_memory("chat_id_A") == "Secret data for group A"

    def test_cross_chat_append_does_not_leak(self, tmp_path):
        """AC-10: Appending to group B does not affect group A."""
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_group_memory("group_alpha", "Alpha original")
        mm.append_group_memory("group_beta", "Beta new entry")
        assert mm.read_group_memory("group_alpha") == "Alpha original"
        assert mm.read_group_memory("group_beta") == "Beta new entry"

    def test_many_groups_no_cross_contamination(self, tmp_path):
        """AC-10: 10 groups each with unique data — no cross-contamination."""
        mm = MemoryManager(base_path=str(tmp_path))
        for i in range(10):
            mm.write_group_memory(f"chat_{i}", f"data_for_group_{i}")

        for i in range(10):
            content = mm.read_group_memory(f"chat_{i}")
            assert content == f"data_for_group_{i}"
            for j in range(10):
                if j != i:
                    assert f"data_for_group_{j}" not in content

    def test_agent_memory_isolated_from_group_memory(self, tmp_path):
        """Agent L1 memory and group L2 memory are fully separate namespaces."""
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_agent_memory("agent_x", SlockMemory(role="Coder", key_knowledge="Python", active_context="task"))
        mm.write_group_memory("agent_x", "This is group data")
        agent_mem = mm.read_agent_memory("agent_x")
        assert agent_mem.role == "Coder"
        assert mm.read_group_memory("agent_x") == "This is group data"


class TestL1CrossGroupPersistence:
    """AC-13: Agent L1 memory persists across groups — same agent_id in different channels."""

    def test_same_agent_different_channels_same_memory(self, tmp_path):
        """An agent's L1 memory is keyed by agent_id, not channel_id."""
        mm = MemoryManager(base_path=str(tmp_path))
        mem = SlockMemory(role="Architect", key_knowledge="System design", active_context="Planning v2")
        mm.write_agent_memory("agent-uuid-001", mem)

        # Simulate the agent being added to a different group —
        # L1 memory is accessed via agent_id regardless of channel context
        restored = mm.read_agent_memory("agent-uuid-001")
        assert restored.role == "Architect"
        assert restored.key_knowledge == "System design"
        assert restored.active_context == "Planning v2"

    def test_agent_memory_independent_of_group_memory(self, tmp_path):
        """Writing group memory for channel_B does not affect agent L1 memory."""
        mm = MemoryManager(base_path=str(tmp_path))
        mem = SlockMemory(role="Reviewer", key_knowledge="Code quality", active_context="Reviewing PR")
        mm.write_agent_memory("agent-uuid-002", mem)

        # Write group memory for two different channels
        mm.write_group_memory("channel_A", "Channel A context")
        mm.write_group_memory("channel_B", "Channel B context")

        # Agent memory remains unchanged regardless of group operations
        restored = mm.read_agent_memory("agent-uuid-002")
        assert restored.role == "Reviewer"
        assert restored.key_knowledge == "Code quality"

    def test_context_update_persists_across_simulated_group_switch(self, tmp_path):
        """Agent updates context in group A; memory is still accessible in group B."""
        mm = MemoryManager(base_path=str(tmp_path))
        mem = SlockMemory(role="Coder", key_knowledge="Python", active_context="Started in group A")
        mm.write_agent_memory("agent-uuid-003", mem)

        # Simulate working in group A: update context
        mm.update_agent_context("agent-uuid-003", "Completed task in group A")

        # Simulate moving to group B: read memory (same agent_id)
        restored = mm.read_agent_memory("agent-uuid-003")
        assert "Completed task in group A" in restored.active_context
        assert restored.role == "Coder"

    def test_two_agents_same_group_independent_l1(self, tmp_path):
        """Two agents in the same group maintain independent L1 memories."""
        mm = MemoryManager(base_path=str(tmp_path))
        mm.write_agent_memory("agent-A", SlockMemory(role="Coder", key_knowledge="Go", active_context=""))
        mm.write_agent_memory("agent-B", SlockMemory(role="Tester", key_knowledge="Pytest", active_context=""))

        assert mm.read_agent_memory("agent-A").role == "Coder"
        assert mm.read_agent_memory("agent-B").role == "Tester"

        # Update one doesn't affect the other
        mm.update_agent_context("agent-A", "Working on feature X")
        assert mm.read_agent_memory("agent-B").active_context == ""


class TestL1SkillProfileCrossGroup:
    """AC-13: Agent L1 skill profiles persist correctly when agent joins a new channel."""

    def test_agent_memory_survives_channel_change(self, tmp_path):
        """Agent registered in channel A, then registered in channel B — L1 memory persists."""
        mm = MemoryManager(base_path=str(tmp_path))

        # Agent created and works in channel A
        mem = SlockMemory(
            role="Senior Coder with 5 years experience",
            key_knowledge="Python, asyncio, FastAPI",
            active_context="Working on auth module in channel A",
        )
        mm.write_agent_memory("agent-cross-001", mem)
        mm.update_agent_context("agent-cross-001", "Completed PR #42 review")

        # Simulate agent joining channel B (different group) — L1 should persist
        # because it's keyed by agent_id, not channel_id
        restored = mm.read_agent_memory("agent-cross-001")
        assert restored.role == "Senior Coder with 5 years experience"
        assert restored.key_knowledge == "Python, asyncio, FastAPI"
        assert "Working on auth module in channel A" in restored.active_context
        assert "Completed PR #42 review" in restored.active_context

    def test_skill_profiles_persist_across_groups(self, tmp_path):
        """Agent skill profiles persist when agent is referenced from a different group."""
        from src.slock_engine.models import SkillProfile

        mm = MemoryManager(base_path=str(tmp_path))

        # Build skill profile in channel A
        profiles = [
            SkillProfile(tag="code", success_rate=85.0, total_tasks=10, last_active=1700000000.0),
            SkillProfile(tag="review", success_rate=92.0, total_tasks=5, last_active=1700000100.0),
        ]
        mm.write_skill_profiles("agent-cross-002", profiles)

        # Read from different context (simulating channel B) — same agent_id
        restored = mm.read_skill_profiles("agent-cross-002")
        assert len(restored) == 2
        assert restored[0].tag == "code"
        assert restored[0].success_rate == 85.0
        assert restored[1].tag == "review"
        assert restored[1].total_tasks == 5


class TestL1MemoryCrossGroupPersonalityConsistency:
    """AC-11: Agent L1 memory preserves personality (role + key_knowledge) across group moves.

    Validates: "Agent 的 L1 记忆在跨群移动后仍可正确加载，人格一致性保持"

    The core invariant: L1 memory is keyed by agent UUID, NOT by group/channel.
    An agent that writes its personality in group A must read back the exact same
    personality when operating in group B.
    """

    def test_role_persists_across_group_change(self, tmp_path):
        """Agent writes role in group A context, reads identical role in group B context."""
        mm = MemoryManager(base_path=str(tmp_path))
        agent_uuid = "agent-personality-uuid-001"

        # --- Phase 1: Agent operating in Group A writes its personality ---
        mm.ensure_directories(agent_id=agent_uuid, channel_id="group_A")
        personality = SlockMemory(
            role="You are a strict code reviewer who never approves without tests.",
            key_knowledge="TDD methodology, pytest fixtures, coverage thresholds",
            active_context="Currently reviewing PR in group A",
        )
        mm.write_agent_memory(agent_uuid, personality)

        # --- Phase 2: Agent moves to Group B — read memory with group B context ---
        mm.ensure_directories(agent_id=agent_uuid, channel_id="group_B")
        restored = mm.read_agent_memory(agent_uuid)

        # The role must be EXACTLY the same — byte-for-byte personality consistency
        assert restored.role == "You are a strict code reviewer who never approves without tests."

    def test_key_knowledge_persists_across_group_change(self, tmp_path):
        """Agent writes key_knowledge in group A, reads identical key_knowledge in group B."""
        mm = MemoryManager(base_path=str(tmp_path))
        agent_uuid = "agent-personality-uuid-002"

        # --- Phase 1: Agent acquires knowledge while in Group A ---
        mm.ensure_directories(agent_id=agent_uuid, channel_id="group_A")
        personality = SlockMemory(
            role="Security auditor",
            key_knowledge="OWASP Top 10, SQL injection patterns, XSS prevention, CSP headers",
            active_context="Auditing auth module in group A",
        )
        mm.write_agent_memory(agent_uuid, personality)

        # --- Phase 2: Agent is now in Group B — key_knowledge must be intact ---
        mm.ensure_directories(agent_id=agent_uuid, channel_id="group_B")
        restored = mm.read_agent_memory(agent_uuid)

        # key_knowledge must be EXACTLY preserved — this IS the agent's personality
        assert restored.key_knowledge == "OWASP Top 10, SQL injection patterns, XSS prevention, CSP headers"

    def test_full_personality_identical_across_multiple_groups(self, tmp_path):
        """Agent personality (role + key_knowledge) is identical across 5 different groups."""
        mm = MemoryManager(base_path=str(tmp_path))
        agent_uuid = "agent-personality-uuid-003"

        # --- Write personality once in the original group ---
        original_role = "You are a friendly teaching assistant who explains concepts with analogies."
        original_knowledge = "Pedagogy, Socratic method, spaced repetition, active recall"

        mm.ensure_directories(agent_id=agent_uuid, channel_id="origin_group")
        mm.write_agent_memory(agent_uuid, SlockMemory(
            role=original_role,
            key_knowledge=original_knowledge,
            active_context="Teaching Python basics in origin_group",
        ))

        # --- Verify personality is identical when read from 5 different group contexts ---
        for group_id in ["group_X", "group_Y", "group_Z", "group_W", "group_V"]:
            mm.ensure_directories(agent_id=agent_uuid, channel_id=group_id)
            restored = mm.read_agent_memory(agent_uuid)
            assert restored.role == original_role, (
                f"Role changed when reading from {group_id}!"
            )
            assert restored.key_knowledge == original_knowledge, (
                f"key_knowledge changed when reading from {group_id}!"
            )

    def test_group_b_operations_do_not_corrupt_personality(self, tmp_path):
        """Writing L2 group memory in group B does not corrupt agent L1 personality."""
        mm = MemoryManager(base_path=str(tmp_path))
        agent_uuid = "agent-personality-uuid-004"

        # --- Agent personality established in group A ---
        mm.ensure_directories(agent_id=agent_uuid, channel_id="group_A")
        mm.write_agent_memory(agent_uuid, SlockMemory(
            role="DevOps engineer specializing in Kubernetes",
            key_knowledge="Helm charts, ArgoCD, GitOps workflows, pod autoscaling",
            active_context="Deploying service mesh in group A",
        ))

        # --- Heavy L2 operations in group B (should NOT affect L1) ---
        mm.ensure_directories(agent_id=agent_uuid, channel_id="group_B")
        mm.write_group_memory("group_B", "Group B shared context: discussing frontend")
        mm.append_group_memory("group_B", "New topic: React hooks best practices")

        # --- Verify L1 personality is completely untouched ---
        restored = mm.read_agent_memory(agent_uuid)
        assert restored.role == "DevOps engineer specializing in Kubernetes"
        assert restored.key_knowledge == "Helm charts, ArgoCD, GitOps workflows, pod autoscaling"
        # L2 group data must NOT leak into L1
        assert "frontend" not in restored.role
        assert "React" not in restored.key_knowledge
