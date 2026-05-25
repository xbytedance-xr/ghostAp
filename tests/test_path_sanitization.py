"""Tests for path sanitization security hardening in slock engine.

Covers:
- MemoryManager._sanitize_path_component: regex-based sanitization and post-checks
- MemoryManager._safe_path: traversal detection via realpath resolution
- AgentIdentity.__post_init__: agent_id sanitization on construction
"""

import os
import tempfile

import pytest

from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import AgentIdentity

# ---------------------------------------------------------------------------
# MemoryManager._sanitize_path_component tests
# ---------------------------------------------------------------------------


class TestSanitizePathComponent:
    """Tests for the static _sanitize_path_component method."""

    def test_valid_alphanumeric(self):
        """Plain alphanumeric strings pass through unchanged."""
        assert MemoryManager._sanitize_path_component("hello123") == "hello123"

    def test_valid_with_hyphen(self):
        """Hyphens are in the allowed set."""
        assert MemoryManager._sanitize_path_component("valid-id") == "valid-id"

    def test_valid_with_underscore(self):
        """Underscores are in the allowed set."""
        assert MemoryManager._sanitize_path_component("valid_id") == "valid_id"

    def test_valid_with_colon(self):
        """Colons are in the allowed set."""
        assert MemoryManager._sanitize_path_component("valid_id:test") == "valid_id:test"

    def test_valid_mixed_allowed_chars(self):
        """All allowed chars together pass unchanged."""
        assert MemoryManager._sanitize_path_component("Agent-01_v2:main") == "Agent-01_v2:main"

    def test_dots_replaced(self):
        """Dots are NOT in the allowed set and get replaced with underscore."""
        assert MemoryManager._sanitize_path_component("has.dot.in.name") == "has_dot_in_name"

    def test_double_dots_replaced(self):
        """'..' is two consecutive non-allowed chars, collapsed to single '_'."""
        result = MemoryManager._sanitize_path_component("..")
        # '..' matches as one consecutive run of non-allowed chars -> '_'
        assert result == "_"

    def test_dot_slash_sequence(self):
        """'../' chars are all non-allowed, collapsed to single '_'."""
        result = MemoryManager._sanitize_path_component("../etc")
        # '../' is three consecutive non-allowed chars -> '_', then 'etc' stays
        assert result == "_etc"

    def test_hidden_dot_prefix(self):
        """Leading dot gets replaced since dots aren't allowed."""
        result = MemoryManager._sanitize_path_component(".hidden")
        # '.' -> '_', 'hidden' stays
        assert result == "_hidden"

    def test_triple_dots(self):
        """'...' is one consecutive run of non-allowed chars -> '_'."""
        result = MemoryManager._sanitize_path_component("...exploit")
        # '...' -> '_', 'exploit' stays
        assert result == "_exploit"

    def test_only_dots(self):
        """Input consisting solely of dots becomes a single '_'."""
        result = MemoryManager._sanitize_path_component("...")
        assert result == "_"

    def test_dots_between_alphanumeric(self):
        """'a..b' -> the '..' in the middle is a single non-allowed run -> '_'."""
        result = MemoryManager._sanitize_path_component("a..b")
        assert result == "a_b"

    def test_slash_replaced(self):
        """Forward slash is not allowed and gets replaced."""
        result = MemoryManager._sanitize_path_component("path/to/file")
        # 'path' + '/' -> '_' + 'to' + '/' -> '_' + 'file'
        assert result == "path_to_file"

    def test_spaces_replaced(self):
        """Spaces are not in allowed set."""
        result = MemoryManager._sanitize_path_component("hello world")
        assert result == "hello_world"

    def test_consecutive_special_chars_collapsed(self):
        """Multiple consecutive non-allowed chars collapse to single '_'."""
        result = MemoryManager._sanitize_path_component("a!!!b")
        assert result == "a_b"

    def test_empty_string(self):
        """Empty string stays empty (regex has nothing to match)."""
        result = MemoryManager._sanitize_path_component("")
        assert result == ""

    def test_traversal_attempt_defused(self):
        """'../../etc/passwd' -> all non-allowed chars replaced, no traversal."""
        result = MemoryManager._sanitize_path_component("../../etc/passwd")
        # '../../' is one consecutive non-allowed run -> '_'
        # 'etc' stays, '/' -> '_', 'passwd' stays
        assert result == "_etc_passwd"


# ---------------------------------------------------------------------------
# MemoryManager._safe_path tests
# ---------------------------------------------------------------------------


