"""AC11: L1 memory cross-group persistence tests.

Validates: Agent's L1 private memory (MEMORY.md) is keyed by agent UUID,
not chat_id. When an agent's member_groups changes (simulating cross-group
movement), memory loads correctly and personality consistency is maintained.
"""

from __future__ import annotations

import threading

import pytest

from src.slock_engine.agent_registry import AgentRegistry
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import AgentIdentity, SlockMemory


@pytest.fixture
def memory_mgr(tmp_path):
    """Create a MemoryManager with isolated storage."""
    return MemoryManager(base_path=str(tmp_path / "slock_storage"))


def _make_agent(agent_id: str = "agent-001", owner_group: str = "group-A") -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        name="CrossGroupAgent",
        emoji="🧭",
        agent_type="coco",
        model_name="test-model",
        role="coder",
        owner_group=owner_group,
        member_groups=[owner_group],
    )


class TestL1MemoryCrossGroupPersistence:
    """L1 memory follows agent UUID across groups."""

    def test_memory_persists_after_group_change(self, memory_mgr):
        """Write memory in group-A, change member_groups to group-B, read still works."""
        agent = _make_agent("agent-cross-001", owner_group="group-A")

        # Write memory as if in group-A
        original_memory = SlockMemory(
            role="I am a Python backend developer.",
            key_knowledge="Project uses FastAPI + PostgreSQL",
            active_context="Currently working on PR #42",
        )
        memory_mgr.write_agent_memory(agent.agent_id, original_memory)

        # Simulate cross-group move: add group-B to member_groups
        agent.member_groups.append("group-B")

        # Read from "group-B context" — same agent_id, memory should be identical
        loaded = memory_mgr.read_agent_memory(agent.agent_id)
        assert loaded.role == original_memory.role
        assert loaded.key_knowledge == original_memory.key_knowledge
        assert loaded.active_context == original_memory.active_context

    def test_memory_path_is_agent_id_based(self, memory_mgr):
        """Memory path uses agent_id, not channel_id."""
        agent = _make_agent("agent-path-test")
        path = memory_mgr.agent_memory_path(agent.agent_id)
        assert "agent-path-test" in path
        assert "group-A" not in path  # Not group-scoped

    def test_different_agents_different_memories(self, memory_mgr):
        """Two agents in same group have separate L1 memories."""
        mem_a = SlockMemory(role="Agent A personality")
        mem_b = SlockMemory(role="Agent B personality")

        memory_mgr.write_agent_memory("agent-A", mem_a)
        memory_mgr.write_agent_memory("agent-B", mem_b)

        loaded_a = memory_mgr.read_agent_memory("agent-A")
        loaded_b = memory_mgr.read_agent_memory("agent-B")

        assert loaded_a.role == "Agent A personality"
        assert loaded_b.role == "Agent B personality"

    def test_memory_survives_owner_group_change(self, memory_mgr):
        """If agent owner_group changes, L1 memory remains accessible."""
        agent = _make_agent("agent-owner-change", owner_group="old-group")
        memory = SlockMemory(
            role="Consistent personality",
            key_knowledge="Domain expertise",
        )
        memory_mgr.write_agent_memory(agent.agent_id, memory)

        # Simulate owner change
        agent_new = AgentIdentity(
            agent_id=agent.agent_id,
            name=agent.name,
            owner_group="new-group",
            member_groups=["new-group"],
        )

        # Memory is still accessible by same agent_id
        loaded = memory_mgr.read_agent_memory(agent_new.agent_id)
        assert loaded.role == "Consistent personality"
        assert loaded.key_knowledge == "Domain expertise"

    def test_context_updates_persist_across_groups(self, memory_mgr):
        """Context updates from group-A are visible when accessed from group-B context."""
        agent_id = "agent-context-cross"
        initial = SlockMemory(role="Code reviewer")
        memory_mgr.write_agent_memory(agent_id, initial)

        # Update context (as if working in group-A)
        memory_mgr.update_agent_context(agent_id, "[2026-05-19] Reviewed PR #100")

        # Read from different group context — update persists
        loaded = memory_mgr.read_agent_memory(agent_id)
        assert "Reviewed PR #100" in loaded.active_context


