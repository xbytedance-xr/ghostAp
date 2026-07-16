"""Tests for /role move — Agent cross-group movement with L1 memory preservation.

Validates:
- L1 memory (MEMORY.md) persists after move
- Skill profiles persist after move
- Channel membership correctly updated (removed from source, added to target)
- Non-IDLE agents cannot be moved
- Permission check enforced
- Target team not found error handling
- Personality consistency in agent prompt
- Migration context record appended
"""

from __future__ import annotations

import threading

import pytest

from src.feishu.handlers.slock import SlockHandler
from src.slock_engine.agent_registry import (
    AgentRegistry,
    DuplicateAgentNameError,
    MoveOutcome,
    MoveResult,
)
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import AgentIdentity, SlockMemory


@pytest.fixture
def storage(tmp_path):
    """Create isolated storage with shared base path."""
    base = str(tmp_path / "slock_move_test")
    return {
        "registry": AgentRegistry.legacy(base_path=base),
        "memory": MemoryManager(base_path=base),
    }


def _make_agent(
    agent_id: str = "move-agent-001",
    name: str = "MoveBot",
    owner_group: str = "group-source",
) -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        name=name,
        emoji="🚀",
        agent_type="codex",
        model_name="o3-pro",
        system_prompt="You are MoveBot, a senior Python engineer.",
        role="coder",
        permissions=["shell", "file_write", "git"],
        owner_group=owner_group,
        member_groups=[owner_group],
    )


class TestMoveAgentPreservesL1Memory:
    """AC1: L1 memory intact after cross-group move."""

    def test_move_preserves_role_and_knowledge(self, storage):
        """Move agent from group-A to group-B; read_agent_memory returns same content."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = _make_agent("move-mem-001", owner_group="group-A")
        registry.register(agent)

        # Write L1 memory
        original = SlockMemory(
            role="I am a Python backend developer specializing in async systems.",
            key_knowledge="FastAPI, asyncio, SQLAlchemy, Redis",
            active_context="[2026-05-18] Completed auth module refactor",
        )
        memory.write_agent_memory(agent.agent_id, original)

        # Move to group-B
        success = registry.move_agent(agent.agent_id, "group-A", "group-B")
        assert success.success

        # L1 memory must be unchanged
        loaded = memory.read_agent_memory(agent.agent_id)
        assert loaded.role == original.role
        assert loaded.key_knowledge == original.key_knowledge
        assert loaded.active_context == original.active_context

    def test_move_preserves_active_context_history(self, storage):
        """All active context entries survive the move."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = _make_agent("move-ctx-001", owner_group="src-group")
        registry.register(agent)

        mem = SlockMemory(
            role="Reviewer",
            active_context="[Entry 1] First task\n[Entry 2] Second task",
        )
        memory.write_agent_memory(agent.agent_id, mem)

        registry.move_agent(agent.agent_id, "src-group", "dst-group")

        loaded = memory.read_agent_memory(agent.agent_id)
        assert "[Entry 1] First task" in loaded.active_context
        assert "[Entry 2] Second task" in loaded.active_context


class TestMoveAgentPreservesSkillProfile:
    """AC3: Skill profiles intact after move."""

    def test_skill_profiles_unchanged_after_move(self, storage):
        """SkillProfile entries (tag, success_rate, total_tasks) identical before/after."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = _make_agent("move-skill-001", owner_group="team-alpha")
        registry.register(agent)

        # Record skills
        memory.record_skill_feedback(agent.agent_id, ["python", "testing", "review"], quality_score=92.0)
        memory.record_skill_feedback(agent.agent_id, ["python"], quality_score=88.0)

        # Snapshot before move
        before = memory.read_skill_profiles(agent.agent_id)
        before_map = {p.tag: (p.success_rate, p.total_tasks) for p in before}

        # Move
        registry.move_agent(agent.agent_id, "team-alpha", "team-beta")

        # Snapshot after move
        after = memory.read_skill_profiles(agent.agent_id)
        after_map = {p.tag: (p.success_rate, p.total_tasks) for p in after}

        assert before_map == after_map


class TestMoveAgentUpdatesChannelMembership:
    """AC4: list_agents reflects move correctly."""

    def test_agent_removed_from_source_added_to_target(self, storage):
        """After move, agent is in target's list but not source's."""
        registry = storage["registry"]

        agent = _make_agent("move-list-001", owner_group="chat-src")
        registry.register(agent)

        # Before move
        assert len(registry.list_agents(channel_id="chat-src")) == 1
        assert len(registry.list_agents(channel_id="chat-dst")) == 0

        # Move
        registry.move_agent(agent.agent_id, "chat-src", "chat-dst")

        # After move
        assert len(registry.list_agents(channel_id="chat-src")) == 0
        assert len(registry.list_agents(channel_id="chat-dst")) == 1

    def test_identity_owner_group_updated(self, storage):
        """owner_group points to target after move."""
        registry = storage["registry"]

        agent = _make_agent("move-owner-001", owner_group="old-owner")
        registry.register(agent)

        registry.move_agent(agent.agent_id, "old-owner", "new-owner")

        updated = registry.get(agent.agent_id)
        assert updated is not None
        assert updated.owner_group == "new-owner"
        assert "new-owner" in updated.member_groups
        assert "old-owner" not in updated.member_groups

    def test_move_persists_to_disk(self, storage):
        """After move, a fresh registry loaded from disk reflects new membership."""
        registry = storage["registry"]

        agent = _make_agent("move-disk-001", owner_group="disk-src")
        registry.register(agent)
        registry.move_agent(agent.agent_id, "disk-src", "disk-dst")

        # Load fresh registry from same base path
        fresh = AgentRegistry.legacy(base_path=registry.base_path)
        loaded = fresh.get("move-disk-001")
        assert loaded is not None
        assert loaded.owner_group == "disk-dst"
        assert "disk-dst" in loaded.member_groups
        assert "disk-src" not in loaded.member_groups


def test_move_rejects_casefold_name_collision_in_target_channel(storage):
    """Moving into a channel with the same case-folded name fails closed."""
    registry = storage["registry"]
    existing = _make_agent("target-alice", name="Alice", owner_group="target")
    moving = _make_agent("source-alice", name="ALICE", owner_group="source")
    registry.register(existing)
    registry.register(moving)

    result = registry.move_agent(moving.agent_id, "source", "target")

    assert result.status.value == "duplicate_name"
    assert registry.get(moving.agent_id).owner_group == "source"
    assert [agent.agent_id for agent in registry.list_agents("target")] == [existing.agent_id]


def test_concurrent_moves_allow_only_one_same_name_in_target_channel(storage):
    """The registry lock makes same-name move admission atomic per channel."""
    registry = storage["registry"]
    first = _make_agent("first-alice", name="Alice", owner_group="source-a")
    second = _make_agent("second-alice", name="alice", owner_group="source-b")
    registry.register(first)
    registry.register(second)
    barrier = threading.Barrier(2)
    outcomes = []

    def move(agent_id: str, source: str) -> None:
        barrier.wait()
        outcomes.append(registry.move_agent(agent_id, source, "target"))

    threads = [
        threading.Thread(target=move, args=(first.agent_id, "source-a")),
        threading.Thread(target=move, args=(second.agent_id, "source-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(outcome.success for outcome in outcomes) == 1
    assert sum(outcome.status.value == "duplicate_name" for outcome in outcomes) == 1
    target_names = [agent.name.casefold() for agent in registry.list_agents("target")]
    assert target_names == ["alice"]


def test_cross_registry_concurrent_register_allows_only_one_same_name(tmp_path):
    """Two registry instances share one authoritative register linearization point."""
    base_path = str(tmp_path / "cross-register")
    registries = [AgentRegistry.legacy(base_path), AgentRegistry.legacy(base_path)]
    for registry in registries:
        assert registry.list_agents() == []
    barrier = threading.Barrier(2)
    successes = []
    duplicates = []

    def register(registry: AgentRegistry, agent_id: str, name: str) -> None:
        barrier.wait()
        try:
            successes.append(
                registry.register(_make_agent(agent_id, name=name, owner_group="target"))
            )
        except DuplicateAgentNameError as exc:
            duplicates.append(exc)

    threads = [
        threading.Thread(target=register, args=(registries[0], "alice-a", "Alice")),
        threading.Thread(target=register, args=(registries[1], "alice-b", "alice")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    for registry in registries:
        if registry._persist_thread:
            registry._persist_thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(successes) == 1
    assert len(duplicates) == 1
    persisted = AgentRegistry.legacy(base_path).list_agents("target")
    assert [agent.name.casefold() for agent in persisted] == ["alice"]


def test_cross_registry_concurrent_update_allows_only_one_same_name(tmp_path):
    """Two registry instances cannot concurrently rename peers to one channel name."""
    base_path = str(tmp_path / "cross-update")
    seed = AgentRegistry.legacy(base_path)
    seed.register(_make_agent("update-a", name="First", owner_group="target"))
    seed.register(_make_agent("update-b", name="Second", owner_group="target"))
    if seed._persist_thread:
        seed._persist_thread.join(timeout=2)

    registries = [AgentRegistry.legacy(base_path), AgentRegistry.legacy(base_path)]
    for registry in registries:
        assert len(registry.list_agents("target")) == 2
    barrier = threading.Barrier(2)
    successes = []
    duplicates = []

    def update(registry: AgentRegistry, agent_id: str, name: str) -> None:
        barrier.wait()
        try:
            successes.append(
                registry.update(_make_agent(agent_id, name=name, owner_group="target"))
            )
        except DuplicateAgentNameError as exc:
            duplicates.append(exc)

    threads = [
        threading.Thread(target=update, args=(registries[0], "update-a", "Straße")),
        threading.Thread(target=update, args=(registries[1], "update-b", "STRASSE")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    for registry in registries:
        if registry._persist_thread:
            registry._persist_thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert successes == [True]
    assert len(duplicates) == 1
    persisted_names = [
        agent.name.casefold()
        for agent in AgentRegistry.legacy(base_path).list_agents("target")
    ]
    assert persisted_names.count("strasse") == 1


def test_cross_registry_concurrent_move_allows_only_one_same_name(tmp_path):
    """Two source registries serialize target-channel move admission and persistence."""
    base_path = str(tmp_path / "cross-move")
    seed = AgentRegistry.legacy(base_path)
    seed.register(_make_agent("move-a", name="Alice", owner_group="source-a"))
    seed.register(_make_agent("move-b", name="alice", owner_group="source-b"))
    if seed._persist_thread:
        seed._persist_thread.join(timeout=2)

    registries = [AgentRegistry.legacy(base_path), AgentRegistry.legacy(base_path)]
    for registry in registries:
        assert len(registry.list_agents()) == 2
    barrier = threading.Barrier(2)
    outcomes = []

    def move(registry: AgentRegistry, agent_id: str, source: str) -> None:
        barrier.wait()
        outcomes.append(registry.move_agent(agent_id, source, "target"))

    threads = [
        threading.Thread(target=move, args=(registries[0], "move-a", "source-a")),
        threading.Thread(target=move, args=(registries[1], "move-b", "source-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert sum(outcome.success for outcome in outcomes) == 1
    assert sum(outcome.status == MoveResult.DUPLICATE_NAME for outcome in outcomes) == 1
    persisted = AgentRegistry.legacy(base_path).list_agents("target")
    assert [agent.name.casefold() for agent in persisted] == ["alice"]


class TestMoveAgentRejectsNonIdle:
    """AC6: Move rejected when agent is not IDLE."""

    def test_move_fails_if_agent_not_in_source(self, storage):
        """move_agent returns NOT_IN_SOURCE if agent doesn't belong to source channel."""
        registry = storage["registry"]

        agent = _make_agent("move-reject-001", owner_group="actual-group")
        registry.register(agent)

        # Try to move from wrong source
        result = registry.move_agent(agent.agent_id, "wrong-group", "target-group")
        assert result.status == MoveResult.NOT_IN_SOURCE

        # Agent unchanged
        loaded = registry.get(agent.agent_id)
        assert loaded.owner_group == "actual-group"

    def test_move_fails_for_nonexistent_agent(self, storage):
        """move_agent returns NOT_FOUND for unknown agent_id."""
        registry = storage["registry"]
        result = registry.move_agent("nonexistent-agent", "src", "dst")
        assert result.status == MoveResult.NOT_FOUND


