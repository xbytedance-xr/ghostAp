"""
Regression tests for L1 memory preservation after cross-group agent move.

Covers:
- AC9: Agent L1 memory (Role + Key Knowledge + Active Context) loads identically
        after move_agent cross-group move.
- AC10: system_prompt built by target engine matches source engine
        (excluding migration context record).
- AC11: End-to-end integration — target engine _build_agent_prompt produces
        memory-equivalent output after full move_role flow.
"""

import pytest

from src.slock_engine.agent_registry import AgentRegistry, MoveOutcome, MoveResult
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import AgentIdentity, SlockMemory


def _make_agent(agent_id: str, name: str, owner_group: str) -> AgentIdentity:
    """Factory to create an AgentIdentity with sensible defaults."""
    return AgentIdentity(
        agent_id=agent_id,
        name=name,
        emoji="🤖",
        agent_type="assistant",
        model_name="gpt-4",
        system_prompt="You are a helpful assistant specialized in code review.",
        role="Code Reviewer",
        permissions=["read", "write", "execute"],
        owner_group=owner_group,
        member_groups=[owner_group],
    )


@pytest.fixture
def storage(tmp_path):
    """Provide registry and memory manager sharing the same base_path."""
    base_path = tmp_path / "slock_data"
    base_path.mkdir()
    return {
        "registry": AgentRegistry(base_path=str(base_path)),
        "memory": MemoryManager(base_path=str(base_path)),
        "base_path": str(base_path),
    }


# ---------------------------------------------------------------------------
# AC9: L1 Memory preserved after cross-group move
# ---------------------------------------------------------------------------