class TestL1PersonalityConsistency:
    """Agent personality (role section) remains consistent across groups."""

    def test_role_identity_unchanged_after_multiple_context_updates(self, memory_mgr):
        """Role section stays stable even after many context updates."""
        agent_id = "agent-personality-stable"
        original = SlockMemory(
            role="I am Architect-Prime. I design system architectures.",
            key_knowledge="Microservices, DDD, Event Sourcing",
        )
        memory_mgr.write_agent_memory(agent_id, original)

        # Simulate multiple context updates
        for i in range(5):
            memory_mgr.update_agent_context(agent_id, f"[Update {i}] Task completed")

        loaded = memory_mgr.read_agent_memory(agent_id)
        assert loaded.role == original.role
        assert loaded.key_knowledge == original.key_knowledge

    def test_skill_profiles_persist_cross_group(self, memory_mgr):
        """Skill profiles follow agent UUID, not group."""
        agent_id = "agent-skills-cross"
        memory_mgr.record_skill_feedback(agent_id, ["python", "testing"], quality_score=90.0)

        # Read from any context — same agent_id
        profiles = memory_mgr.read_skill_profiles(agent_id)
        tags = {p.tag for p in profiles}
        assert "python" in tags
        assert "testing" in tags

    def test_skill_profile_path_is_agent_scoped(self, memory_mgr):
        """Skill profile path uses agent_id, not channel."""
        path = memory_mgr.skill_profile_path("agent-xyz")
        assert "agent-xyz" in path
        assert "groups" not in path


class TestL2GroupIsolation:
    """L2 group memory does NOT cross group boundaries (inverse verification)."""

    def test_group_memory_isolated_between_groups(self, memory_mgr):
        """Group A's shared memory is not visible in Group B."""
        memory_mgr.write_group_memory("group-A", "Group A secrets")
        memory_mgr.write_group_memory("group-B", "Group B secrets")

        assert memory_mgr.read_group_memory("group-A") == "Group A secrets"
        assert memory_mgr.read_group_memory("group-B") == "Group B secrets"

    def test_l1_and_l2_paths_dont_overlap(self, memory_mgr):
        """L1 agent path and L2 group path are in separate directories."""
        agent_path = memory_mgr.agent_memory_path("agent-001")
        group_path = memory_mgr.group_memory_path("group-001")

        assert "agents" in agent_path
        assert "groups" in group_path
        assert "agents" not in group_path
        assert "groups" not in agent_path


# ==================================================================
# Task 2: Integration test — AgentRegistry + MemoryManager cross-group
# ==================================================================