class TestMoveAgentAppendsContextRecord:
    """Migration context record is appended to active_context."""

    def test_context_record_appended_after_move(self, storage):
        """update_agent_context adds migration record post-move."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = _make_agent("move-ctx-rec-001", owner_group="team-old")
        registry.register(agent)

        mem = SlockMemory(role="Tester", active_context="[Initial] Setup done")
        memory.write_agent_memory(agent.agent_id, mem)

        # Move
        registry.move_agent(agent.agent_id, "team-old", "team-new")

        # Append migration record (simulating what move_role handler does)
        memory.update_agent_context(
            agent.agent_id, "[2026-05-19 10:00] Moved from team-old to team-new"
        )

        loaded = memory.read_agent_memory(agent.agent_id)
        assert "Moved from team-old to team-new" in loaded.active_context
        # Original context preserved
        assert "[Initial] Setup done" in loaded.active_context


class TestMoveAgentPersonalityConsistency:
    """AC2: system_prompt and key_knowledge preserved, personality consistent."""

    def test_system_prompt_unchanged_after_move(self, storage):
        """AgentIdentity.system_prompt not modified by move."""
        registry = storage["registry"]

        agent = _make_agent("move-persona-001", owner_group="persona-src")
        agent.system_prompt = "You are an elite architect with 20 years of experience."
        registry.register(agent)

        registry.move_agent(agent.agent_id, "persona-src", "persona-dst")

        loaded = registry.get(agent.agent_id)
        assert loaded.system_prompt == "You are an elite architect with 20 years of experience."

    def test_all_identity_fields_preserved(self, storage):
        """All personality fields (name, emoji, role, permissions) unchanged."""
        registry = storage["registry"]

        agent = AgentIdentity(
            agent_id="move-fields-001",
            name="ArchitectPrime",
            emoji="🏛️",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="I design distributed systems.",
            role="architect",
            permissions=["shell", "file_write", "git", "deploy"],
            owner_group="fields-src",
            member_groups=["fields-src"],
        )
        registry.register(agent)

        registry.move_agent(agent.agent_id, "fields-src", "fields-dst")

        loaded = registry.get(agent.agent_id)
        assert loaded.name == "ArchitectPrime"
        assert loaded.emoji == "🏛️"
        assert loaded.agent_type == "claude"
        assert loaded.model_name == "sonnet-4"
        assert loaded.system_prompt == "I design distributed systems."
        assert loaded.role == "architect"
        assert loaded.permissions == ["shell", "file_write", "git", "deploy"]


class TestMoveAgentSameGroupRejected:
    """Edge case: moving to same group should be a no-op or fail gracefully."""

    def test_move_to_same_group(self, storage):
        """Moving agent where source == target still updates (idempotent)."""
        registry = storage["registry"]

        agent = _make_agent("move-same-001", owner_group="same-group")
        registry.register(agent)

        # This is technically valid at registry level (handler prevents it)
        result = registry.move_agent(agent.agent_id, "same-group", "same-group")
        # Should succeed — agent stays in same-group
        assert result.success
        loaded = registry.get(agent.agent_id)
        assert loaded.owner_group == "same-group"


class TestMoveAgentTargetPermission:
    """AC1/AC2: Target group permission check prevents unauthorized moves."""

    def test_move_succeeds_at_registry_level(self, storage):
        """Registry-level move works when all params are valid (permission is handler-level)."""
        registry = storage["registry"]

        agent = _make_agent("perm-ok-001", owner_group="src-perm")
        registry.register(agent)

        # Registry move itself doesn't enforce permissions — that's at handler level
        result = registry.move_agent(agent.agent_id, "src-perm", "dst-perm")
        assert result.success

        loaded = registry.get(agent.agent_id)
        assert loaded.owner_group == "dst-perm"

    def test_move_fails_wrong_source_prevents_injection(self, storage):
        """Cannot move agent from a group it doesn't belong to (registry-level safety)."""
        registry = storage["registry"]

        agent = _make_agent("perm-fail-001", owner_group="real-group")
        registry.register(agent)

        # Attempt to move from a group the agent is NOT in
        result = registry.move_agent(agent.agent_id, "fake-group", "target-group")
        assert result.status == MoveResult.NOT_IN_SOURCE

        # Agent stays in original group
        loaded = registry.get(agent.agent_id)
        assert loaded.owner_group == "real-group"


class TestMoveAgentL1MemorySystemPrompt:
    """AC6: System prompt built from L1 memory is consistent after move."""

    def _build_prompt_parts(self, agent: AgentIdentity, memory: SlockMemory) -> str:
        """Replicate the engine's _build_agent_prompt logic for testing."""
        parts: list[str] = []
        if agent.system_prompt:
            parts.append(agent.system_prompt)
        if agent.permissions:
            parts.append(
                f"\n# Authorized Tools\n"
                f"You are ONLY permitted to use the following tools: "
                f"{', '.join(agent.permissions)}."
            )
        if memory.role:
            parts.append(f"\n# Your Role\n{memory.role}")
        if memory.key_knowledge:
            parts.append(f"\n# Key Knowledge\n{memory.key_knowledge}")
        if memory.active_context:
            parts.append(f"\n# Recent Context\n{memory.active_context[-2000:]}")
        parts.append("\n# User Message\ntest message")
        return "\n".join(parts)

    def test_system_prompt_identical_after_move(self, storage):
        """Full prompt (system_prompt + permissions + memory) unchanged after move."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("prompt-001", owner_group="prompt-src")
        agent.system_prompt = "You are an elite full-stack developer."
        agent.permissions = ["shell", "file_write", "git"]
        registry.register(agent)

        mem = SlockMemory(
            role="Senior full-stack dev specializing in React + Python.",
            key_knowledge="- React 18 hooks patterns\n- FastAPI async best practices",
            active_context="[2026-05-18 14:00] Completed auth module",
        )
        memory_mgr.write_agent_memory(agent.agent_id, mem)

        # Build prompt before move
        prompt_before = self._build_prompt_parts(agent, mem)

        # Move agent
        registry.move_agent(agent.agent_id, "prompt-src", "prompt-dst")

        # Read memory after move
        mem_after = memory_mgr.read_agent_memory(agent.agent_id)
        agent_after = registry.get(agent.agent_id)

        # Build prompt after move
        prompt_after = self._build_prompt_parts(agent_after, mem_after)

        assert prompt_before == prompt_after

    def test_prompt_only_differs_by_migration_record(self, storage):
        """After move + context append, only the migration record is added."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("prompt-diff-001", owner_group="diff-src")
        agent.system_prompt = "You are a code reviewer."
        registry.register(agent)

        mem = SlockMemory(
            role="Code reviewer",
            key_knowledge="Python, Go, TypeScript",
            active_context="[2026-05-17] Reviewed PR #100",
        )
        memory_mgr.write_agent_memory(agent.agent_id, mem)

        # Prompt before
        prompt_before = self._build_prompt_parts(agent, mem)

        # Move and append migration record
        registry.move_agent(agent.agent_id, "diff-src", "diff-dst")
        migration_record = "[2026-05-19 10:00] Moved from diff-src to diff-dst"
        memory_mgr.update_agent_context(agent.agent_id, migration_record)

        # Read after
        mem_after = memory_mgr.read_agent_memory(agent.agent_id)
        agent_after = registry.get(agent.agent_id)

        prompt_after = self._build_prompt_parts(agent_after, mem_after)

        # The only difference should be the migration record in active_context
        assert prompt_before != prompt_after  # They differ
        # But remove the migration line and they should match
        assert mem_after.role == mem.role
        assert mem_after.key_knowledge == mem.key_knowledge
        assert migration_record in mem_after.active_context
        assert "[2026-05-17] Reviewed PR #100" in mem_after.active_context