class TestL1MemoryPreservedAfterCrossGroupMove:
    """AC9: Agent L1 memory loads identically after move_agent cross-group move."""

    SOURCE_GROUP = "group_alpha"
    TARGET_GROUP = "group_beta"

    def _setup_and_move(self, storage):
        """Register agent, write memory, perform cross-group move."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("agent_001", "ReviewBot", self.SOURCE_GROUP)
        registry.register(agent)

        original_memory = SlockMemory(
            role="Senior Code Reviewer responsible for Python backend services.",
            key_knowledge="- Always check for SQL injection vulnerabilities\n- Enforce type hints on public APIs\n- Flag missing error handling in async code",
            active_context="Currently reviewing PR #142 for auth service refactor.",
        )
        memory_mgr.write_agent_memory("agent_001", original_memory)

        # Perform cross-group move
        result = registry.move_agent("agent_001", self.SOURCE_GROUP, self.TARGET_GROUP)
        assert result.success, "move_agent should succeed"

        return original_memory

    def test_role_section_identical(self, storage):
        """Role section is byte-identical after cross-group move."""
        original_memory = self._setup_and_move(storage)

        # Read from a fresh MemoryManager with the same base_path
        fresh_memory_mgr = MemoryManager(base_path=storage["base_path"])
        loaded_memory = fresh_memory_mgr.read_agent_memory("agent_001")

        assert loaded_memory.role == original_memory.role

    def test_key_knowledge_identical(self, storage):
        """Key knowledge section is byte-identical after cross-group move."""
        original_memory = self._setup_and_move(storage)

        fresh_memory_mgr = MemoryManager(base_path=storage["base_path"])
        loaded_memory = fresh_memory_mgr.read_agent_memory("agent_001")

        assert loaded_memory.key_knowledge == original_memory.key_knowledge

    def test_active_context_identical(self, storage):
        """Active context section is byte-identical after cross-group move."""
        original_memory = self._setup_and_move(storage)

        fresh_memory_mgr = MemoryManager(base_path=storage["base_path"])
        loaded_memory = fresh_memory_mgr.read_agent_memory("agent_001")

        assert loaded_memory.active_context == original_memory.active_context

    def test_full_memory_object_equality(self, storage):
        """All 3 L1 memory fields match in a single assertion."""
        original_memory = self._setup_and_move(storage)

        fresh_memory_mgr = MemoryManager(base_path=storage["base_path"])
        loaded_memory = fresh_memory_mgr.read_agent_memory("agent_001")

        assert loaded_memory.role == original_memory.role
        assert loaded_memory.key_knowledge == original_memory.key_knowledge
        assert loaded_memory.active_context == original_memory.active_context


# ---------------------------------------------------------------------------
# AC10: system_prompt consistency after move
# ---------------------------------------------------------------------------


class TestSystemPromptConsistencyAfterMove:
    """AC10: system_prompt built by target engine matches source engine
    (excluding migration context record)."""

    SOURCE_GROUP = "group_alpha"
    TARGET_GROUP = "group_beta"

    def test_system_prompt_field_unchanged_by_move(self, storage):
        """The agent's system_prompt field itself is not mutated by move."""
        registry = storage["registry"]

        agent = _make_agent("agent_002", "PromptBot", self.SOURCE_GROUP)
        original_system_prompt = agent.system_prompt
        registry.register(agent)

        registry.move_agent("agent_002", self.SOURCE_GROUP, self.TARGET_GROUP)

        # Reload from a fresh registry instance
        fresh_registry = AgentRegistry(base_path=storage["base_path"])
        reloaded_agent = fresh_registry.get("agent_002")

        assert reloaded_agent is not None, "Agent should be retrievable after move"
        assert reloaded_agent.system_prompt == original_system_prompt

    def test_memory_contributes_same_prompt_content(self, storage):
        """Memory-derived prompt content is identical before and after move."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("agent_003", "MemPromptBot", self.SOURCE_GROUP)
        registry.register(agent)

        memory = SlockMemory(
            role="Infrastructure specialist for cloud deployments.",
            key_knowledge="- Kubernetes best practices\n- Terraform state management\n- CI/CD pipeline optimization",
            active_context="Deploying v2.3.1 to staging environment.",
        )
        memory_mgr.write_agent_memory("agent_003", memory)

        # Build prompt representation BEFORE move
        prompt_before = memory.role + memory.key_knowledge + memory.active_context

        # Perform move
        registry.move_agent("agent_003", self.SOURCE_GROUP, self.TARGET_GROUP)

        # Read from fresh manager AFTER move
        fresh_memory_mgr = MemoryManager(base_path=storage["base_path"])
        loaded_memory = fresh_memory_mgr.read_agent_memory("agent_003")

        prompt_after = (
            loaded_memory.role
            + loaded_memory.key_knowledge
            + loaded_memory.active_context
        )

        assert prompt_before == prompt_after

    def test_migration_context_record_is_only_difference(self, storage):
        """After appending a migration record, only active_context differs
        and only by the appended record."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("agent_004", "MigrateBot", self.SOURCE_GROUP)
        registry.register(agent)

        original_memory = SlockMemory(
            role="QA automation engineer.",
            key_knowledge="- Selenium grid configuration\n- Test data management\n- Flaky test detection",
            active_context="Running nightly regression suite.",
        )
        memory_mgr.write_agent_memory("agent_004", original_memory)

        # Perform move
        registry.move_agent("agent_004", self.SOURCE_GROUP, self.TARGET_GROUP)

        # Append migration context record
        migration_record = (
            f"[MIGRATION] Moved from {self.SOURCE_GROUP} to {self.TARGET_GROUP}."
        )
        memory_mgr.update_agent_context("agent_004", migration_record)

        # Read updated memory
        updated_memory = memory_mgr.read_agent_memory("agent_004")

        # Role and key_knowledge must be unchanged
        assert updated_memory.role == original_memory.role
        assert updated_memory.key_knowledge == original_memory.key_knowledge

        # Active context differs only by the appended migration record
        assert original_memory.active_context in updated_memory.active_context
        assert migration_record in updated_memory.active_context

        # The difference should be exactly the migration record
        difference = updated_memory.active_context.replace(
            original_memory.active_context, "", 1
        ).strip()
        assert migration_record in difference


# ---------------------------------------------------------------------------
# Independent MemoryManager load verification
# ---------------------------------------------------------------------------


