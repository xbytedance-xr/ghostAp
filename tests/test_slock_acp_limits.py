"""Tests for ACP token limits and path restrictions (AC22, AC23)."""

import asyncio
import inspect
import os
import sys
import types
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Mock external 'acp' SDK package (not installed in this test environment).
# We inject synthetic modules into sys.modules so the import chain for
# src.acp.session / src.acp.client resolves without the external dep.
# ---------------------------------------------------------------------------
_EXT_ACP_SUBMODULES = ("acp", "acp.interfaces", "acp.schema", "acp.helpers", "acp.stdio")
for _mod_name in _EXT_ACP_SUBMODULES:
    if _mod_name not in sys.modules:
        _stub = types.ModuleType(_mod_name)
        _stub.__package__ = "acp"
        if _mod_name == "acp":
            _stub.__path__ = []  # marks it as a package
        _stub.__dict__.setdefault("__all__", [])
        sys.modules[_mod_name] = _stub

# Populate attributes that source code imports from external acp
_acp_iface = sys.modules["acp.interfaces"]
_acp_iface.Agent = type("Agent", (), {})
_acp_iface.Client = type("Client", (), {})

_acp_schema = sys.modules["acp.schema"]
_acp_schema.PromptResponse = MagicMock
_acp_schema.__getattr__ = lambda name: MagicMock

_acp_helpers = sys.modules["acp.helpers"]
_acp_helpers.text_block = MagicMock(side_effect=lambda t: {"type": "text", "text": t})

_acp_stdio = sys.modules["acp.stdio"]
_acp_stdio.spawn_agent_process = MagicMock()

# ---------------------------------------------------------------------------


class TestACPTokenLimits:
    """AC22: ACP session passes max_tokens in slock discussion calls."""

    def test_prompt_accepts_max_tokens_kwarg(self):
        """AC22: prompt() method accepts max_tokens parameter."""
        from src.acp.session import ACPSession

        sig = inspect.signature(ACPSession.prompt)
        assert "max_tokens" in sig.parameters
        param = sig.parameters["max_tokens"]
        # It should be a keyword-only argument with default None
        assert param.default is None
        assert param.kind == inspect.Parameter.KEYWORD_ONLY

    def test_slock_max_tokens_per_round_default(self):
        """AC22: slock_max_tokens_per_round defaults to 8000."""
        from src.config.settings import Settings

        field_info = Settings.model_fields["slock_max_tokens_per_round"]
        assert field_info.default == 8000

    def test_prompt_passes_max_tokens_to_conn(self):
        """AC22: prompt() passes max_tokens kwarg through to _conn.prompt()."""
        from src.acp.session import ACPSession

        session = ACPSession.__new__(ACPSession)
        # Minimal internal state setup
        import threading

        session._agent_cmd = "test"
        session._session_id = "sess-123"
        session._state = MagicMock()
        session._state.message_count = 0
        session._event_handler = None
        session._handler_lock = threading.Lock()

        # Mock _conn with async prompt method
        mock_conn = MagicMock()
        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        mock_response.usage = None
        mock_conn.prompt = AsyncMock(return_value=mock_response)
        session._conn = mock_conn
        session._proc = MagicMock(returncode=None)

        # Patch ACPHistoryStore to avoid filesystem access
        with patch("src.acp.session.ACPHistoryStore") as mock_store_cls:
            mock_store_cls.return_value.load.return_value = []
            asyncio.run(session.prompt("test message", max_tokens=8000))

        # Verify that _conn.prompt was called with max_tokens=8000
        mock_conn.prompt.assert_called_once()
        call_kwargs = mock_conn.prompt.call_args
        assert call_kwargs.kwargs.get("max_tokens") == 8000

    def test_prompt_omits_max_tokens_when_none(self):
        """AC22: prompt() does NOT pass max_tokens when it is None (backward compat)."""
        from src.acp.session import ACPSession

        import threading

        session = ACPSession.__new__(ACPSession)
        session._agent_cmd = "test"
        session._session_id = "sess-456"
        session._state = MagicMock()
        session._state.message_count = 0
        session._event_handler = None
        session._handler_lock = threading.Lock()

        mock_conn = MagicMock()
        mock_response = MagicMock()
        mock_response.stop_reason = "end_turn"
        mock_response.usage = None
        mock_conn.prompt = AsyncMock(return_value=mock_response)
        session._conn = mock_conn
        session._proc = MagicMock(returncode=None)

        with patch("src.acp.session.ACPHistoryStore") as mock_store_cls:
            mock_store_cls.return_value.load.return_value = []
            asyncio.run(session.prompt("test message"))

        # Verify max_tokens is NOT in the kwargs when None
        call_kwargs = mock_conn.prompt.call_args
        assert "max_tokens" not in (call_kwargs.kwargs or {})


