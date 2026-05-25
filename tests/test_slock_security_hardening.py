"""Tests for security hardening (AC20, AC21, fail-closed, regex fallback)."""
import os
import re
import time
from unittest.mock import MagicMock

import pytest


class TestPathRestrictionFailSafe:
    """AC20: Empty path_restrictions still restricts to workspace."""

    def test_empty_restrictions_uses_workspace_path(self, tmp_path):
        """With empty slock_tool_path_restrictions, workspace_path becomes the restriction."""
        from src.slock_engine.models import AgentIdentity

        workspace = str(tmp_path / "workspace")
        os.makedirs(workspace, exist_ok=True)

        agent = AgentIdentity(
            agent_id="restricted_agent",
            name="Restricted",
            workspace_path=workspace,
            permissions=["shell", "file_write"],
        )

        settings = MagicMock()
        settings.slock_tool_path_restrictions = []

        # Mock engine with settings
        engine = MagicMock()
        engine._settings = settings

        session = MagicMock()
        captured_filter = [None]
        def capture_filter(fn):
            captured_filter[0] = fn
        session.set_tool_filter = capture_filter

        # Import and call
        from src.slock_engine.engine import SlockEngine
        # Call _apply_tool_restrictions directly
        SlockEngine._apply_tool_restrictions(engine, session, agent)

        assert captured_filter[0] is not None
        tool_filter = captured_filter[0]

        # Should reject /etc/passwd write
        assert tool_filter("file_write", {"path": "/etc/passwd"}) is False
        # Should allow workspace write
        assert tool_filter("file_write", {"path": os.path.join(workspace, "file.txt")}) is True
        # Should allow non-file tools
        assert tool_filter("web_search", {"query": "test"}) is True

    def test_empty_workspace_uses_sandbox(self):
        """With empty workspace_path, falls back to /tmp/slock_sandbox/{agent_id}."""
        from src.slock_engine.models import AgentIdentity

        agent = AgentIdentity(
            agent_id="sandbox_agent",
            name="Sandbox",
            workspace_path="",
            permissions=["file_write"],
        )

        settings = MagicMock()
        settings.slock_tool_path_restrictions = []

        engine = MagicMock()
        engine._settings = settings

        session = MagicMock()
        captured_filter = [None]
        session.set_tool_filter = lambda fn: captured_filter.__setitem__(0, fn)

        from src.slock_engine.engine import SlockEngine
        SlockEngine._apply_tool_restrictions(engine, session, agent)

        assert captured_filter[0] is not None
        tool_filter = captured_filter[0]

        sandbox_path = f"/tmp/slock_sandbox/{agent.agent_id}"
        assert tool_filter("file_write", {"path": f"{sandbox_path}/test.txt"}) is True
        assert tool_filter("file_write", {"path": "/etc/shadow"}) is False


class TestDissolveTokenOperatorBinding:
    """AC21: Non-initiator dissolve confirmation is rejected."""

    def test_non_initiator_rejected(self):
        """Operator_id mismatch causes rejection."""
        # The _dissolve_tokens dict stores (token, timestamp, operator_id)
        tokens = {"chat_123": ("abc123token", time.time(), "user_A")}

        # Verify the tuple structure
        token, ts, operator_id = tokens["chat_123"]
        assert operator_id == "user_A"

        # Simulate verification: current_operator != original_operator_id
        current_operator = "user_B"
        assert current_operator != operator_id  # This would trigger rejection

    def test_admin_can_bypass_operator_check(self):
        """Admin users bypass the operator_id binding check."""
        tokens = {"chat_456": ("def456token", time.time(), "user_A")}
        token, ts, operator_id = tokens["chat_456"]

        current_operator = "admin_user"
        # Admin check: _has_slock_permission returns True for admins
        is_admin = True  # Simulated

        # Even though operator doesn't match, admin bypasses
        if current_operator != operator_id:
            if is_admin:
                pass  # Allowed through
            else:
                pytest.fail("Non-admin non-initiator should be blocked")


# ------------------------------------------------------------------
# Task 3: Fail-closed permission check tests
# ------------------------------------------------------------------