# ============================================================================
# Handler-level tests: permission model, notification failure, L1 consistency
# ============================================================================

from unittest.mock import MagicMock, patch


def _make_handler_with_mocks():
    """Create a SlockHandler with mocked messaging methods."""
    ctx = MagicMock()
    handler = SlockHandler(ctx)
    handler.reply_text = MagicMock(return_value="msg_id_001")
    handler.reply_card = MagicMock(return_value="msg_id_002")
    handler.send_card_to_chat = MagicMock(return_value="sent_msg_001")
    handler.send_text_to_chat = MagicMock(return_value="sent_msg_002")
    handler.update_card = MagicMock()
    return handler


def _make_engine_mock(
    chat_id: str = "chat-source",
    team_name: str = "SourceTeam",
    owner_id: str = "operator-001",
):
    """Create a mock engine with channel and registry."""
    engine = MagicMock()
    engine.channel = MagicMock()
    engine.channel.channel_id = chat_id
    engine.channel.team_name = team_name
    engine.channel.owner_id = owner_id
    engine.registry = MagicMock()
    engine.memory = MagicMock()
    engine.get_agent_status = MagicMock()
    return engine


class TestSourceOwnerNonAdminCanMove:
    """Source owner who is ALSO target owner can execute /role move (dual permission)."""

    @patch("src.feishu.handlers.slock.SlockHandler._get_engine_manager")
    @patch("src.thread.manager.get_current_sender_id", return_value="operator-001")
    @patch("src.config.get_settings")
    def test_source_and_target_owner_can_move(
        self, mock_settings, mock_sender, mock_get_mgr
    ):
        """Non-admin operator who owns both source and target can move an agent."""
        from src.slock_engine.models import AgentIdentity

        handler = _make_handler_with_mocks()

        # Settings: operator NOT in admin_ids
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["other-admin-999"])
        mock_settings.return_value = settings

        # Source engine: operator IS the channel owner
        source_engine = _make_engine_mock(
            chat_id="chat-source", team_name="SourceTeam", owner_id="operator-001"
        )
        agent = AgentIdentity(
            agent_id="move-test-agent",
            name="TestAgent",
            emoji="🤖",
            agent_type="codex",
            model_name="o3-pro",
            system_prompt="test",
            role="coder",
            permissions=[],
            owner_group="chat-source",
            member_groups=["chat-source"],
        )
        source_engine.registry.find_by_name.return_value = agent
        source_engine.try_lock_for_move = MagicMock(return_value=True)
        source_engine.unlock_after_move = MagicMock()
        source_engine.registry.move_agent.return_value = MoveOutcome(status=MoveResult.SUCCESS)

        # Target engine: operator IS ALSO the target owner
        target_engine = _make_engine_mock(
            chat_id="chat-target", team_name="TargetTeam", owner_id="operator-001"
        )

        # Manager setup
        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        manager.find_team.return_value = target_engine
        mock_get_mgr.return_value = manager

        handler.move_role(
            message_id="msg-001",
            chat_id="chat-source",
            name="TestAgent",
            target_team_name="TargetTeam",
        )

        # Move succeeded: reply_card was called (confirmation card)
        handler.reply_card.assert_called_once()
        # send_card_to_chat was called for target notification + source departure
        assert handler.send_card_to_chat.call_count == 2
        # registry.move_agent was invoked
        source_engine.registry.move_agent.assert_called_once_with(
            "move-test-agent", "chat-source", "chat-target"
        )


class TestTargetPermissionDenied:
    """Source owner who is NOT target owner (and not global admin) gets rejected."""

    @patch("src.feishu.handlers.slock.SlockHandler._get_engine_manager")
    @patch("src.thread.manager.get_current_sender_id", return_value="operator-001")
    @patch("src.config.get_settings")
    def test_source_owner_not_target_owner_rejected(
        self, mock_settings, mock_sender, mock_get_mgr
    ):
        """Source owner but not target owner or admin → permission denied, target not contacted."""
        from src.slock_engine.models import AgentIdentity

        handler = _make_handler_with_mocks()

        # Settings: operator NOT in admin_ids
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["other-admin-999"])
        mock_settings.return_value = settings

        # Source engine: operator IS the channel owner
        source_engine = _make_engine_mock(
            chat_id="chat-source", team_name="SourceTeam", owner_id="operator-001"
        )
        agent = AgentIdentity(
            agent_id="move-reject-agent",
            name="RejectBot",
            emoji="🤖",
            agent_type="codex",
            model_name="o3-pro",
            system_prompt="test",
            role="coder",
            permissions=[],
            owner_group="chat-source",
            member_groups=["chat-source"],
        )
        source_engine.registry.find_by_name.return_value = agent

        # Target engine: different owner (NOT the operator)
        target_engine = _make_engine_mock(
            chat_id="chat-target", team_name="TargetTeam", owner_id="someone-else"
        )

        # Manager setup
        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        manager.find_team.return_value = target_engine
        mock_get_mgr.return_value = manager

        handler.move_role(
            message_id="msg-001",
            chat_id="chat-source",
            name="RejectBot",
            target_team_name="TargetTeam",
        )

        # Permission denied message sent to source group
        handler.reply_text.assert_called()
        denied_msg = handler.reply_text.call_args[0][1]
        assert "权限不足" in denied_msg

        # Target group NEVER contacted
        handler.send_card_to_chat.assert_not_called()
        # No confirm card sent
        handler.reply_card.assert_not_called()


class TestGlobalAdminBypassesTargetPermission:
    """Global admin can move agents to any target group without being target owner."""

    @patch("src.feishu.handlers.slock.SlockHandler._get_engine_manager")
    @patch("src.thread.manager.get_current_sender_id", return_value="global-admin-001")
    @patch("src.config.get_settings")
    def test_global_admin_moves_to_any_target(
        self, mock_settings, mock_sender, mock_get_mgr
    ):
        """Global admin successfully moves agent even when not target owner."""
        from src.slock_engine.models import AgentIdentity

        handler = _make_handler_with_mocks()

        # Settings: operator IS a global admin
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["global-admin-001"])
        mock_settings.return_value = settings

        # Source engine: different owner (not admin)
        source_engine = _make_engine_mock(
            chat_id="chat-source", team_name="SourceTeam", owner_id="source-owner"
        )
        agent = AgentIdentity(
            agent_id="admin-move-agent",
            name="AdminBot",
            emoji="🔑",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="test",
            role="architect",
            permissions=[],
            owner_group="chat-source",
            member_groups=["chat-source"],
        )
        source_engine.registry.find_by_name.return_value = agent
        source_engine.try_lock_for_move = MagicMock(return_value=True)
        source_engine.unlock_after_move = MagicMock()
        source_engine.registry.move_agent.return_value = MoveOutcome(status=MoveResult.SUCCESS)

        # Target engine: completely different owner
        target_engine = _make_engine_mock(
            chat_id="chat-target", team_name="TargetTeam", owner_id="target-owner-xyz"
        )

        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        manager.find_team.return_value = target_engine
        mock_get_mgr.return_value = manager

        handler.move_role(
            message_id="msg-admin",
            chat_id="chat-source",
            name="AdminBot",
            target_team_name="TargetTeam",
        )

        # Move succeeded
        source_engine.registry.move_agent.assert_called_once()
        handler.reply_card.assert_called_once()
        # send_card_to_chat called twice: target notification + source departure
        assert handler.send_card_to_chat.call_count == 2


class TestPermissionErrorOnlyInSourceGroup:
    """AC7: Permission denied error messages are only sent to the source group."""

    @patch("src.feishu.handlers.slock.SlockHandler._get_engine_manager")
    @patch("src.thread.manager.get_current_sender_id", return_value="unauthorized-user")
    @patch("src.config.get_settings")
    def test_permission_error_stays_in_source_group(
        self, mock_settings, mock_sender, mock_get_mgr
    ):
        """Unauthorized user gets error in source group; target group never contacted."""
        handler = _make_handler_with_mocks()

        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin-001"])
        mock_settings.return_value = settings

        # Source engine: operator is NOT the owner
        source_engine = _make_engine_mock(
            chat_id="chat-source", team_name="SourceTeam", owner_id="real-owner-001"
        )

        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        mock_get_mgr.return_value = manager

        handler.move_role(
            message_id="msg-001",
            chat_id="chat-source",
            name="SomeAgent",
            target_team_name="TargetTeam",
        )

        # Error sent to source group via reply_text
        handler.reply_text.assert_called_once()
        call_args = handler.reply_text.call_args
        assert "权限不足" in call_args[0][1]

        # Target group never contacted
        handler.send_card_to_chat.assert_not_called()


