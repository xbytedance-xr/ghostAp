"""AC-16: --validate CLI entry-point regression test.

Ensures `python -m src.main --validate` exits with code 0 when config is valid.
"""

from __future__ import annotations

import subprocess
import sys

import pytest


class TestValidateCLI:
    """AC-16: `python -m src.main --validate` exits 0 with valid config."""

    def test_validate_exits_zero(self):
        """AC-16: --validate startup check passes and returns exit code 0."""
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "--validate"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"--validate failed with exit code {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )

    def test_validate_output_contains_pass_marker(self):
        """AC-16: --validate output contains '配置校验通过' success marker."""
        result = subprocess.run(
            [sys.executable, "-m", "src.main", "--validate"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "配置校验通过" in result.stdout, (
            f"Expected '配置校验通过' in stdout, got: {result.stdout}"
        )
