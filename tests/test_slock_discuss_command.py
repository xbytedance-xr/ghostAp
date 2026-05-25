"""Tests for /discuss command parsing — AC-R01.

Verifies:
- /discuss is recognized as a slock command in managed chats
- parse_slock_command produces DISCUSSION action with topic args
- /discuss without topic still produces DISCUSSION action (empty args)
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.slock_engine.slash_commands import SlockCommandAction, is_slock_command, parse_slock_command


class TestDiscussCommandParsing:
    """AC-R01: /discuss 命令必须被正确解析并路由。"""

    def test_parse_discuss_with_topic(self):
        """'/discuss 讨论API设计' -> DISCUSSION action with topic."""
        result = parse_slock_command("/discuss 讨论API设计")
        assert result.action == SlockCommandAction.DISCUSSION
        assert result.args == "讨论API设计"

    def test_parse_discuss_without_topic(self):
        """'/discuss' alone -> DISCUSSION_LIST (shows active discussions)."""
        result = parse_slock_command("/discuss")
        assert result.action == SlockCommandAction.DISCUSSION_LIST

    def test_parse_discuss_with_multiword_topic(self):
        """'/discuss how should we handle auth?' -> full topic preserved."""
        result = parse_slock_command("/discuss how should we handle auth?")
        assert result.action == SlockCommandAction.DISCUSSION
        assert result.args == "how should we handle auth?"

    def test_is_slock_command_discuss_in_managed_chat(self):
        """/discuss is recognized in managed chats."""
        manager = MagicMock()
        manager.is_managed_chat.return_value = True
        assert is_slock_command("/discuss topic", chat_id="chat1", manager=manager)

    def test_is_slock_command_discuss_not_in_unmanaged_chat(self):
        """/discuss returns NEEDS_ACTIVATION without managed chat context."""
        from src.slock_engine.slash_commands import NEEDS_ACTIVATION
        manager = MagicMock()
        manager.is_managed_chat.return_value = False
        assert is_slock_command("/discuss topic", chat_id="chat1", manager=manager) == NEEDS_ACTIVATION

    def test_is_slock_command_discuss_no_manager(self):
        """/discuss returns False when no manager context is available."""
        assert not is_slock_command("/discuss topic", chat_id="chat1", manager=None)

    def test_parse_discuss_list(self):
        """/discuss list -> DISCUSSION_LIST action."""
        result = parse_slock_command("/discuss list")
        assert result.action == SlockCommandAction.DISCUSSION_LIST

    def test_parse_discuss_list_case_insensitive(self):
        """/discuss LIST -> DISCUSSION_LIST action (case insensitive)."""
        result = parse_slock_command("/discuss LIST")
        assert result.action == SlockCommandAction.DISCUSSION_LIST
