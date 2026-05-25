"""Tests for Fail-Closed path blacklist behavior.

Verifies that is_path_blacklisted returns True (deny) on exceptions
instead of False (allow), preventing path restriction bypass.
"""

import logging
from unittest.mock import patch

import pytest

from src.utils.path_security import is_path_blacklisted


class TestFailClosedPathBlacklist:
    """Test suite for Fail-Closed path blacklisting."""

    def test_exception_returns_true(self, caplog: pytest.LogCaptureFixture) -> None:
        """When os.path.abspath raises, is_path_blacklisted should return True (Fail-Closed)."""
        caplog.set_level(logging.ERROR)

        # Patch os.path.abspath to raise an exception (called for all paths)
        with patch("src.utils.path_security.get_acp_blacklist", return_value=([], ["/tmp/blacklisted"], [])):
            with patch("os.path.abspath", side_effect=OSError("Filesystem error")):
                result = is_path_blacklisted("/some/path")

        # Should return True (deny) on exception, not False (allow)
        assert result is True, "Fail-Closed: exception should deny access"

        # Verify error was logged
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_records) > 0, "Exception should be logged at ERROR level"
        assert "fail-closed" in caplog.text.lower() or "fail_closed" in caplog.text.lower()

    def test_normal_operation_still_works(self) -> None:
        """Normal path checking should still work when no exceptions occur."""
        with patch("src.utils.path_security.get_acp_blacklist", return_value=([], ["/tmp/blacklisted"], [])):
            # Path not in blacklist
            assert is_path_blacklisted("/tmp/safe/file.txt") is False
            # Path in blacklisted directory
            assert is_path_blacklisted("/tmp/blacklisted/secret.txt") is True

    def test_empty_blacklist_returns_false(self) -> None:
        """Empty blacklist should return False (nothing is blacklisted)."""
        with patch("src.utils.path_security.get_acp_blacklist", return_value=([], [], [])):
            assert is_path_blacklisted("/any/path") is False

    def test_empty_path_returns_false(self) -> None:
        """Empty path should return False (no path to check)."""
        assert is_path_blacklisted("") is False