class TestSafePath:
    """Tests for _safe_path traversal detection."""

    def setup_method(self):
        """Create a temporary base directory for each test."""
        self._tmpdir = tempfile.mkdtemp()
        self.manager = MemoryManager(base_path=self._tmpdir)

    def test_normal_path_resolves_inside_base(self):
        """A normal subpath resolves correctly under base."""
        result = self.manager._safe_path("agents", "test-agent")
        expected = os.path.join(self._tmpdir, "agents", "test-agent")
        assert result == expected

    def test_traversal_with_dotdot_raises(self):
        """Path containing '..' that escapes base raises ValueError."""
        with pytest.raises(ValueError, match="Path traversal detected"):
            self.manager._safe_path("..", "etc", "passwd")

    def test_traversal_with_relative_escape_raises(self):
        """Deeply nested '..' that still escapes raises ValueError."""
        with pytest.raises(ValueError, match="Path traversal detected"):
            self.manager._safe_path("agents", "..", "..", "..", "etc")

    def test_dotdot_that_stays_inside_is_fine(self):
        """'..' that doesn't escape base_path is allowed."""
        # Going up from a subdirectory but staying within base
        result = self.manager._safe_path("agents", "subdir", "..", "other")
        expected = os.path.realpath(os.path.join(self._tmpdir, "agents", "other"))
        assert result == expected

    def test_base_path_itself_is_allowed(self):
        """Resolving to exactly base_path is allowed."""
        result = self.manager._safe_path("")
        assert result == self._tmpdir or result == os.path.realpath(self._tmpdir)


# ---------------------------------------------------------------------------
# AgentIdentity sanitization tests
# ---------------------------------------------------------------------------


class TestAgentIdentitySanitization:
    """Tests for AgentIdentity.__post_init__ agent_id sanitization."""

    def test_normal_agent_id_unchanged(self):
        """A clean agent_id with only allowed chars stays unchanged."""
        identity = AgentIdentity(agent_id="normal-agent")
        assert identity.agent_id == "normal-agent"

    def test_colons_allowed(self):
        """Colons are in the allowed set for agent_id."""
        identity = AgentIdentity(agent_id="agent:v1:name")
        assert identity.agent_id == "agent:v1:name"

    def test_underscores_and_hyphens_allowed(self):
        """Mixed underscores and hyphens stay unchanged."""
        identity = AgentIdentity(agent_id="my_agent-01")
        assert identity.agent_id == "my_agent-01"

    def test_dots_replaced_in_agent_id(self):
        """Dots are not allowed and get replaced."""
        identity = AgentIdentity(agent_id="agent.name")
        assert "." not in identity.agent_id
        assert identity.agent_id == "agent_name"

    def test_double_dots_sanitized(self):
        """'..' input is sanitized (regex replaces, then secondary check)."""
        identity = AgentIdentity(agent_id="..")
        # Regex: '..' -> '_' (single replacement)
        # Then: '..' not in '_' and '_' doesn't start with '.' -> no further change
        assert ".." not in identity.agent_id
        assert not identity.agent_id.startswith(".")
        assert identity.agent_id == "_"

    def test_traversal_attempt_sanitized(self):
        """'../etc' is fully sanitized."""
        identity = AgentIdentity(agent_id="../etc")
        assert ".." not in identity.agent_id
        assert "/" not in identity.agent_id
        assert not identity.agent_id.startswith(".")
        assert identity.agent_id == "_etc"

    def test_slash_in_agent_id_sanitized(self):
        """Slashes are not allowed and get replaced."""
        identity = AgentIdentity(agent_id="path/traversal")
        assert "/" not in identity.agent_id
        assert identity.agent_id == "path_traversal"

    def test_spaces_in_agent_id_sanitized(self):
        """Spaces are not allowed and get replaced."""
        identity = AgentIdentity(agent_id="my agent")
        assert " " not in identity.agent_id
        assert identity.agent_id == "my_agent"

    def test_complex_traversal_sanitized(self):
        """'../../etc/passwd' is fully defused."""
        identity = AgentIdentity(agent_id="../../etc/passwd")
        assert ".." not in identity.agent_id
        assert "/" not in identity.agent_id
        assert not identity.agent_id.startswith(".")
        assert identity.agent_id == "_etc_passwd"

    def test_hidden_file_prefix_sanitized(self):
        """.hidden prefix is sanitized."""
        identity = AgentIdentity(agent_id=".hidden-agent")
        assert not identity.agent_id.startswith(".")
        # Regex replaces '.' -> '_', result is '_hidden-agent'
        # Secondary check: doesn't start with '.', no '..' -> fine
        assert identity.agent_id == "_hidden-agent"

    def test_uuid_style_id_unchanged(self):
        """UUID-style IDs (with hyphens) pass through fine."""
        test_id = "550e8400-e29b-41d4-a716-446655440000"
        identity = AgentIdentity(agent_id=test_id)
        assert identity.agent_id == test_id