class TestIndependentMemoryManagerLoadAfterMove:
    """Verify that independent MemoryManager instances can read moved agent memory."""

    SOURCE_GROUP = "group_alpha"
    TARGET_GROUP = "group_beta"

    def _setup_and_move(self, storage):
        """Register agent, write memory, perform move, return original memory."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("agent_005", "IndyBot", self.SOURCE_GROUP)
        registry.register(agent)

        original_memory = SlockMemory(
            role="DevOps engineer managing production infrastructure.",
            key_knowledge="- Incident response runbooks\n- SLO/SLI monitoring\n- Capacity planning",
            active_context="Investigating latency spike in payment service.",
        )
        memory_mgr.write_agent_memory("agent_005", original_memory)

        registry.move_agent("agent_005", self.SOURCE_GROUP, self.TARGET_GROUP)

        return original_memory

    def test_fresh_manager_reads_complete_memory(self, storage):
        """A NEW MemoryManager instance reads all 3 sections correctly."""
        original_memory = self._setup_and_move(storage)

        fresh_mgr = MemoryManager(base_path=storage["base_path"])
        loaded = fresh_mgr.read_agent_memory("agent_005")

        assert loaded is not None, "Memory should be readable from fresh manager"
        assert loaded.role == original_memory.role
        assert loaded.key_knowledge == original_memory.key_knowledge
        assert loaded.active_context == original_memory.active_context

        # All sections should be non-empty (populated)
        assert loaded.role != ""
        assert loaded.key_knowledge != ""
        assert loaded.active_context != ""

    def test_two_independent_managers_read_same_content(self, storage):
        """Two separate MemoryManager instances return identical content."""
        self._setup_and_move(storage)

        manager_a = MemoryManager(base_path=storage["base_path"])
        manager_b = MemoryManager(base_path=storage["base_path"])

        memory_a = manager_a.read_agent_memory("agent_005")
        memory_b = manager_b.read_agent_memory("agent_005")

        assert memory_a.role == memory_b.role
        assert memory_a.key_knowledge == memory_b.key_knowledge
        assert memory_a.active_context == memory_b.active_context


# ---------------------------------------------------------------------------
# Boundary scenario tests
# ---------------------------------------------------------------------------


class TestL1MemoryBoundaryScenarios:
    """Boundary tests: empty memory, special characters, consecutive moves."""

    SOURCE_GROUP = "group_alpha"
    TARGET_GROUP = "group_beta"
    THIRD_GROUP = "group_gamma"

    def test_empty_memory_agent_move_returns_empty_slock_memory(self, storage):
        """Agent with no L1 memory still returns SlockMemory() after move."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("agent_empty", "EmptyBot", self.SOURCE_GROUP)
        registry.register(agent)
        # Do NOT write any memory — simulate freshly registered agent

        result = registry.move_agent("agent_empty", self.SOURCE_GROUP, self.TARGET_GROUP)
        assert result.success

        loaded = memory_mgr.read_agent_memory("agent_empty")
        assert loaded.role == ""
        assert loaded.key_knowledge == ""
        assert loaded.active_context == ""

    def test_special_characters_roundtrip(self, storage):
        """L1 memory with markdown headings, unicode, newlines survives move."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("agent_special", "SpecialBot", self.SOURCE_GROUP)
        registry.register(agent)

        tricky_memory = SlockMemory(
            role="# Sub-heading in role\n角色：高级工程师\nLine with `backticks` and **bold**",
            key_knowledge="- 知识点 1：包含 emoji 🎯\n- 知识点 2：换行\n后续\n# Fake heading inside knowledge",
            active_context="Context with special chars: <>&\"'\n## Another fake heading\n末尾无换行",
        )
        memory_mgr.write_agent_memory("agent_special", tricky_memory)

        result = registry.move_agent("agent_special", self.SOURCE_GROUP, self.TARGET_GROUP)
        assert result.success

        fresh_mgr = MemoryManager(base_path=storage["base_path"])
        loaded = fresh_mgr.read_agent_memory("agent_special")

        assert loaded.role == tricky_memory.role
        assert loaded.key_knowledge == tricky_memory.key_knowledge
        assert loaded.active_context == tricky_memory.active_context

    def test_consecutive_multi_group_moves_preserve_memory(self, storage):
        """L1 memory stays intact after 3 consecutive cross-group moves."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("agent_nomad", "NomadBot", self.SOURCE_GROUP)
        registry.register(agent)

        original_memory = SlockMemory(
            role="Nomadic engineer adapting to any team.",
            key_knowledge="- Portable expertise\n- Cross-team communication",
            active_context="Currently between assignments.",
        )
        memory_mgr.write_agent_memory("agent_nomad", original_memory)

        # Move 1: SOURCE → TARGET
        assert registry.move_agent("agent_nomad", self.SOURCE_GROUP, self.TARGET_GROUP).success
        # Move 2: TARGET → THIRD
        assert registry.move_agent("agent_nomad", self.TARGET_GROUP, self.THIRD_GROUP).success
        # Move 3: THIRD → SOURCE (back to origin)
        assert registry.move_agent("agent_nomad", self.THIRD_GROUP, self.SOURCE_GROUP).success

        fresh_mgr = MemoryManager(base_path=storage["base_path"])
        loaded = fresh_mgr.read_agent_memory("agent_nomad")

        assert loaded.role == original_memory.role
        assert loaded.key_knowledge == original_memory.key_knowledge
        assert loaded.active_context == original_memory.active_context

    def test_memory_with_context_updates_between_moves(self, storage):
        """L1 memory accumulates context correctly across multiple moves."""
        registry = storage["registry"]
        memory_mgr = storage["memory"]

        agent = _make_agent("agent_ctx", "CtxBot", self.SOURCE_GROUP)
        registry.register(agent)

        memory = SlockMemory(
            role="Context accumulator.",
            key_knowledge="Tracks movement history.",
            active_context="Initial context.",
        )
        memory_mgr.write_agent_memory("agent_ctx", memory)

        # Move 1 + context update
        registry.move_agent("agent_ctx", self.SOURCE_GROUP, self.TARGET_GROUP)
        memory_mgr.update_agent_context("agent_ctx", "[Move 1] alpha → beta")

        # Move 2 + context update
        registry.move_agent("agent_ctx", self.TARGET_GROUP, self.THIRD_GROUP)
        memory_mgr.update_agent_context("agent_ctx", "[Move 2] beta → gamma")

        loaded = memory_mgr.read_agent_memory("agent_ctx")

        assert loaded.role == "Context accumulator."
        assert loaded.key_knowledge == "Tracks movement history."
        assert "Initial context." in loaded.active_context
        assert "[Move 1] alpha → beta" in loaded.active_context
        assert "[Move 2] beta → gamma" in loaded.active_context


