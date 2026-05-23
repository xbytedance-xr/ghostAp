"""Tests for _check_path_restriction path safety (AC15)."""
import os
import tempfile
from unittest.mock import patch

import pytest

from src.acp.client import _check_path_restriction


class TestCheckPathRestriction:
    """Verify _check_path_restriction handles non-existent paths safely."""

    def test_nonexistent_path_no_error(self, tmp_path):
        """Non-existent path should not raise FileNotFoundError."""
        fake_path = str(tmp_path / "does" / "not" / "exist" / "file.txt")
        restrictions = [str(tmp_path)]
        # Should not raise
        result = _check_path_restriction(fake_path, restrictions)
        assert result is True  # Path is under the restriction prefix

    def test_nonexistent_path_outside_restriction(self, tmp_path):
        """Non-existent path outside restriction should return False."""
        fake_path = "/some/nonexistent/path/file.txt"
        restrictions = [str(tmp_path)]
        result = _check_path_restriction(fake_path, restrictions)
        assert result is False

    def test_empty_restrictions_allows_all(self):
        """Empty restrictions list should allow any path."""
        result = _check_path_restriction("/any/path", [])
        assert result is True

    def test_existing_path_within_restriction(self, tmp_path):
        """Existing path within restriction should return True."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")
        restrictions = [str(tmp_path)]
        result = _check_path_restriction(str(test_file), restrictions)
        assert result is True

    def test_symlink_resolved_for_existing_path(self, tmp_path):
        """Symlinks should be resolved for existing paths."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        real_file = real_dir / "file.txt"
        real_file.write_text("content")
        
        link_dir = tmp_path / "link"
        os.symlink(str(real_dir), str(link_dir))
        
        # Restriction on real dir, access via symlink
        restrictions = [str(real_dir)]
        result = _check_path_restriction(str(link_dir / "file.txt"), restrictions)
        assert result is True

    def test_path_prefix_boundary(self, tmp_path):
        """Ensure /home/user doesn't match /home/username."""
        restrictions = [str(tmp_path / "user")]
        (tmp_path / "user").mkdir()
        (tmp_path / "username").mkdir()
        
        result = _check_path_restriction(str(tmp_path / "username" / "file.txt"), restrictions)
        assert result is False