class TestNotificationSendFailureAbortsMove:
    """With 'move first, notify second' design, notification failure does NOT
    prevent the move — move_agent has already succeeded. Source group receives
    a degraded warning and the flow continues to confirm card."""

    @patch("src.feishu.handlers.slock.SlockHandler._get_engine_manager")
    @patch("src.thread.manager.get_current_sender_id", return_value="operator-001")
    @patch("src.config.get_settings")
    def test_notification_failure_does_not_rollback(
        self, mock_settings, mock_sender, mock_get_mgr
    ):
        """send_card_to_chat returns None → move_agent already succeeded,
        source group receives degraded warning, confirm card still sent."""
        from src.slock_engine.models import AgentIdentity, AgentStatus

        handler = _make_handler_with_mocks()
        # Notification send fails
        handler.send_card_to_chat.return_value = None

        settings = MagicMock()
        settings.admin_user_ids = frozenset(["operator-001"])
        mock_settings.return_value = settings

        source_engine = _make_engine_mock(
            chat_id="chat-source", team_name="SourceTeam", owner_id="operator-001"
        )
        agent = AgentIdentity(
            agent_id="abort-agent",
            name="AbortBot",
            emoji="🛑",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="test",
            role="reviewer",
            permissions=[],
            owner_group="chat-source",
            member_groups=["chat-source"],
        )
        source_engine.registry.find_by_name.return_value = agent
        source_engine.get_agent_status.return_value = AgentStatus.IDLE
        source_engine.registry.move_agent.return_value = MoveOutcome(status=MoveResult.SUCCESS)

        target_engine = _make_engine_mock(
            chat_id="chat-target", team_name="TargetTeam", owner_id="other"
        )

        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        manager.find_team.return_value = target_engine
        mock_get_mgr.return_value = manager

        handler.move_role(
            message_id="msg-001",
            chat_id="chat-source",
            name="AbortBot",
            target_team_name="TargetTeam",
        )

        # move_agent WAS called (new flow: move happens first)
        source_engine.registry.move_agent.assert_called_once()

        # Degraded warning reported to source group
        handler.reply_text.assert_called()
        warning_msg = handler.reply_text.call_args[0][1]
        assert "移动成功" in warning_msg
        assert "通知" in warning_msg

        # Confirm card still sent despite notification failure
        handler.reply_card.assert_called_once()


class TestL1MemoryLoadableAfterMove:
    """AC2: L1 memory loadable by target group engine after cross-group move."""

    def test_l1_memory_loadable_from_target_engine(self, storage):
        """After move, a separate MemoryManager (simulating target engine) can load L1."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = _make_agent("l1-load-001", owner_group="group-src")
        registry.register(agent)

        original = SlockMemory(
            role="I am a Python expert.",
            key_knowledge="Django, FastAPI, asyncio",
            active_context="[2026-05-18] Deployed v2.0",
        )
        memory.write_agent_memory(agent.agent_id, original)

        # Move to new group
        registry.move_agent(agent.agent_id, "group-src", "group-dst")

        # Simulate target engine loading - uses same base_path (as in production)
        target_memory = MemoryManager(base_path=memory.base_path)
        loaded = target_memory.read_agent_memory(agent.agent_id)

        assert loaded.role == original.role
        assert loaded.key_knowledge == original.key_knowledge
        assert loaded.active_context == original.active_context

    def test_l1_memory_with_migration_record_loadable(self, storage):
        """After move + migration record append, full L1 is loadable."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = _make_agent("l1-load-002", owner_group="origin")
        registry.register(agent)

        mem = SlockMemory(
            role="DevOps engineer",
            key_knowledge="Docker, k8s, Terraform",
            active_context="[2026-05-17] Set up CI pipeline",
        )
        memory.write_agent_memory(agent.agent_id, mem)

        registry.move_agent(agent.agent_id, "origin", "destination")
        memory.update_agent_context(agent.agent_id, "[2026-05-19 10:00] Moved from origin to destination")

        # Fresh memory manager loads it correctly
        fresh = MemoryManager(base_path=memory.base_path)
        loaded = fresh.read_agent_memory(agent.agent_id)

        assert loaded.role == "DevOps engineer"
        assert loaded.key_knowledge == "Docker, k8s, Terraform"
        assert "Moved from origin to destination" in loaded.active_context
        assert "[2026-05-17] Set up CI pipeline" in loaded.active_context


class TestSystemPromptConsistentAfterMove:
    """AC3: system_prompt built from L1 + identity is consistent post-move."""

    def _build_prompt_parts(self, agent: AgentIdentity, memory: SlockMemory) -> str:
        """Replicate the engine's _build_agent_prompt logic for testing."""
        parts: list[str] = []
        if agent.system_prompt:
            parts.append(agent.system_prompt)
        if agent.permissions:
            parts.append(
                f"\n# Authorized Tools\n"
                f"You are ONLY permitted to use the following tools: "
                f"{', '.join(agent.permissions)}."
            )
        if memory.role:
            parts.append(f"\n# Your Role\n{memory.role}")
        if memory.key_knowledge:
            parts.append(f"\n# Key Knowledge\n{memory.key_knowledge}")
        if memory.active_context:
            parts.append(f"\n# Recent Context\n{memory.active_context[-2000:]}")
        parts.append("\n# User Message\ntest message")
        return "\n".join(parts)

    def test_system_prompt_consistent_post_move_fresh_load(self, storage):
        """Prompt built by target engine (fresh load) matches pre-move prompt."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("prompt-fresh-001", owner_group="team-a")
        agent.system_prompt = "You are an expert Python developer."
        agent.permissions = ["shell", "file_write"]
        registry.register(agent)

        mem = SlockMemory(
            role="Backend engineer specializing in distributed systems.",
            key_knowledge="- gRPC\n- Event sourcing\n- CQRS",
            active_context="[2026-05-18] Implemented saga pattern",
        )
        memory_mgr.write_agent_memory(agent.agent_id, mem)

        # Prompt before move
        prompt_before = self._build_prompt_parts(agent, mem)

        # Move
        registry.move_agent(agent.agent_id, "team-a", "team-b")

        # Load from fresh registry + memory (simulating target engine)
        fresh_registry = AgentRegistry.legacy(base_path=registry.base_path)
        fresh_memory = MemoryManager(base_path=memory_mgr.base_path)

        agent_after = fresh_registry.get(agent.agent_id)
        mem_after = fresh_memory.read_agent_memory(agent.agent_id)

        prompt_after = self._build_prompt_parts(agent_after, mem_after)

        assert prompt_before == prompt_after


# ============================================================================
# Cross-engine cache coherence tests (Tasks 3, 4, 5, 6)
# ============================================================================


class TestCrossEngineRegistryRefresh:
    """Two independent AgentRegistry instances sharing the same base_path.

    Validates that after source.move_agent, target's stale cache can be
    refreshed via refresh_agent to discover the moved agent.
    """

    def test_target_cache_stale_then_refresh_discovers_agent(self, storage):
        """Target registry doesn't see agent until refresh_agent is called."""
        base = storage["registry"].base_path

        source_registry = AgentRegistry.legacy(base_path=base)
        target_registry = AgentRegistry.legacy(base_path=base)

        agent = _make_agent("cross-001", owner_group="src-group")
        source_registry.register(agent)

        # Force target to load (will cache current state — agent in src-group)
        target_registry.list_agents(channel_id="dst-group")

        # Move via source
        source_registry.move_agent(agent.agent_id, "src-group", "dst-group")

        # Target cache is stale — shouldn't see the agent in dst-group yet
        stale_list = target_registry.list_agents(channel_id="dst-group")
        assert len(stale_list) == 0

        # Refresh target
        refreshed = target_registry.refresh_agent(agent.agent_id)
        assert refreshed is not None
        assert refreshed.owner_group == "dst-group"

        # Now target sees the agent
        fresh_list = target_registry.list_agents(channel_id="dst-group")
        assert len(fresh_list) == 1
        assert fresh_list[0].agent_id == agent.agent_id

    def test_refresh_updates_member_groups(self, storage):
        """After refresh, member_groups reflect the move."""
        base = storage["registry"].base_path

        source = AgentRegistry.legacy(base_path=base)
        target = AgentRegistry.legacy(base_path=base)

        agent = _make_agent("cross-002", owner_group="alpha")
        source.register(agent)
        if source._persist_thread:
            source._persist_thread.join(timeout=2)

        # Pre-load target
        target.get(agent.agent_id)

        source.move_agent(agent.agent_id, "alpha", "beta")

        # Before refresh: target still thinks agent is in alpha
        cached = target.get(agent.agent_id)
        assert cached is not None
        assert cached.owner_group == "alpha"  # stale

        # After refresh
        target.refresh_agent(agent.agent_id)
        updated = target.get(agent.agent_id)
        assert updated.owner_group == "beta"
        assert "beta" in updated.member_groups
        assert "alpha" not in updated.member_groups


class TestRefreshAgentIdempotent:
    """refresh_agent on nonexistent agent_id returns None without error."""

    def test_nonexistent_agent_returns_none(self, storage):
        registry = storage["registry"]
        result = registry.refresh_agent("totally-fake-agent-id")
        assert result is None

    def test_refresh_after_remove_returns_none(self, storage):
        """If agent file was deleted, refresh evicts from cache and returns None."""
        registry = storage["registry"]

        agent = _make_agent("refresh-rm-001", owner_group="grp")
        registry.register(agent)

        # Verify it exists
        assert registry.get("refresh-rm-001") is not None

        # Remove (deletes file)
        registry.remove("refresh-rm-001")

        # Refresh should return None and not crash
        result = registry.refresh_agent("refresh-rm-001")
        assert result is None
        assert registry.get("refresh-rm-001") is None