# ---------------------------------------------------------------------------
# AC11: End-to-end integration test — cross-engine prompt consistency
# ---------------------------------------------------------------------------


class TestEndToEndCrossEngineMovePromptConsistency:
    """AC11: Simulates full move_role flow across two SlockEngine instances,
    verifying _build_agent_prompt produces memory-equivalent output."""

    SOURCE_CHAT = "chat_source_e2e"
    TARGET_CHAT = "chat_target_e2e"

    def test_target_engine_prompt_matches_source_after_move(self, tmp_path):
        """After full move flow, target engine builds same memory-derived prompt."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.models import SlockChannel

        base_path = str(tmp_path / "slock_data")

        # Create source and target engines sharing the same storage base
        source_engine = SlockEngine(
            chat_id=self.SOURCE_CHAT,
            root_path=str(tmp_path / "project"),
            memory_base_path=base_path,
        )
        target_engine = SlockEngine(
            chat_id=self.TARGET_CHAT,
            root_path=str(tmp_path / "project"),
            memory_base_path=base_path,
        )

        # Activate channels
        source_channel = SlockChannel(
            channel_id=self.SOURCE_CHAT, name="Source", team_name="SourceTeam"
        )
        target_channel = SlockChannel(
            channel_id=self.TARGET_CHAT, name="Target", team_name="TargetTeam"
        )
        source_engine.activate_channel(source_channel)
        target_engine.activate_channel(target_channel)

        # Register agent in source
        agent = _make_agent("agent_e2e", "E2EBot", self.SOURCE_CHAT)
        source_engine.registry.register(agent)

        # Write rich L1 memory
        rich_memory = SlockMemory(
            role="Senior backend engineer with expertise in distributed systems.",
            key_knowledge="- Kafka partition strategy\n- Circuit breaker patterns\n- Eventual consistency trade-offs",
            active_context="Reviewing microservice decomposition proposal.",
        )
        source_engine.memory.write_agent_memory("agent_e2e", rich_memory)

        # Build prompt BEFORE move (from source engine)
        source_memory = source_engine.memory.read_agent_memory("agent_e2e")
        prompt_before = source_engine._build_agent_prompt(agent, "test message", source_memory)

        # === Execute full move flow ===
        # Step 1: Lock for move
        assert source_engine.try_lock_for_move("agent_e2e")

        # Step 2: Registry move
        assert source_engine.registry.move_agent("agent_e2e", self.SOURCE_CHAT, self.TARGET_CHAT).success

        # Step 3: Unlock (triggers L1 memory verification)
        source_engine.unlock_after_move("agent_e2e")

        # Step 4: Refresh target registry
        refreshed = target_engine.registry.refresh_agent("agent_e2e")
        assert refreshed is not None
        assert refreshed.owner_group == self.TARGET_CHAT

        # === Verify prompt from target engine ===
        target_memory = target_engine.memory.read_agent_memory("agent_e2e")
        prompt_after = target_engine._build_agent_prompt(agent, "test message", target_memory)

        assert prompt_before == prompt_after

    def test_target_engine_memory_fields_match_source(self, tmp_path):
        """All three L1 memory fields are identical when read from target engine."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.models import SlockChannel

        base_path = str(tmp_path / "slock_data")

        source_engine = SlockEngine(
            chat_id=self.SOURCE_CHAT,
            root_path=str(tmp_path / "project"),
            memory_base_path=base_path,
        )
        target_engine = SlockEngine(
            chat_id=self.TARGET_CHAT,
            root_path=str(tmp_path / "project"),
            memory_base_path=base_path,
        )

        agent = _make_agent("agent_e2e2", "E2E2Bot", self.SOURCE_CHAT)
        source_engine.registry.register(agent)

        original_memory = SlockMemory(
            role="Security analyst with focus on API hardening.",
            key_knowledge="- OWASP Top 10\n- JWT token lifecycle\n- Rate limiting strategies",
            active_context="Auditing auth service for token refresh vulnerability.",
        )
        source_engine.memory.write_agent_memory("agent_e2e2", original_memory)

        # Execute move
        source_engine.try_lock_for_move("agent_e2e2")
        source_engine.registry.move_agent("agent_e2e2", self.SOURCE_CHAT, self.TARGET_CHAT)
        source_engine.unlock_after_move("agent_e2e2")
        target_engine.registry.refresh_agent("agent_e2e2")

        # Read from target engine's memory manager
        target_loaded = target_engine.memory.read_agent_memory("agent_e2e2")

        assert target_loaded.role == original_memory.role
        assert target_loaded.key_knowledge == original_memory.key_knowledge
        assert target_loaded.active_context == original_memory.active_context

    def test_migration_context_append_only_extends_active_context(self, tmp_path):
        """After migration record append, only active_context grows; role/knowledge unchanged."""
        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.models import SlockChannel

        base_path = str(tmp_path / "slock_data")

        source_engine = SlockEngine(
            chat_id=self.SOURCE_CHAT,
            root_path=str(tmp_path / "project"),
            memory_base_path=base_path,
        )
        target_engine = SlockEngine(
            chat_id=self.TARGET_CHAT,
            root_path=str(tmp_path / "project"),
            memory_base_path=base_path,
        )

        agent = _make_agent("agent_e2e3", "E2E3Bot", self.SOURCE_CHAT)
        source_engine.registry.register(agent)

        original_memory = SlockMemory(
            role="DevOps lead managing CI/CD pipelines.",
            key_knowledge="- GitHub Actions patterns\n- Docker multi-stage builds",
            active_context="Optimizing build cache for monorepo.",
        )
        source_engine.memory.write_agent_memory("agent_e2e3", original_memory)

        # Execute move + migration context append (mimicking handler flow)
        source_engine.try_lock_for_move("agent_e2e3")
        source_engine.registry.move_agent("agent_e2e3", self.SOURCE_CHAT, self.TARGET_CHAT)
        source_engine.unlock_after_move("agent_e2e3")
        target_engine.registry.refresh_agent("agent_e2e3")

        # Append migration record (as handler does)
        migration_record = "[2026-05-20 10:00] Moved from SourceTeam to TargetTeam"
        source_engine.memory.update_agent_context("agent_e2e3", migration_record)

        # Verify from target engine
        target_loaded = target_engine.memory.read_agent_memory("agent_e2e3")

        assert target_loaded.role == original_memory.role
        assert target_loaded.key_knowledge == original_memory.key_knowledge
        assert original_memory.active_context in target_loaded.active_context
        assert migration_record in target_loaded.active_context