class TestPathRestrictions:
    """AC23: slock_tool_path_restrictions whitelist enforcement."""

    def test_check_path_restriction_allows_whitelisted(self):
        """Paths within whitelist are allowed."""
        from src.acp.client import _check_path_restriction

        assert _check_path_restriction("/workspace/file.py", ["/workspace", "/tmp"]) is True
        assert _check_path_restriction("/tmp/output.txt", ["/workspace", "/tmp"]) is True

    def test_check_path_restriction_denies_non_whitelisted(self):
        """AC23: Paths outside whitelist are denied."""
        from src.acp.client import _check_path_restriction

        assert _check_path_restriction("/etc/passwd", ["/workspace", "/tmp"]) is False
        assert _check_path_restriction("/home/user/.ssh/id_rsa", ["/workspace", "/tmp"]) is False

    def test_check_path_restriction_empty_allows_all(self):
        """Empty restrictions list allows all paths (backward compatible)."""
        from src.acp.client import _check_path_restriction

        assert _check_path_restriction("/etc/passwd", []) is True
        assert _check_path_restriction("/any/path", []) is True

    def test_check_path_restriction_prevents_prefix_attack(self):
        """'/workspace' should not match '/workspaceMalicious'."""
        from src.acp.client import _check_path_restriction

        assert _check_path_restriction("/workspaceMalicious/file", ["/workspace"]) is False

    def test_check_path_restriction_allows_exact_match(self):
        """Exact path match is allowed (edge case: path == prefix)."""
        from src.acp.client import _check_path_restriction

        assert _check_path_restriction("/workspace", ["/workspace"]) is True

    def test_check_path_restriction_allows_subdirectory(self):
        """Subdirectories of whitelisted paths are allowed."""
        from src.acp.client import _check_path_restriction

        assert _check_path_restriction("/workspace/sub/dir/file.txt", ["/workspace"]) is True
        assert _check_path_restriction("/tmp/nested/deep/file", ["/tmp"]) is True

    def test_settings_default_slock_tool_path_restrictions(self):
        """slock_tool_path_restrictions defaults to empty list (allow all)."""
        from src.config.settings import Settings

        field_info = Settings.model_fields["slock_tool_path_restrictions"]
        # default_factory produces an empty list
        assert field_info.default_factory is not None
        assert field_info.default_factory() == []

    def test_write_text_file_blocked_by_restriction(self):
        """AC23: write_text_file returns permission error for restricted paths."""
        from pathlib import Path

        from src.acp.client import GhostAPClient

        client = GhostAPClient(
            on_event=MagicMock(),
            auto_approve=True,
            root_dir="/workspace",
        )

        # Patch _get_tool_path_restrictions to return a restricted list and
        # _safe_resolve_path to simulate a resolved path outside whitelist.
        with patch("src.acp.client._get_tool_path_restrictions", return_value=["/workspace", "/tmp"]):
            with patch("src.acp.client._safe_resolve_path", return_value=Path("/etc/passwd")):
                response = asyncio.run(
                    client.write_text_file(
                        content="malicious",
                        path="/etc/passwd",
                        session_id="test-session",
                    )
                )
                # Should contain error about path restriction
                meta = getattr(response, "field_meta", None) or {}
                assert "error" in meta
                assert "Permission denied" in meta["error"]

    def test_read_text_file_blocked_by_restriction(self):
        """AC23: read_text_file returns error for restricted paths."""
        from pathlib import Path

        from src.acp.client import GhostAPClient

        client = GhostAPClient(
            on_event=MagicMock(),
            auto_approve=True,
            root_dir="/workspace",
        )

        with patch("src.acp.client._get_tool_path_restrictions", return_value=["/workspace", "/tmp"]):
            with patch("src.acp.client._safe_resolve_path", return_value=Path("/etc/passwd")):
                response = asyncio.run(
                    client.read_text_file(
                        path="/etc/passwd",
                        session_id="test-session",
                    )
                )
                meta = getattr(response, "field_meta", None) or {}
                assert "error" in meta
                assert "Permission denied" in meta["error"] or "path restriction" in meta["error"].lower()