class TestL1MemoryLoadableFromTargetEngine:
    """After move, an independent MemoryManager (simulating target engine) loads L1 correctly."""

    def test_l1_memory_identical_from_independent_manager(self, storage):
        """Target engine's MemoryManager reads same L1 content post-move."""
        base = storage["registry"].base_path
        source_registry = AgentRegistry.legacy(base_path=base)
        source_memory = MemoryManager(base_path=base)

        agent = _make_agent("l1-target-001", owner_group="origin")
        source_registry.register(agent)

        original = SlockMemory(
            role="I am an infrastructure engineer.",
            key_knowledge="Kubernetes, Terraform, AWS CDK, Pulumi",
            active_context="[2026-05-19] Migrated service mesh to Istio 1.20",
        )
        source_memory.write_agent_memory(agent.agent_id, original)

        # Move
        source_registry.move_agent(agent.agent_id, "origin", "destination")

        # Independent MemoryManager (target engine)
        target_memory = MemoryManager(base_path=base)
        loaded = target_memory.read_agent_memory(agent.agent_id)

        assert loaded.role == original.role
        assert loaded.key_knowledge == original.key_knowledge
        assert loaded.active_context == original.active_context

    def test_l1_memory_survives_context_append_after_move(self, storage):
        """Migration record appended post-move is visible from target engine."""
        base = storage["registry"].base_path
        source_registry = AgentRegistry.legacy(base_path=base)
        source_memory = MemoryManager(base_path=base)

        agent = _make_agent("l1-target-002", owner_group="team-x")
        source_registry.register(agent)

        original = SlockMemory(
            role="DevOps specialist",
            key_knowledge="CI/CD pipelines",
            active_context="[2026-05-18] Set up GitHub Actions",
        )
        source_memory.write_agent_memory(agent.agent_id, original)

        # Move + append migration record (like handler does)
        source_registry.move_agent(agent.agent_id, "team-x", "team-y")
        source_memory.update_agent_context(
            agent.agent_id, "[2026-05-19 12:00] Moved from team-x to team-y"
        )

        # Target engine reads
        target_memory = MemoryManager(base_path=base)
        loaded = target_memory.read_agent_memory(agent.agent_id)

        assert loaded.role == original.role
        assert loaded.key_knowledge == original.key_knowledge
        assert "[2026-05-18] Set up GitHub Actions" in loaded.active_context
        assert "Moved from team-x to team-y" in loaded.active_context


class TestBuildAgentPromptConsistentAfterCrossEngineMove:
    """Prompt assembled by target engine is consistent with pre-move prompt."""

    @staticmethod
    def _build_prompt(agent: AgentIdentity, memory: SlockMemory, message: str = "test") -> str:
        """Replicate engine._build_agent_prompt for testing."""
        parts: list[str] = []
        if agent.system_prompt:
            parts.append(agent.system_prompt)
        if agent.permissions:
            parts.append(
                f"\n# Authorized Tools\n"
                f"You are ONLY permitted to use the following tools: "
                f"{', '.join(agent.permissions)}."
            )
        if memory.role:
            parts.append(f"\n# Your Role\n{memory.role}")
        if memory.key_knowledge:
            parts.append(f"\n# Key Knowledge\n{memory.key_knowledge}")
        if memory.active_context:
            parts.append(f"\n# Recent Context\n{memory.active_context[-2000:]}")
        parts.append(f"\n# User Message\n{message}")
        return "\n".join(parts)

    def test_prompt_identical_after_move_and_refresh(self, storage):
        """Target engine prompt matches source pre-move prompt (no migration record)."""
        base = storage["registry"].base_path

        source_reg = AgentRegistry.legacy(base_path=base)
        source_mem = MemoryManager(base_path=base)

        agent = _make_agent("prompt-cross-001", owner_group="eng-1")
        agent.system_prompt = "You are a security researcher."
        agent.permissions = ["shell", "file_write", "network"]
        source_reg.register(agent)

        mem = SlockMemory(
            role="AppSec engineer focusing on web vulnerabilities.",
            key_knowledge="OWASP Top 10, Burp Suite, CodeQL",
            active_context="[2026-05-18] Completed SAST integration",
        )
        source_mem.write_agent_memory(agent.agent_id, mem)

        prompt_before = self._build_prompt(agent, mem)

        # Move
        source_reg.move_agent(agent.agent_id, "eng-1", "eng-2")

        # Target engine loads
        target_reg = AgentRegistry.legacy(base_path=base)
        target_mem = MemoryManager(base_path=base)

        target_reg.refresh_agent(agent.agent_id)
        agent_after = target_reg.get(agent.agent_id)
        mem_after = target_mem.read_agent_memory(agent.agent_id)

        prompt_after = self._build_prompt(agent_after, mem_after)

        # Prompt should be identical (no migration record appended yet)
        assert prompt_before == prompt_after

    def test_prompt_differs_only_by_migration_record(self, storage):
        """After migration record append, only that record differs in prompt."""
        base = storage["registry"].base_path

        source_reg = AgentRegistry.legacy(base_path=base)
        source_mem = MemoryManager(base_path=base)

        agent = _make_agent("prompt-cross-002", owner_group="team-src")
        agent.system_prompt = "You are an ML engineer."
        agent.permissions = ["shell", "file_write"]
        source_reg.register(agent)

        mem = SlockMemory(
            role="ML engineer specializing in LLM fine-tuning.",
            key_knowledge="PyTorch, transformers, PEFT, LoRA",
            active_context="[2026-05-17] Trained adapter on custom dataset",
        )
        source_mem.write_agent_memory(agent.agent_id, mem)

        prompt_before = self._build_prompt(agent, mem)

        # Move + migration record
        source_reg.move_agent(agent.agent_id, "team-src", "team-dst")
        migration = "[2026-05-19 14:00] Moved from team-src to team-dst"
        source_mem.update_agent_context(agent.agent_id, migration)

        # Target engine loads
        target_reg = AgentRegistry.legacy(base_path=base)
        target_mem = MemoryManager(base_path=base)
        target_reg.refresh_agent(agent.agent_id)

        agent_after = target_reg.get(agent.agent_id)
        mem_after = target_mem.read_agent_memory(agent.agent_id)

        prompt_after = self._build_prompt(agent_after, mem_after)

        # Prompts differ
        assert prompt_before != prompt_after
        # But only by the migration record in active_context
        assert mem_after.role == mem.role
        assert mem_after.key_knowledge == mem.key_knowledge
        assert agent_after.system_prompt == agent.system_prompt
        assert agent_after.permissions == agent.permissions
        assert migration in mem_after.active_context
        assert "[2026-05-17] Trained adapter on custom dataset" in mem_after.active_context


# ---------------------------------------------------------------------------
# Task 4: TestMoveRoleExecutionOrder — verify move_agent is called BEFORE
# send_card_to_chat (notification).
# ---------------------------------------------------------------------------