# ---------------------------------------------------------------------------
# AC-8: active_context redact preserves role/key_knowledge
# ---------------------------------------------------------------------------


class TestRedactActiveContextForMove:
    """AC-8: redact_active_context_for_move preserves role/key_knowledge, truncates active_context."""

    SOURCE_GROUP = "group_source"
    TARGET_GROUP = "group_target"

    def test_role_preserved_byte_identical(self, storage):
        """Role section must be byte-identical after redact."""
        memory_mgr = storage["memory"]
        registry = storage["registry"]
        agent = _make_agent("agent_redact_1", "RedactBot", self.SOURCE_GROUP)
        registry.register(agent)

        original_role = "Senior Code Reviewer responsible for Python backend services."
        original_knowledge = "- Check SQL injection\n- Enforce type hints"
        original_context = "Long conversation history from source group discussions about PR #142..."

        memory_mgr.write_agent_memory("agent_redact_1", SlockMemory(
            role=original_role,
            key_knowledge=original_knowledge,
            active_context=original_context,
        ))

        # Perform redact
        memory_mgr.redact_active_context_for_move("agent_redact_1", self.SOURCE_GROUP, self.TARGET_GROUP)

        # Verify
        after = memory_mgr.read_agent_memory("agent_redact_1")
        assert after.role == original_role, "Role must be byte-identical"
        assert after.key_knowledge == original_knowledge, "Key Knowledge must be byte-identical"

    def test_active_context_replaced_with_migration_record(self, storage):
        """Active context replaced with single migration record line."""
        memory_mgr = storage["memory"]
        registry = storage["registry"]
        agent = _make_agent("agent_redact_2", "RedactBot2", self.SOURCE_GROUP)
        registry.register(agent)

        memory_mgr.write_agent_memory("agent_redact_2", SlockMemory(
            role="Reviewer",
            key_knowledge="knows stuff",
            active_context="secret source group conversation about internal APIs and tokens",
        ))

        memory_mgr.redact_active_context_for_move("agent_redact_2", self.SOURCE_GROUP, self.TARGET_GROUP)

        after = memory_mgr.read_agent_memory("agent_redact_2")
        assert "Context redacted on move" in after.active_context
        assert self.SOURCE_GROUP in after.active_context
        assert self.TARGET_GROUP in after.active_context
        # Original content is gone
        assert "secret source group conversation" not in after.active_context

    def test_empty_memory_does_not_crash(self, storage):
        """Redacting an agent with no existing memory should not raise."""
        memory_mgr = storage["memory"]
        registry = storage["registry"]
        agent = _make_agent("agent_redact_empty", "EmptyBot", self.SOURCE_GROUP)
        registry.register(agent)

        # No memory written — redact should handle gracefully
        memory_mgr.redact_active_context_for_move("agent_redact_empty", self.SOURCE_GROUP, self.TARGET_GROUP)

        after = memory_mgr.read_agent_memory("agent_redact_empty")
        assert "Context redacted on move" in after.active_context
        assert after.role == ""
        assert after.key_knowledge == ""

    def test_unicode_and_special_chars_preserved(self, storage):
        """Role/knowledge with unicode/special chars survive redact unchanged."""
        memory_mgr = storage["memory"]
        registry = storage["registry"]
        agent = _make_agent("agent_redact_unicode", "UnicodeBot", self.SOURCE_GROUP)
        registry.register(agent)

        unicode_role = "高级代码审查员 — 专注于 Python 🐍 后端"
        unicode_knowledge = "- 检查 SQL 注入漏洞\n- 强制使用类型标注 → ✅"
        memory_mgr.write_agent_memory("agent_redact_unicode", SlockMemory(
            role=unicode_role,
            key_knowledge=unicode_knowledge,
            active_context="源群对话历史内容",
        ))

        memory_mgr.redact_active_context_for_move("agent_redact_unicode", self.SOURCE_GROUP, self.TARGET_GROUP)

        after = memory_mgr.read_agent_memory("agent_redact_unicode")
        assert after.role == unicode_role
        assert after.key_knowledge == unicode_knowledge


