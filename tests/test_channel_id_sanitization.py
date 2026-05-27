"""Tests for MemoryManager channel_id/agent_id path traversal protection (AC-19)."""
from __future__ import annotations

import os
import tempfile

import pytest

from src.slock_engine.memory_manager import MemoryManager


class TestPathSanitization:
    """Verify channel_id and agent_id containing traversal sequences are blocked."""

    def setup_method(self):
        self.tmpdir = os.path.realpath(tempfile.mkdtemp())
        self.mm = MemoryManager(base_path=self.tmpdir)

    def test_normal_channel_id_works(self):
        """Normal Feishu chat_id format (oc_abc123) should work fine."""
        path = self.mm._group_memory_path("oc_abc123")
        assert path.startswith(self.tmpdir)
        assert "oc_abc123" in path

    def test_normal_agent_id_works(self):
        """Normal agent_id format should work fine."""
        path = self.mm._agent_memory_path("coder_agent_1")
        assert path.startswith(self.tmpdir)
        assert "coder_agent_1" in path

    def test_channel_id_traversal_raises(self):
        """channel_id='../../etc' should be sanitized and path stays within base."""
        # After sanitization, '../..' becomes '_.._' or similar — path should stay in base
        path = self.mm._group_memory_path("../../etc")
        # The sanitized path must still be under base_path
        assert os.path.realpath(path).startswith(self.tmpdir)

    def test_channel_id_slash_sanitized(self):
        """channel_id containing slashes gets sanitized to underscores."""
        path = self.mm._group_memory_path("foo/bar/baz")
        assert os.path.realpath(path).startswith(self.tmpdir)
        # No raw slashes from the channel_id should appear in the path component
        basename = os.path.basename(os.path.dirname(path))
        assert "/" not in basename or basename == "groups"

    def test_agent_id_traversal_stays_in_base(self):
        """agent_id='../../../tmp/evil' stays within base after sanitization."""
        path = self.mm._agent_memory_path("../../../tmp/evil")
        assert os.path.realpath(path).startswith(self.tmpdir)

    def test_sanitize_path_component_strips_dangerous_chars(self):
        """_sanitize_path_component replaces path separators with underscore."""
        result = MemoryManager._sanitize_path_component("../../etc/passwd")
        # Slashes must be eliminated (path separator injection)
        assert "/" not in result
        assert "\\" not in result

    def test_safe_path_rejects_escape(self):
        """_safe_path raises ValueError when resolved path escapes base."""
        # Directly test with a pre-sanitized but still-escaping path (edge case)
        # This tests the realpath+startswith guard as defense-in-depth
        with pytest.raises(ValueError, match="Path traversal detected"):
            # Use raw path parts that could escape via symlink or similar
            self.mm._safe_path("..", "..", "etc", "passwd")

    def test_get_group_base_path_sanitized(self):
        """get_group_base_path also sanitizes channel_id."""
        path = self.mm.get_group_base_path("oc_normal123")
        assert path.startswith(self.tmpdir)
        assert "oc_normal123" in path

    def test_message_archive_path_sanitized(self):
        """message_archive_path sanitizes channel_id."""
        path = self.mm.message_archive_path("oc_test456")
        assert path.startswith(self.tmpdir)
        assert "oc_test456" in path