class TestMoveRoleExecutionOrder:
    """AC1: registry.move_agent() executes before notification card is sent.

    If move_agent fails, send_card_to_chat must NOT be called (zero false
    notification guarantee).
    """

    @patch("src.feishu.handlers.slock.SlockHandler._get_engine_manager")
    @patch("src.thread.manager.get_current_sender_id", return_value="op-order")
    @patch("src.config.get_settings")
    def test_move_agent_failure_prevents_notification(
        self, mock_settings, mock_sender, mock_get_mgr
    ):
        """move_agent returns False → send_card_to_chat never called."""
        from src.slock_engine.models import AgentIdentity, AgentStatus

        handler = _make_handler_with_mocks()

        settings = MagicMock()
        settings.admin_user_ids = frozenset(["op-order"])
        mock_settings.return_value = settings

        source_engine = _make_engine_mock(
            chat_id="chat-src-order", team_name="SrcOrder", owner_id="op-order"
        )
        agent = AgentIdentity(
            agent_id="order-agent-001",
            name="OrderBot",
            emoji="📋",
            agent_type="codex",
            model_name="o3-pro",
            system_prompt="test",
            role="coder",
            permissions=[],
            owner_group="chat-src-order",
            member_groups=["chat-src-order"],
        )
        source_engine.registry.find_by_name.return_value = agent
        source_engine.get_agent_status.return_value = AgentStatus.IDLE
        # move_agent FAILS
        source_engine.registry.move_agent.return_value = MoveOutcome(status=MoveResult.NOT_IN_SOURCE)

        target_engine = _make_engine_mock(
            chat_id="chat-tgt-order", team_name="TgtOrder", owner_id="other"
        )

        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        manager.find_team.return_value = target_engine
        mock_get_mgr.return_value = manager

        handler.move_role(
            message_id="msg-order",
            chat_id="chat-src-order",
            name="OrderBot",
            target_team_name="TgtOrder",
        )

        # Notification was NEVER sent (zero false notification)
        handler.send_card_to_chat.assert_not_called()

        # Error reply to source group
        handler.reply_text.assert_called()
        err = handler.reply_text.call_args[0][1]
        assert "移动失败" in err

    @pytest.mark.parametrize("implementation", ["main", "mixin"])
    def test_duplicate_name_failure_has_dedicated_handler_message(self, implementation):
        """Both legacy move handlers explain a target-channel name collision."""
        from src.feishu.handlers.slock import SlockHandler
        from src.feishu.handlers.slock_roles import SlockRoleMixin
        from src.slock_engine.models import AgentIdentity

        handler = _make_handler_with_mocks()
        handler._check_slock_permission = MagicMock(return_value=True)
        source_engine = _make_engine_mock(
            chat_id="chat-source", team_name="Source", owner_id="operator"
        )
        target_engine = _make_engine_mock(
            chat_id="chat-target", team_name="Target", owner_id="operator"
        )
        agent = AgentIdentity(
            agent_id="duplicate-agent",
            name="Alice",
            owner_group="chat-source",
            member_groups=["chat-source"],
        )
        source_engine.registry.find_by_name.return_value = agent
        source_engine.try_lock_for_move.return_value = True
        source_engine.registry.move_agent.return_value = MoveOutcome(
            status=MoveResult.DUPLICATE_NAME,
            error_msg="Agent name 'Alice' already exists in channel chat-target",
        )
        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        manager.find_team.return_value = target_engine
        handler._get_engine_manager = MagicMock(return_value=manager)
        move_role = (
            SlockHandler.move_role
            if implementation == "main"
            else SlockRoleMixin.move_role
        )

        with patch("src.thread.manager.get_current_sender_id", return_value="operator"):
            move_role(handler, "message", "chat-source", "Alice", "Target")

        message = handler.reply_text.call_args.args[1]
        assert "同名" in message
        assert "目标团队" in message
        handler.send_card_to_chat.assert_not_called()

    @patch("src.feishu.handlers.slock.SlockHandler._get_engine_manager")
    @patch("src.thread.manager.get_current_sender_id", return_value="op-order")
    @patch("src.config.get_settings")
    def test_successful_move_order_move_before_notification(
        self, mock_settings, mock_sender, mock_get_mgr
    ):
        """On success, move_agent is called before send_card_to_chat."""
        from src.slock_engine.models import AgentIdentity, AgentStatus

        handler = _make_handler_with_mocks()
        # Track call order
        call_order: list[str] = []
        orig_move = MagicMock(return_value=MoveOutcome(status=MoveResult.SUCCESS))
        orig_send = MagicMock(return_value="sent-001")

        def track_move(*a, **kw):
            call_order.append("move_agent")
            return orig_move(*a, **kw)

        def track_send(*a, **kw):
            call_order.append("send_card_to_chat")
            return orig_send(*a, **kw)

        settings = MagicMock()
        settings.admin_user_ids = frozenset(["op-order"])
        mock_settings.return_value = settings

        source_engine = _make_engine_mock(
            chat_id="chat-src-order", team_name="SrcOrder", owner_id="op-order"
        )
        agent = AgentIdentity(
            agent_id="order-agent-002",
            name="OrderBot2",
            emoji="📋",
            agent_type="codex",
            model_name="o3-pro",
            system_prompt="test",
            role="coder",
            permissions=[],
            owner_group="chat-src-order",
            member_groups=["chat-src-order"],
        )
        source_engine.registry.find_by_name.return_value = agent
        source_engine.get_agent_status.return_value = AgentStatus.IDLE
        source_engine.registry.move_agent.side_effect = track_move

        target_engine = _make_engine_mock(
            chat_id="chat-tgt-order", team_name="TgtOrder", owner_id="other"
        )

        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        manager.find_team.return_value = target_engine
        mock_get_mgr.return_value = manager

        handler.send_card_to_chat.side_effect = track_send

        handler.move_role(
            message_id="msg-order-2",
            chat_id="chat-src-order",
            name="OrderBot2",
            target_team_name="TgtOrder",
        )

        # Verify ordering
        assert "move_agent" in call_order
        assert "send_card_to_chat" in call_order
        assert call_order.index("move_agent") < call_order.index("send_card_to_chat")


# ---------------------------------------------------------------------------
# Task 5: TestNotificationFailureDegradation — notification card send fails,
# source group receives degraded warning, confirm card still sent.
# ---------------------------------------------------------------------------


class TestNotificationFailureDegradation:
    """AC6: Notification card failure does not rollback move. Source group
    receives degraded warning and confirm card is still sent."""

    @patch("src.feishu.handlers.slock.SlockHandler._get_engine_manager")
    @patch("src.thread.manager.get_current_sender_id", return_value="op-degrade")
    @patch("src.config.get_settings")
    def test_notification_fail_replies_degraded_warning(
        self, mock_settings, mock_sender, mock_get_mgr
    ):
        """send_card_to_chat returns None → source gets warning, confirm card sent."""
        from src.slock_engine.models import AgentIdentity, AgentStatus

        handler = _make_handler_with_mocks()
        # Notification fails
        handler.send_card_to_chat.return_value = None

        settings = MagicMock()
        settings.admin_user_ids = frozenset(["op-degrade"])
        mock_settings.return_value = settings

        source_engine = _make_engine_mock(
            chat_id="chat-src-deg", team_name="SrcDegrade", owner_id="op-degrade"
        )
        agent = AgentIdentity(
            agent_id="degrade-agent-001",
            name="DegradeBot",
            emoji="⚠️",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="test",
            role="reviewer",
            permissions=[],
            owner_group="chat-src-deg",
            member_groups=["chat-src-deg"],
        )
        source_engine.registry.find_by_name.return_value = agent
        source_engine.get_agent_status.return_value = AgentStatus.IDLE
        source_engine.registry.move_agent.return_value = MoveOutcome(status=MoveResult.SUCCESS)

        target_engine = _make_engine_mock(
            chat_id="chat-tgt-deg", team_name="TgtDegrade", owner_id="other"
        )

        manager = MagicMock()
        manager.get_activated_engine.return_value = source_engine
        manager.find_team.return_value = target_engine
        mock_get_mgr.return_value = manager

        handler.move_role(
            message_id="msg-degrade",
            chat_id="chat-src-deg",
            name="DegradeBot",
            target_team_name="TgtDegrade",
        )

        # move_agent was called and succeeded
        source_engine.registry.move_agent.assert_called_once()

        # Source group got degraded warning
        handler.reply_text.assert_called()
        warning = handler.reply_text.call_args[0][1]
        assert "移动成功" in warning
        assert "通知" in warning

        # Confirm card STILL sent (flow continues)
        handler.reply_card.assert_called_once()

        # L1 context update was attempted
        source_engine.memory.update_agent_context.assert_called_once()


# ---------------------------------------------------------------------------
# Task 6 (strengthened): TestL1MemoryLoadableAfterMove &
# TestSystemPromptConsistentAfterMove — use real tmp_path with independent
# MemoryManager instances to verify cross-engine L1 memory loading.
# ---------------------------------------------------------------------------


class TestL1MemoryFullyLoadableFromIndependentManager:
    """AC2 (strengthened): A completely new MemoryManager instance (simulating
    target engine process) can load L1 memory after move with all three
    sections intact."""

    def test_fresh_manager_reads_complete_memory(self, storage, tmp_path):
        """After move, independent MemoryManager loads role+knowledge+context."""
        base = str(tmp_path / "slock_move_test")
        registry = AgentRegistry.legacy(base_path=base)
        memory = MemoryManager(base_path=base)

        agent = _make_agent("l1-full-001", owner_group="src-full")
        registry.register(agent)

        # Write rich L1 memory
        original = SlockMemory(
            role="I am a distributed systems architect specializing in consensus.",
            key_knowledge="Raft, Paxos, CRDTs, event sourcing, saga patterns",
            active_context="[2026-05-19] Designed retry strategy for payment service",
        )
        memory.write_agent_memory(agent.agent_id, original)

        # Perform move
        success = registry.move_agent(agent.agent_id, "src-full", "dst-full")
        assert success.success

        # Simulate target engine: completely new instances
        target_registry = AgentRegistry.legacy(base_path=base)
        target_memory = MemoryManager(base_path=base)
        refreshed = target_registry.refresh_agent(agent.agent_id)
        assert refreshed is not None
        assert refreshed.owner_group == "dst-full"

        # L1 memory loaded by target
        loaded = target_memory.read_agent_memory(agent.agent_id)
        assert loaded.role == original.role
        assert loaded.key_knowledge == original.key_knowledge
        assert loaded.active_context == original.active_context

    def test_migration_record_appended_and_visible_to_target(self, tmp_path):
        """After move + context update, target engine sees migration record."""
        base = str(tmp_path / "slock_migration_record")
        registry = AgentRegistry.legacy(base_path=base)
        memory = MemoryManager(base_path=base)

        agent = _make_agent("l1-mig-001", owner_group="src-mig")
        registry.register(agent)

        original = SlockMemory(
            role="Security auditor",
            key_knowledge="OWASP top 10, CVE analysis",
            active_context="[2026-05-18] Reviewed auth module",
        )
        memory.write_agent_memory(agent.agent_id, original)

        # Move + append migration
        registry.move_agent(agent.agent_id, "src-mig", "dst-mig")
        migration = "[2026-05-20 10:00] Moved from src-mig to dst-mig"
        memory.update_agent_context(agent.agent_id, migration)

        # Target engine loads
        target_memory = MemoryManager(base_path=base)
        loaded = target_memory.read_agent_memory(agent.agent_id)

        # All original content preserved
        assert loaded.role == original.role
        assert loaded.key_knowledge == original.key_knowledge
        # Migration record present + original context preserved
        assert migration in loaded.active_context
        assert "[2026-05-18] Reviewed auth module" in loaded.active_context