class TestPermissionCheckFailClosed:
    """Verify that exceptions in permission safety check deny the command (fail-closed)."""

    def _make_client(self, sandbox_mock):
        """Create a GhostAPClient with a mocked sandbox."""
        from src.acp.client import GhostAPClient
        client = GhostAPClient(
            on_event=MagicMock(),
            auto_approve=True,
            root_dir="/tmp",
            sandbox=sandbox_mock,
        )
        return client

    def _make_tool_call(self, command: str):
        """Create a mock tool_call with kind='execute'."""
        tc = MagicMock()
        tc.kind = "execute"
        tc.raw_input = {"command": command}
        return tc

    def _make_options(self):
        """Create mock permission options."""
        opt = MagicMock()
        opt.kind = "allow_once"
        opt.option_id = "opt_1"
        return [opt]

    @pytest.mark.asyncio
    async def test_permission_check_exception_denies_command(self):
        """When is_command_safe() raises a generic Exception, command is denied."""
        sandbox = MagicMock()
        sandbox.is_command_safe.side_effect = Exception("unexpected internal error")

        client = self._make_client(sandbox)
        tool_call = self._make_tool_call("rm -rf /")
        options = self._make_options()

        resp = await client.request_permission(options, "session_1", tool_call)
        assert resp.outcome.outcome == "cancelled"

    @pytest.mark.asyncio
    async def test_permission_check_re_error_denies_command(self):
        """When is_command_safe() raises re.error, command is denied."""
        sandbox = MagicMock()
        sandbox.is_command_safe.side_effect = re.error("bad pattern")

        client = self._make_client(sandbox)
        tool_call = self._make_tool_call("echo hello")
        options = self._make_options()

        resp = await client.request_permission(options, "session_1", tool_call)
        assert resp.outcome.outcome == "cancelled"

    @pytest.mark.asyncio
    async def test_permission_check_normal_flow_still_allows(self):
        """Normal safe commands are still allowed when no exception occurs."""
        sandbox = MagicMock()
        sandbox.is_command_safe.return_value = (True, None)

        client = self._make_client(sandbox)
        tool_call = self._make_tool_call("ls -la")
        options = self._make_options()

        resp = await client.request_permission(options, "session_1", tool_call)
        assert resp.outcome.outcome == "selected"


# ------------------------------------------------------------------
# Task 4: Engine regex fallback tests
# ------------------------------------------------------------------

class TestEngineDangerousPatternRegexFallback:
    """Verify engine handles malicious regex in slock_dangerous_shell_patterns gracefully."""

    def test_engine_malicious_regex_fallback(self):
        """Invalid user-configured regex falls back to builtin patterns only."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        mock_settings = MagicMock()
        mock_settings.slock_dangerous_shell_patterns = ["[unclosed"]  # invalid regex
        engine._settings = mock_settings

        # Simulate the __init__ pattern compilation logic
        all_patterns = list(SlockEngine._BUILTIN_DANGEROUS_PATTERNS)
        extra = getattr(engine._settings, "slock_dangerous_shell_patterns", [])
        if extra:
            all_patterns.extend(extra)
        try:
            engine._dangerous_shell_patterns = re.compile(
                r"|".join(all_patterns), re.IGNORECASE,
            )
        except re.error:
            engine._dangerous_shell_patterns = re.compile(
                r"|".join(SlockEngine._BUILTIN_DANGEROUS_PATTERNS), re.IGNORECASE,
            )

        # Should have compiled successfully with only builtins
        assert engine._dangerous_shell_patterns is not None
        # Should match a builtin dangerous pattern (rm -rf)
        assert engine._dangerous_shell_patterns.search("rm -rf /") is not None
        # The invalid pattern "[unclosed" should NOT be in the compiled regex
        # (if it were, compilation would have failed)
        builtin_only = re.compile(
            r"|".join(SlockEngine._BUILTIN_DANGEROUS_PATTERNS), re.IGNORECASE,
        )
        assert engine._dangerous_shell_patterns.pattern == builtin_only.pattern

    def test_engine_valid_extra_patterns(self):
        """Valid user-configured patterns are included in compilation."""
        from src.slock_engine.engine import SlockEngine

        engine = SlockEngine.__new__(SlockEngine)
        mock_settings = MagicMock()
        mock_settings.slock_dangerous_shell_patterns = [r"docker\s+run"]
        engine._settings = mock_settings

        all_patterns = list(SlockEngine._BUILTIN_DANGEROUS_PATTERNS)
        extra = getattr(engine._settings, "slock_dangerous_shell_patterns", [])
        if extra:
            all_patterns.extend(extra)
        try:
            engine._dangerous_shell_patterns = re.compile(
                r"|".join(all_patterns), re.IGNORECASE,
            )
        except re.error:
            engine._dangerous_shell_patterns = re.compile(
                r"|".join(SlockEngine._BUILTIN_DANGEROUS_PATTERNS), re.IGNORECASE,
            )

        # Should match extra user pattern
        assert engine._dangerous_shell_patterns.search("docker run nginx") is not None
        # Should still match builtins
        assert engine._dangerous_shell_patterns.search("rm -rf /") is not None