class TestRegistryCrossGroupIntegration:
    """Integration: AgentRegistry.register() to new group preserves L1 memory."""

    @pytest.fixture
    def storage(self, tmp_path):
        base = str(tmp_path / "slock_int")
        return {
            "registry": AgentRegistry(base_path=base),
            "memory": MemoryManager(base_path=base),
        }

    def test_register_to_new_group_preserves_memory(self, storage):
        """Register agent in group-A, write memory, re-register in group-B, memory unchanged."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = AgentIdentity(
            agent_id="int-agent-001",
            name="IntegrationBot",
            emoji="🔗",
            agent_type="coco",
            model_name="test",
            role="coder",
            owner_group="group-A",
            member_groups=["group-A"],
        )
        registry.register(agent)

        # Write L1 memory
        mem = SlockMemory(
            role="I am a senior Python engineer.",
            key_knowledge="FastAPI, asyncio, PostgreSQL",
            active_context="Working on auth module",
        )
        memory.write_agent_memory(agent.agent_id, mem)

        # Re-register same agent into group-B (simulates cross-group invite)
        agent_b = AgentIdentity(
            agent_id="int-agent-001",
            name="IntegrationBot",
            emoji="🔗",
            agent_type="coco",
            model_name="test",
            role="coder",
            owner_group="group-B",
            member_groups=["group-B"],
        )
        merged = registry.register(agent_b)

        # Verify group membership merged
        assert "group-A" in merged.member_groups
        assert "group-B" in merged.member_groups

        # Verify L1 memory intact
        loaded = memory.read_agent_memory("int-agent-001")
        assert loaded.role == "I am a senior Python engineer."
        assert loaded.key_knowledge == "FastAPI, asyncio, PostgreSQL"
        assert loaded.active_context == "Working on auth module"

    def test_identity_persists_cross_group_on_disk(self, storage):
        """After cross-group register, reload from disk still finds merged identity."""
        registry = storage["registry"]

        agent = AgentIdentity(
            agent_id="int-agent-disk",
            name="DiskBot",
            emoji="💾",
            agent_type="claude",
            model_name="test",
            role="reviewer",
            owner_group="grp-X",
            member_groups=["grp-X"],
        )
        registry.register(agent)

        # Cross-group
        agent2 = AgentIdentity(
            agent_id="int-agent-disk",
            name="DiskBot",
            emoji="💾",
            agent_type="claude",
            model_name="test",
            role="reviewer",
            owner_group="grp-Y",
            member_groups=["grp-Y"],
        )
        registry.register(agent2)

        # Create fresh registry from same disk path
        fresh = AgentRegistry(base_path=registry.base_path)
        reloaded = fresh.get("int-agent-disk")
        assert reloaded is not None
        assert "grp-X" in reloaded.member_groups
        assert "grp-Y" in reloaded.member_groups


# ==================================================================
# Task 3: Boundary test — agent removed from group, L1 memory survives
# ==================================================================


class TestL1MemorySurvivesGroupRemoval:
    """L1 memory persists even when agent is removed from all groups."""

    @pytest.fixture
    def storage(self, tmp_path):
        base = str(tmp_path / "slock_boundary")
        return {
            "registry": AgentRegistry(base_path=base),
            "memory": MemoryManager(base_path=base),
        }

    def test_memory_intact_after_group_removal(self, storage):
        """Remove agent from member_groups; L1 memory file still loadable."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = AgentIdentity(
            agent_id="boundary-agent-001",
            name="BoundaryBot",
            emoji="🚪",
            agent_type="codex",
            model_name="test",
            role="tester",
            owner_group="group-orig",
            member_groups=["group-orig", "group-extra"],
        )
        registry.register(agent)

        mem = SlockMemory(
            role="I specialize in integration testing.",
            key_knowledge="pytest, mocking, fixtures",
        )
        memory.write_agent_memory(agent.agent_id, mem)

        # Simulate removal: update identity with empty member_groups
        agent.member_groups = []
        agent.owner_group = ""
        registry.update(agent)

        # L1 memory MUST still be accessible
        loaded = memory.read_agent_memory("boundary-agent-001")
        assert loaded.role == "I specialize in integration testing."
        assert loaded.key_knowledge == "pytest, mocking, fixtures"

    def test_skill_profiles_survive_group_removal(self, storage):
        """Skill profiles persist after agent leaves all groups."""
        memory = storage["memory"]

        memory.record_skill_feedback("orphan-agent", ["go", "grpc"], quality_score=85.0)

        # Skill profiles remain accessible regardless of group membership
        profiles = memory.read_skill_profiles("orphan-agent")
        tags = {p.tag for p in profiles}
        assert "go" in tags
        assert "grpc" in tags


# ==================================================================
# Task 4: Concurrency safety — parallel update_agent_context
# ==================================================================


class TestConcurrentContextUpdateSafety:
    """Concurrent context updates must not corrupt the role section."""

    @pytest.fixture
    def memory_mgr(self, tmp_path):
        return MemoryManager(base_path=str(tmp_path / "slock_concurrent"))

    def test_parallel_context_updates_preserve_role(self, memory_mgr):
        """Two threads writing context simultaneously cannot corrupt role."""
        agent_id = "concurrent-agent-001"
        original_role = "I am the Principal Architect. I design large-scale systems."
        original_knowledge = "Distributed systems, consensus algorithms"

        memory_mgr.write_agent_memory(
            agent_id,
            SlockMemory(
                role=original_role,
                key_knowledge=original_knowledge,
                active_context="Initial context",
            ),
        )

        errors: list[str] = []
        iterations = 20

        def writer(thread_id: int) -> None:
            try:
                for i in range(iterations):
                    memory_mgr.update_agent_context(
                        agent_id, f"[Thread-{thread_id}] Update {i}"
                    )
            except Exception as e:
                errors.append(f"Thread-{thread_id}: {e}")

        t1 = threading.Thread(target=writer, args=(1,))
        t2 = threading.Thread(target=writer, args=(2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"Thread errors: {errors}"

        # Critical assertion: role and key_knowledge must be intact
        final = memory_mgr.read_agent_memory(agent_id)
        assert final.role == original_role
        assert final.key_knowledge == original_knowledge
        # Context should have accumulated entries from both threads
        assert "Thread-1" in final.active_context
        assert "Thread-2" in final.active_context