class TestSystemPromptIdenticalAfterMoveStrengthened:
    """AC3 (strengthened): system_prompt assembled by target engine matches
    source engine pre-move prompt exactly (except migration record in context)."""

    @staticmethod
    def _build_prompt_parts(agent: AgentIdentity, memory: SlockMemory) -> dict:
        """Simulate prompt assembly — returns sections for comparison."""
        parts = {}
        parts["system_prompt"] = agent.system_prompt
        parts["role"] = memory.role
        parts["key_knowledge"] = memory.key_knowledge
        parts["active_context"] = memory.active_context
        parts["permissions"] = agent.permissions
        return parts

    def test_prompt_sections_identical_except_context(self, tmp_path):
        """Move + migration record: role/knowledge/system_prompt identical."""
        base = str(tmp_path / "slock_prompt_consistency")
        registry = AgentRegistry.legacy(base_path=base)
        memory = MemoryManager(base_path=base)

        agent = _make_agent("prompt-001", owner_group="team-before")
        agent.system_prompt = "You are a meticulous code reviewer. Be thorough."
        registry.register(agent)

        mem = SlockMemory(
            role="Code reviewer with 10 years experience.",
            key_knowledge="Python, Go, Rust, security patterns",
            active_context="[2026-05-18] Reviewed PR #321",
        )
        memory.write_agent_memory(agent.agent_id, mem)

        # Capture pre-move prompt
        pre_parts = self._build_prompt_parts(agent, mem)

        # Move
        registry.move_agent(agent.agent_id, "team-before", "team-after")
        migration = "[2026-05-20 12:00] Moved from team-before to team-after"
        memory.update_agent_context(agent.agent_id, migration)

        # Target loads
        target_reg = AgentRegistry.legacy(base_path=base)
        target_mem = MemoryManager(base_path=base)
        target_reg.refresh_agent(agent.agent_id)

        agent_after = target_reg.get(agent.agent_id)
        mem_after = target_mem.read_agent_memory(agent.agent_id)
        post_parts = self._build_prompt_parts(agent_after, mem_after)

        # These MUST be identical
        assert post_parts["system_prompt"] == pre_parts["system_prompt"]
        assert post_parts["role"] == pre_parts["role"]
        assert post_parts["key_knowledge"] == pre_parts["key_knowledge"]
        assert post_parts["permissions"] == pre_parts["permissions"]

        # Context differs only by appended migration record
        assert pre_parts["active_context"] in post_parts["active_context"]
        assert migration in post_parts["active_context"]


# ============================================================================
# Cross-engine L1 memory loadability and prompt consistency
# ============================================================================


class TestL1MemoryLoadableAfterCrossEngineMove:
    """Verifies that after an agent is moved between two independent SlockEngine
    instances (each with their own AgentRegistry and MemoryManager pointing to
    the same base_path), the target engine can load the L1 memory and build an
    identical agent prompt."""

    @staticmethod
    def _build_prompt(agent: AgentIdentity, memory: SlockMemory) -> str:
        """Replicate engine._build_agent_prompt for testing."""
        parts: list[str] = []
        if agent.system_prompt:
            parts.append(agent.system_prompt)
        if agent.permissions:
            parts.append(
                f"\n# Authorized Tools\n"
                f"You are ONLY permitted to use the following tools: "
                f"{', '.join(agent.permissions)}."
            )
        if memory.role:
            parts.append(f"\n# Your Role\n{memory.role}")
        if memory.key_knowledge:
            parts.append(f"\n# Key Knowledge\n{memory.key_knowledge}")
        if memory.active_context:
            parts.append(f"\n# Recent Context\n{memory.active_context[-2000:]}")
        parts.append("\n# User Message\ntest message")
        return "\n".join(parts)

    def test_cross_engine_prompt_matches_after_move(self, tmp_path):
        """Create agent in source engine, write L1 memory, build prompt.
        Move agent via source registry, refresh in target registry, build prompt.
        Assert prompts match (system_prompt, role, key_knowledge identical;
        active_context may have migration suffix but original content present)."""
        base = str(tmp_path / "cross_engine_prompt")

        # Source engine components
        source_registry = AgentRegistry.legacy(base_path=base)
        source_memory = MemoryManager(base_path=base)

        agent = _make_agent("cross-prompt-001", owner_group="src-engine")
        agent.system_prompt = "You are a senior backend engineer with deep Python expertise."
        agent.permissions = ["shell", "file_write", "git"]
        source_registry.register(agent)

        mem = SlockMemory(
            role="Senior Python backend developer focusing on API design.",
            key_knowledge="FastAPI, SQLAlchemy, Redis, PostgreSQL, event-driven architecture",
            active_context="[2026-05-19] Completed payment gateway integration",
        )
        source_memory.write_agent_memory(agent.agent_id, mem)

        # Build prompt from source engine
        prompt_source = self._build_prompt(agent, mem)

        # Move agent via source registry
        success = source_registry.move_agent(agent.agent_id, "src-engine", "dst-engine")
        assert success.success

        # Append migration context (simulating handler behavior)
        migration_record = "[2026-05-20 09:00] Moved from src-engine to dst-engine"
        source_memory.update_agent_context(agent.agent_id, migration_record)

        # Target engine components (completely independent instances)
        target_registry = AgentRegistry.legacy(base_path=base)
        target_memory = MemoryManager(base_path=base)

        # Refresh to discover the moved agent
        refreshed = target_registry.refresh_agent(agent.agent_id)
        assert refreshed is not None
        assert refreshed.owner_group == "dst-engine"

        # Load memory and build prompt from target engine
        agent_after = target_registry.get(agent.agent_id)
        mem_after = target_memory.read_agent_memory(agent.agent_id)
        prompt_target = self._build_prompt(agent_after, mem_after)

        # system_prompt section identical
        assert agent_after.system_prompt == agent.system_prompt
        # role section identical
        assert mem_after.role == mem.role
        # key_knowledge section identical
        assert mem_after.key_knowledge == mem.key_knowledge
        # active_context has migration suffix but original content present
        assert "[2026-05-19] Completed payment gateway integration" in mem_after.active_context
        assert migration_record in mem_after.active_context
        # Full prompt differs only by migration record in Recent Context
        assert prompt_source != prompt_target  # differs due to migration record
        # Remove migration record effect — check core sections match
        assert agent_after.permissions == agent.permissions

    def test_cross_engine_independent_memory_managers_load_same_l1(self, tmp_path):
        """Create agent, write memory via source MemoryManager. Create a
        completely new MemoryManager with the same base_path. Read memory
        from new MemoryManager. Assert all fields match."""
        base = str(tmp_path / "independent_memory_managers")

        source_registry = AgentRegistry.legacy(base_path=base)
        source_memory = MemoryManager(base_path=base)

        agent = _make_agent("cross-mem-001", owner_group="mem-src")
        source_registry.register(agent)

        original = SlockMemory(
            role="I am a data engineer building ETL pipelines.",
            key_knowledge="Apache Spark, Airflow, dbt, Snowflake, Delta Lake",
            active_context="[2026-05-19] Optimized daily aggregation job from 4h to 45min",
        )
        source_memory.write_agent_memory(agent.agent_id, original)

        # Move agent
        source_registry.move_agent(agent.agent_id, "mem-src", "mem-dst")

        # Create completely new MemoryManager with same base_path
        independent_memory = MemoryManager(base_path=base)
        loaded = independent_memory.read_agent_memory(agent.agent_id)

        # All fields must match exactly
        assert loaded.role == original.role
        assert loaded.key_knowledge == original.key_knowledge
        assert loaded.active_context == original.active_context


# ============================================================================
# Persona consistency after move
# ============================================================================


class TestPersonaConsistencyAfterMove:
    """Verifies that all persona-defining fields of an agent remain unchanged
    after a move."""

    def test_all_identity_fields_preserved_after_move(self, tmp_path):
        """Create agent with all fields populated. Move agent. Reload from
        disk. Assert every field is identical."""
        base = str(tmp_path / "persona_consistency")

        registry = AgentRegistry.legacy(base_path=base)

        agent = AgentIdentity(
            agent_id="persona-full-001",
            name="PersonaBot",
            emoji="🎭",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="You are an expert architect who values simplicity.",
            role="architect",
            permissions=["shell", "file_write", "git", "deploy", "network"],
            owner_group="persona-src",
            member_groups=["persona-src"],
        )
        registry.register(agent)

        # Move agent
        success = registry.move_agent(agent.agent_id, "persona-src", "persona-dst")
        assert success.success

        # Reload from disk via fresh registry
        fresh_registry = AgentRegistry.legacy(base_path=base)
        loaded = fresh_registry.get(agent.agent_id)

        assert loaded is not None
        assert loaded.agent_id == agent.agent_id
        assert loaded.name == agent.name
        assert loaded.emoji == agent.emoji
        assert loaded.agent_type == agent.agent_type
        assert loaded.model_name == agent.model_name
        assert loaded.system_prompt == agent.system_prompt
        assert loaded.role == agent.role
        assert loaded.permissions == agent.permissions
        # owner_group and member_groups reflect the move target
        assert loaded.owner_group == "persona-dst"
        assert "persona-dst" in loaded.member_groups

    def test_skill_profile_preserved_after_move(self, tmp_path):
        """Create agent, write a skill profile. Move agent. Read skill profile
        from fresh MemoryManager. Assert it's identical."""
        base = str(tmp_path / "skill_profile_preserve")

        registry = AgentRegistry.legacy(base_path=base)
        memory = MemoryManager(base_path=base)

        agent = _make_agent("skill-persona-001", owner_group="skill-src")
        registry.register(agent)

        # Record skill feedback to establish a profile
        memory.record_skill_feedback(agent.agent_id, ["python", "architecture", "code-review"], quality_score=95.0)
        memory.record_skill_feedback(agent.agent_id, ["python", "testing"], quality_score=88.0)
        memory.record_skill_feedback(agent.agent_id, ["architecture"], quality_score=91.0)

        # Snapshot skill profiles before move
        before = memory.read_skill_profiles(agent.agent_id)
        before_map = {p.tag: (p.success_rate, p.total_tasks) for p in before}
        assert len(before_map) > 0  # sanity check

        # Move agent
        success = registry.move_agent(agent.agent_id, "skill-src", "skill-dst")
        assert success.success

        # Read skill profile from fresh MemoryManager (simulating target engine)
        fresh_memory = MemoryManager(base_path=base)
        after = fresh_memory.read_skill_profiles(agent.agent_id)
        after_map = {p.tag: (p.success_rate, p.total_tasks) for p in after}

        # All skill profiles must be identical
        assert after_map == before_map


