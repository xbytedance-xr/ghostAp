"""Tests for /memory command and memory management UX — AC-R04, AC-R05.

Verifies:
- /memory is recognized and parsed correctly (AC-R04)
- Memory management card shows edit buttons only when permitted (AC-R05)
- Permission model: admin + owner can edit, others cannot
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.slash_commands import SlockCommandAction, is_slock_command, parse_slock_command


class TestMemoryCommandParsing:
    """AC-R04: /memory 命令解析正确。"""

    def test_parse_memory_with_agent_name(self):
        """/memory @Alice -> MEMORY with target 'Alice'."""
        result = parse_slock_command("/memory @Alice")
        assert result.action == SlockCommandAction.MEMORY
        assert result.target == "Alice"

    def test_parse_memory_without_at_sign(self):
        """/memory Alice -> MEMORY with target 'Alice'."""
        result = parse_slock_command("/memory Alice")
        assert result.action == SlockCommandAction.MEMORY
        assert result.target == "Alice"

    def test_parse_memory_without_target(self):
        """/memory alone -> MEMORY_LIST (shows all agents' memory summary)."""
        result = parse_slock_command("/memory")
        assert result.action == SlockCommandAction.MEMORY_LIST

    def test_parse_memory_list(self):
        """/memory list -> MEMORY_LIST action."""
        result = parse_slock_command("/memory list")
        assert result.action == SlockCommandAction.MEMORY_LIST

    def test_parse_memory_list_case_insensitive(self):
        """/memory LIST -> MEMORY_LIST action (case insensitive)."""
        result = parse_slock_command("/memory LIST")
        assert result.action == SlockCommandAction.MEMORY_LIST

    def test_parse_memory_list_does_not_conflict_with_agent(self):
        """/memory @list_agent -> should still be MEMORY (agent named 'list_agent')."""
        result = parse_slock_command("/memory @list_agent")
        assert result.action == SlockCommandAction.MEMORY
        assert result.target == "list_agent"

    def test_is_slock_command_memory_in_managed_chat(self):
        """/memory is recognized in managed chats."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = True
        assert is_slock_command("/memory Agent", chat_id="c1", manager=manager) is True

    def test_is_slock_command_memory_not_in_unmanaged_chat(self):
        """/memory returns NEEDS_ACTIVATION without managed context."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = False
        assert is_slock_command("/memory Agent", chat_id="c1", manager=manager) == "NEEDS_ACTIVATION"


class TestMemoryManageCard:
    """AC-R05: memory_manage_card 仅对有权限用户显示编辑按钮。"""

    def test_build_memory_manage_card_with_edit(self):
        """Card includes edit buttons when can_edit=True."""
        from src.slock_engine.card_templates import build_memory_manage_card
        from src.slock_engine.models import SlockMemory

        memory = SlockMemory(
            role="Coder role",
            active_context="Working on feature X",
        )
        card = build_memory_manage_card(
            memory=memory,
            agent_name="Alice",
            agent_id="a-001",
            can_edit=True,
            channel_id="ch-001",
        )
        # Verify it's a valid card dict
        assert "body" in card or "elements" in card or "schema" in card
        # Serialize to check button presence
        import json
        card_str = json.dumps(card, ensure_ascii=False)
        assert "slock_memory_clear_context" in card_str

    def test_build_memory_manage_card_without_edit(self):
        """Card hides edit buttons when can_edit=False."""
        from src.slock_engine.card_templates import build_memory_manage_card
        from src.slock_engine.models import SlockMemory

        memory = SlockMemory(
            role="Coder role",
            active_context="Working on feature X",
        )
        card = build_memory_manage_card(
            memory=memory,
            agent_name="Alice",
            agent_id="a-001",
            can_edit=False,
            channel_id="ch-001",
        )
        import json
        card_str = json.dumps(card, ensure_ascii=False)
        assert "slock_memory_clear_context" not in card_str


class TestMemoryEditPermission:
    """AC-R05: 权限模型 — admin_user_ids + owner_id。"""

    def _make_handler_with_engine(self, owner_id="owner-1", admin_ids=None):
        """Create a mock handler for testing permission."""
        # We test the permission check logic directly
        from src.feishu.handlers.slock import SlockHandler

        handler = MagicMock(spec=SlockHandler)
        handler._check_memory_edit_permission = SlockHandler._check_memory_edit_permission.__get__(handler)

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.owner_id = owner_id

        return handler, engine

    def test_admin_has_permission(self):
        """Admin users can edit memory."""
        handler, engine = self._make_handler_with_engine()
        with patch("src.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(admin_user_ids=["admin-1"])
            assert handler._check_memory_edit_permission(engine, "admin-1") is True

    def test_owner_has_permission(self):
        """Channel owner can edit memory."""
        handler, engine = self._make_handler_with_engine(owner_id="owner-1")
        with patch("src.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(admin_user_ids=[])
            assert handler._check_memory_edit_permission(engine, "owner-1") is True

    def test_random_user_no_permission(self):
        """Non-admin, non-owner users cannot edit memory."""
        handler, engine = self._make_handler_with_engine(owner_id="owner-1")
        with patch("src.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(admin_user_ids=["admin-1"])
            assert handler._check_memory_edit_permission(engine, "random-user") is False

    def test_empty_operator_no_permission(self):
        """Empty operator_id has no permission."""
        handler, engine = self._make_handler_with_engine()
        assert handler._check_memory_edit_permission(engine, "") is False
