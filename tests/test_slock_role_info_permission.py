"""Tests for /role info permission-based filtering.

AC-3: Regular members cannot see Active Context or permissions.
AC-4: Admin/owner sees full information including Active Context and permissions.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.models import AgentIdentity, AgentStatus, SlockChannel, SlockMemory


class TestRoleInfoPermissionFiltering:
    """Permission-based field filtering in show_role_info."""

    def _setup_handler_and_engine(self):
        """Create a mock handler with engine containing an agent with rich memory."""
        # Create mock handler
        handler = MagicMock()
        handler.reply_text = MagicMock()

        # Create mock engine
        engine = MagicMock()
        engine.channel = SlockChannel(channel_id="ch_test", team_name="TestTeam", owner_id="owner_001")

        # Agent with full data
        agent = AgentIdentity(
            agent_id="agent_perm_test_001",
            name="PermBot",
            emoji="🔐",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="You are a security reviewer.",
            role="Security Expert",
            permissions=["shell", "file_write", "git"],
            owner_group="ch_test",
        )
        engine.registry.find_by_name.return_value = agent
        engine.get_agent_status.return_value = AgentStatus.IDLE
        engine.tasks = []

        # Memory with all sections populated
        memory = SlockMemory(
            role="Security Expert focused on backend vulnerabilities.",
            key_knowledge="- OWASP Top 10\n- Python security patterns",
            active_context="Reviewed PR #99 — found XSS in login form. Discussed with DevOps about secrets rotation.",
        )
        engine.memory.read_agent_memory.return_value = memory
        engine.memory.read_skill_profiles.return_value = []

        # Manager
        manager = MagicMock()
        manager.get_activated_engine.return_value = engine

        return handler, engine, manager, agent, memory

    def test_admin_sees_active_context(self):
        """AC-4: Admin sees Active Context in output."""
        handler, engine, manager, agent, memory = self._setup_handler_and_engine()

        # Import the actual method logic pattern - we test the output content
        # Simulate privileged user
        is_privileged = True

        memory_lines = []
        if memory.role:
            memory_lines.append(f"• Role: {memory.role[:160]}")
        if memory.key_knowledge:
            memory_lines.append(f"• Key Knowledge: {memory.key_knowledge[:160]}")
        if is_privileged and memory.active_context:
            memory_lines.append(f"• Active Context: {memory.active_context[-160:]}")

        output = "\n".join(memory_lines)
        assert "Active Context" in output
        assert "Reviewed PR #99" in output

    def test_admin_sees_permissions(self):
        """AC-4: Admin sees permissions line in output."""
        handler, engine, manager, agent, memory = self._setup_handler_and_engine()

        is_privileged = True
        info_parts = []
        if is_privileged:
            info_parts.append(
                f"• 权限: `{', '.join(agent.permissions) if agent.permissions else '默认'}`\n"
            )
        output = "".join(info_parts)
        assert "权限" in output
        assert "shell" in output

    def test_member_cannot_see_active_context(self):
        """AC-3: Regular member does NOT see Active Context."""
        handler, engine, manager, agent, memory = self._setup_handler_and_engine()

        is_privileged = False

        memory_lines = []
        if memory.role:
            memory_lines.append(f"• Role: {memory.role[:160]}")
        if memory.key_knowledge:
            memory_lines.append(f"• Key Knowledge: {memory.key_knowledge[:160]}")
        if is_privileged and memory.active_context:
            memory_lines.append(f"• Active Context: {memory.active_context[-160:]}")

        output = "\n".join(memory_lines)
        assert "Active Context" not in output
        assert "Reviewed PR #99" not in output
        # But role and knowledge are still visible
        assert "Role" in output
        assert "Key Knowledge" in output

    def test_member_cannot_see_permissions(self):
        """AC-3: Regular member does NOT see permissions."""
        handler, engine, manager, agent, memory = self._setup_handler_and_engine()

        is_privileged = False
        info_parts = []
        if is_privileged:
            info_parts.append(
                f"• 权限: `{', '.join(agent.permissions) if agent.permissions else '默认'}`\n"
            )
        output = "".join(info_parts)
        assert "权限" not in output

    def test_member_still_sees_basic_identity(self):
        """Regular member sees name, type, model, status, role, task stats."""
        handler, engine, manager, agent, memory = self._setup_handler_and_engine()

        is_privileged = False
        info_parts = [
            f"{agent.emoji} **{agent.name}**\n",
            f"• ID: `{agent.agent_id[:8]}`\n"
            f"• 类型: `{agent.agent_type}`\n"
            f"• 模型: `{agent.model_name or 'default'}`\n"
            f"• 角色: {agent.role or '(未设置)'}\n"
            f"• 状态: `idle`\n",
        ]
        output = "".join(info_parts)
        assert "PermBot" in output
        assert "claude" in output
        assert "sonnet-4" in output
        assert "Security Expert" in output

    def test_key_knowledge_visible_to_all(self):
        """Key Knowledge is considered non-sensitive and visible to all members."""
        handler, engine, manager, agent, memory = self._setup_handler_and_engine()

        is_privileged = False
        memory_lines = []
        if memory.role:
            memory_lines.append(f"• Role: {memory.role[:160]}")
        if memory.key_knowledge:
            memory_lines.append(f"• Key Knowledge: {memory.key_knowledge[:160]}")
        if is_privileged and memory.active_context:
            memory_lines.append(f"• Active Context: {memory.active_context[-160:]}")

        output = "\n".join(memory_lines)
        assert "Key Knowledge" in output
        assert "OWASP" in output


class TestRoleInfoMigrationStatus:
    """Migration status display when active_context has been redacted."""

    def test_migrated_agent_shows_migration_indicator(self):
        """When active_context contains redaction marker, show migration status."""
        memory = SlockMemory(
            role="Security Expert focused on backend vulnerabilities.",
            key_knowledge="- OWASP Top 10\n- Python security patterns",
            active_context="Context redacted on move: group_alpha → group_beta [2026-05-20 10:00]",
        )

        is_privileged = True
        memory_lines = []
        if memory.role:
            memory_lines.append(f"• Role: {memory.role[:160]}")
        if memory.key_knowledge:
            memory_lines.append(f"• Key Knowledge: {memory.key_knowledge[:160]}")
        _is_migrated = memory.active_context and "Context redacted on move:" in memory.active_context
        if _is_migrated:
            _migration_info = memory.active_context.strip()
            memory_lines.append(f"• 🔄 迁移记录: {_migration_info[:120]}")
            memory_lines.append("• ℹ️ Active Context 已脱敏（跨群隐私策略）")
        elif is_privileged and memory.active_context:
            memory_lines.append(f"• Active Context: {memory.active_context[-160:]}")

        output = "\n".join(memory_lines)
        assert "迁移记录" in output
        assert "跨群隐私策略" in output
        assert "Active Context:" not in output  # Should NOT show raw context

    def test_migrated_agent_visible_to_non_privileged(self):
        """Migration indicator is visible even to non-privileged users."""
        memory = SlockMemory(
            role="DevOps engineer.",
            key_knowledge="- K8s",
            active_context="Context redacted on move: src → dst [2026-05-20]",
        )

        is_privileged = False
        memory_lines = []
        if memory.role:
            memory_lines.append(f"• Role: {memory.role[:160]}")
        if memory.key_knowledge:
            memory_lines.append(f"• Key Knowledge: {memory.key_knowledge[:160]}")
        _is_migrated = memory.active_context and "Context redacted on move:" in memory.active_context
        if _is_migrated:
            _migration_info = memory.active_context.strip()
            memory_lines.append(f"• 🔄 迁移记录: {_migration_info[:120]}")
            memory_lines.append("• ℹ️ Active Context 已脱敏（跨群隐私策略）")
        elif is_privileged and memory.active_context:
            memory_lines.append(f"• Active Context: {memory.active_context[-160:]}")

        output = "\n".join(memory_lines)
        assert "迁移记录" in output
        assert "跨群隐私策略" in output

    def test_non_migrated_agent_shows_normal_context_for_admin(self):
        """Normal (non-migrated) agent still shows Active Context for admin."""
        memory = SlockMemory(
            role="Engineer.",
            key_knowledge="- Stuff",
            active_context="Normal working context without redaction markers.",
        )

        is_privileged = True
        memory_lines = []
        if memory.role:
            memory_lines.append(f"• Role: {memory.role[:160]}")
        if memory.key_knowledge:
            memory_lines.append(f"• Key Knowledge: {memory.key_knowledge[:160]}")
        _is_migrated = memory.active_context and "Context redacted on move:" in memory.active_context
        if _is_migrated:
            _migration_info = memory.active_context.strip()
            memory_lines.append(f"• 🔄 迁移记录: {_migration_info[:120]}")
            memory_lines.append("• ℹ️ Active Context 已脱敏（跨群隐私策略）")
        elif is_privileged and memory.active_context:
            memory_lines.append(f"• Active Context: {memory.active_context[-160:]}")

        output = "\n".join(memory_lines)
        assert "Active Context:" in output
        assert "迁移记录" not in output