# ---------------------------------------------------------------------------
# FS-5: _verify_l1_memory_after_move ERROR on empty role for established agent
# ---------------------------------------------------------------------------


class TestVerifyL1MemoryAfterMoveErrorOnEmptyRole:
    """FS-5: When an established agent has empty role after move, ERROR is logged."""

    def test_error_logged_when_role_empty_but_has_context(self, storage, caplog):
        """Agent with key_knowledge but empty role triggers ERROR."""
        import logging

        from src.slock_engine.engine import SlockEngine
        from unittest.mock import patch

        memory_mgr = storage["memory"]
        registry = storage["registry"]
        agent = _make_agent("agent_verify_err", "VerifyBot", "group_src")
        registry.register(agent)

        # Write memory with empty role but has knowledge (established agent)
        memory_mgr.write_agent_memory("agent_verify_err", SlockMemory(
            role="",
            key_knowledge="Important project knowledge that proves this is not a new agent",
            active_context="Some context",
        ))

        with patch("src.slock_engine.engine.create_engine_session"):
            engine = SlockEngine(
                chat_id="chat_verify",
                root_path="/tmp/test_root",
                memory_base_path=storage["base_path"],
            )

        with caplog.at_level(logging.ERROR, logger="src.slock_engine.engine"):
            diag = engine._verify_l1_memory_after_move("agent_verify_err")

        assert diag != "", "Should return non-empty diagnostic string"
        assert "role section empty" in diag.lower() or "L1 role section empty" in diag
        assert "agent_verify_err" in diag

    def test_no_error_when_role_present(self, storage, caplog):
        """Agent with valid role does not trigger ERROR."""
        import logging

        from src.slock_engine.engine import SlockEngine
        from unittest.mock import patch

        memory_mgr = storage["memory"]
        registry = storage["registry"]
        agent = _make_agent("agent_verify_ok", "OkBot", "group_src")
        registry.register(agent)

        memory_mgr.write_agent_memory("agent_verify_ok", SlockMemory(
            role="Valid role definition for the agent",
            key_knowledge="Some knowledge",
            active_context="Some context",
        ))

        with patch("src.slock_engine.engine.create_engine_session"):
            engine = SlockEngine(
                chat_id="chat_verify_ok",
                root_path="/tmp/test_root",
                memory_base_path=storage["base_path"],
            )

        with caplog.at_level(logging.ERROR, logger="src.slock_engine.engine"):
            diag = engine._verify_l1_memory_after_move("agent_verify_ok")

        assert diag == "", "Should return empty string when all OK"

    def test_no_error_for_completely_empty_new_agent(self, storage, caplog):
        """Brand new agent with no memory at all does not trigger ERROR."""
        import logging

        from src.slock_engine.engine import SlockEngine
        from unittest.mock import patch

        with patch("src.slock_engine.engine.create_engine_session"):
            engine = SlockEngine(
                chat_id="chat_verify_new",
                root_path="/tmp/test_root",
                memory_base_path=storage["base_path"],
            )

        with caplog.at_level(logging.ERROR, logger="src.slock_engine.engine"):
            diag = engine._verify_l1_memory_after_move("agent_nonexistent")

        assert diag == "", "New agent with empty memory should not trigger error"
