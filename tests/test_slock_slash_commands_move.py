"""Tests for /role move command parsing with shlex support."""

from __future__ import annotations

import pytest

from src.slock_engine.slash_commands import parse_slock_command, SlockCommandAction


class TestRoleMoveShlex:
    """AC4: /role move supports quoted multi-word arguments."""

    def test_quoted_agent_and_team(self):
        """Quoted multi-word arguments parsed correctly."""
        cmd = parse_slock_command('/role move "Coder Alpha" "前端团队"')
        assert cmd is not None
        assert cmd.action == SlockCommandAction.ROLE_MOVE
        assert cmd.target == "Coder Alpha"
        assert cmd.args == "前端团队"

    def test_unquoted_single_words(self):
        """Simple single-word arguments still work (backward compat)."""
        cmd = parse_slock_command("/role move coder backend")
        assert cmd is not None
        assert cmd.action == SlockCommandAction.ROLE_MOVE
        assert cmd.target == "coder"
        assert cmd.args == "backend"

    def test_multi_word_agent_single_word_team(self):
        """Quoted agent name with unquoted team name."""
        cmd = parse_slock_command('/role move "Coder Alpha" backend')
        assert cmd is not None
        assert cmd.action == SlockCommandAction.ROLE_MOVE
        assert cmd.target == "Coder Alpha"
        assert cmd.args == "backend"

    def test_single_word_agent_quoted_team(self):
        """Unquoted agent with quoted team name."""
        cmd = parse_slock_command('/role move coder "前端团队"')
        assert cmd is not None
        assert cmd.action == SlockCommandAction.ROLE_MOVE
        assert cmd.target == "coder"
        assert cmd.args == "前端团队"

    def test_unclosed_quote_returns_empty(self):
        """Unclosed quote results in empty target/args for caller to show usage."""
        cmd = parse_slock_command('/role move "Coder Alpha 前端团队')
        assert cmd is not None
        assert cmd.action == SlockCommandAction.ROLE_MOVE
        assert cmd.target == ""
        assert cmd.args == ""

    def test_only_agent_name_no_team(self):
        """Only agent name provided, target team is empty."""
        cmd = parse_slock_command("/role move coder")
        assert cmd is not None
        assert cmd.action == SlockCommandAction.ROLE_MOVE
        assert cmd.target == "coder"
        assert cmd.args == ""

    def test_empty_args(self):
        """No arguments at all."""
        cmd = parse_slock_command("/role move")
        assert cmd is not None
        assert cmd.action == SlockCommandAction.ROLE_MOVE
        assert cmd.target == ""
        assert cmd.args == ""

    def test_three_word_agent_name_with_quotes(self):
        """Three-word agent name in quotes."""
        cmd = parse_slock_command('/role move "Senior Coder Alpha" "研发团队"')
        assert cmd is not None
        assert cmd.target == "Senior Coder Alpha"
        assert cmd.args == "研发团队"