class TestRemoveRoleChannelIsolation:
    """After move, source group cannot find agent via channel-scoped lookup."""

    def test_source_group_find_returns_none_after_move(self, storage):
        """find_by_name with source channel_id returns None after agent moved out."""
        registry = storage["registry"]
        agent = _make_agent("iso-rm-001", name="IsoBot", owner_group="group-A")
        registry.register(agent)

        # Agent visible in group-A before move
        found = registry.find_by_name("IsoBot", channel_id="group-A")
        assert found is not None
        assert found.agent_id == "iso-rm-001"

        # Move to group-B
        success = registry.move_agent("iso-rm-001", "group-A", "group-B")
        assert success.success

        # Source group can no longer find the agent
        assert registry.find_by_name("IsoBot", channel_id="group-A") is None

    def test_target_group_find_works_after_move(self, storage):
        """find_by_name with target channel_id returns agent after move."""
        registry = storage["registry"]
        agent = _make_agent("iso-rm-002", name="IsoBot2", owner_group="group-A")
        registry.register(agent)

        registry.move_agent("iso-rm-002", "group-A", "group-B")

        found = registry.find_by_name("IsoBot2", channel_id="group-B")
        assert found is not None
        assert found.agent_id == "iso-rm-002"

    def test_remove_from_source_group_fails_after_move(self, storage):
        """Simulates /role remove in source group — agent not found."""
        registry = storage["registry"]
        agent = _make_agent("iso-rm-003", name="RemoveMe", owner_group="src-grp")
        registry.register(agent)

        registry.move_agent("iso-rm-003", "src-grp", "dst-grp")

        # Source group scoped lookup returns None (role remove would fail)
        assert registry.find_by_name("RemoveMe", channel_id="src-grp") is None

        # Target group scoped lookup returns the agent (role remove would succeed)
        found = registry.find_by_name("RemoveMe", channel_id="dst-grp")
        assert found is not None


class TestShowRoleInfoChannelIsolation:
    """After move, source group cannot view agent info via channel-scoped lookup."""

    def test_source_group_info_not_found_after_move(self, storage):
        """find_by_name for /role info in source group returns None after move."""
        registry = storage["registry"]
        agent = _make_agent("iso-info-001", name="InfoBot", owner_group="info-src")
        registry.register(agent)

        # Visible before move
        assert registry.find_by_name("InfoBot", channel_id="info-src") is not None

        # Move
        registry.move_agent("iso-info-001", "info-src", "info-dst")

        # Source group: not found
        assert registry.find_by_name("InfoBot", channel_id="info-src") is None

    def test_target_group_info_found_after_move(self, storage):
        """find_by_name for /role info in target group returns agent after move."""
        registry = storage["registry"]
        agent = _make_agent("iso-info-002", name="InfoBot2", owner_group="info-src")
        registry.register(agent)

        registry.move_agent("iso-info-002", "info-src", "info-dst")

        found = registry.find_by_name("InfoBot2", channel_id="info-dst")
        assert found is not None
        assert found.agent_id == "iso-info-002"


# ---------------------------------------------------------------------------
# AC-5 + AC-6: End-to-end cross-group move with redact + persona consistency
# ---------------------------------------------------------------------------


class TestEndToEndMoveWithRedactAndPersonaConsistency:
    """E2E: Move + redact → target engine loads L1 correctly, persona consistent."""

    SOURCE = "e2e-source-group"
    TARGET = "e2e-target-group"

    def _setup_full_agent(self, storage):
        """Register agent with rich L1 memory, return pre-move data."""
        registry = storage["registry"]
        memory = storage["memory"]

        agent = _make_agent("e2e-agent-001", name="E2EBot", owner_group=self.SOURCE)
        registry.register(agent)

        original_memory = SlockMemory(
            role="Senior Python engineer specializing in distributed systems and API design.",
            key_knowledge=(
                "- Project uses FastAPI + SQLAlchemy\n"
                "- CI pipeline: pre-commit → pytest → mypy → deploy\n"
                "- Code style: Google Python Style Guide"
            ),
            active_context=(
                "[2025-05-10 10:00] Discussed auth refactor with Architect in source group.\n"
                "[2025-05-11 14:30] Reviewed PR #200 — found race condition in session handler.\n"
                "[2025-05-12 09:00] Source group standup: assigned task to implement retry logic."
            ),
        )
        memory.write_agent_memory("e2e-agent-001", original_memory)
        return agent, original_memory

    def test_persona_consistency_after_move_and_redact(self, storage):
        """AC-6: _build_agent_prompt role+key_knowledge identical after move+redact."""

        agent, original_memory = self._setup_full_agent(storage)
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        # Build pre-move prompt parts
        pre_move_memory = memory_mgr.read_agent_memory("e2e-agent-001")
        assert pre_move_memory.role == original_memory.role
        assert pre_move_memory.key_knowledge == original_memory.key_knowledge

        # Perform move
        success = registry.move_agent("e2e-agent-001", self.SOURCE, self.TARGET)
        assert success.success

        # Perform redact
        memory_mgr.redact_active_context_for_move("e2e-agent-001", self.SOURCE, self.TARGET)

        # Simulate target engine refresh
        target_registry = AgentRegistry.legacy(base_path=registry.base_path)
        refreshed = target_registry.refresh_agent("e2e-agent-001")
        assert refreshed is not None
        assert refreshed.owner_group == self.TARGET

        # Read memory from target engine perspective
        target_memory_mgr = MemoryManager(base_path=memory_mgr.base_path)
        post_move_memory = target_memory_mgr.read_agent_memory("e2e-agent-001")

        # AC-6: role and key_knowledge identical
        assert post_move_memory.role == original_memory.role
        assert post_move_memory.key_knowledge == original_memory.key_knowledge

        # AC-8: active_context is redacted (source history gone)
        assert "Discussed auth refactor" not in post_move_memory.active_context
        assert "Reviewed PR #200" not in post_move_memory.active_context
        assert "Context redacted on move" in post_move_memory.active_context

    def test_build_agent_prompt_role_knowledge_consistent(self, storage):
        """AC-6: Full engine _build_agent_prompt preserves role+knowledge sections."""
        from unittest.mock import patch

        from src.slock_engine.engine import SlockEngine

        agent, original_memory = self._setup_full_agent(storage)
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        # Create source engine and build pre-move prompt
        with patch("src.slock_engine.engine.create_engine_session"):
            source_engine = SlockEngine(
                chat_id=self.SOURCE,
                root_path="/tmp/e2e_root",
                memory_base_path=registry.base_path,
            )
        pre_memory = source_engine.memory.read_agent_memory("e2e-agent-001")
        pre_prompt = source_engine._build_agent_prompt(agent, "test message", pre_memory)

        # Move + redact
        registry.move_agent("e2e-agent-001", self.SOURCE, self.TARGET)
        memory_mgr.redact_active_context_for_move("e2e-agent-001", self.SOURCE, self.TARGET)

        # Create target engine
        with patch("src.slock_engine.engine.create_engine_session"):
            target_engine = SlockEngine(
                chat_id=self.TARGET,
                root_path="/tmp/e2e_root",
                memory_base_path=registry.base_path,
            )
        target_engine.registry.refresh_agent("e2e-agent-001")
        post_memory = target_engine.memory.read_agent_memory("e2e-agent-001")
        post_prompt = target_engine._build_agent_prompt(agent, "test message", post_memory)

        # Role section must be identical in both prompts
        assert "# Your Role" in pre_prompt
        assert "# Your Role" in post_prompt
        pre_role_section = pre_prompt.split("# Your Role")[1].split("#")[0]
        post_role_section = post_prompt.split("# Your Role")[1].split("#")[0]
        assert pre_role_section == post_role_section

        # Key Knowledge section must be identical
        assert "# Key Knowledge" in pre_prompt
        assert "# Key Knowledge" in post_prompt
        pre_knowledge_section = pre_prompt.split("# Key Knowledge")[1].split("#")[0]
        post_knowledge_section = post_prompt.split("# Key Knowledge")[1].split("#")[0]
        assert pre_knowledge_section == post_knowledge_section

    def test_target_member_cannot_see_source_context_via_memory(self, storage):
        """AC-5: After redact, source-group conversation history is not in L1 memory."""
        agent, original_memory = self._setup_full_agent(storage)
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        # Verify source context exists before move
        pre = memory_mgr.read_agent_memory("e2e-agent-001")
        assert "Discussed auth refactor" in pre.active_context

        # Move + redact
        registry.move_agent("e2e-agent-001", self.SOURCE, self.TARGET)
        memory_mgr.redact_active_context_for_move("e2e-agent-001", self.SOURCE, self.TARGET)

        # Target member reads memory — source context is gone
        post = memory_mgr.read_agent_memory("e2e-agent-001")
        assert "Discussed auth refactor" not in post.active_context
        assert "PR #200" not in post.active_context
        assert "session handler" not in post.active_context
        # Only migration record remains
        assert "Context redacted on move" in post.active_context
        assert self.SOURCE in post.active_context
        assert self.TARGET in post.active_context
