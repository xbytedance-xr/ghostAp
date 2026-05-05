"""Verify ruff TID251 (banned-api) rule catches deprecated shim imports."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


class TestTID251BannedImports:
    """TID251 should flag deprecated shim imports in non-exempt files."""

    @pytest.fixture()
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def _run_ruff(self, source: str, project_root: Path) -> subprocess.CompletedProcess:
        """Write source to a temp file inside src/ and run ruff --select TID251."""
        # Write to a temp .py under src/card/ so ruff picks up pyproject.toml config
        # but NOT in an exempt path (tests/** or shim files themselves).
        tmp_dir = project_root / "src" / "card" / "_lint_check_tmp"
        tmp_dir.mkdir(exist_ok=True)
        tmp_file = tmp_dir / "_test_banned.py"
        try:
            tmp_file.write_text(source, encoding="utf-8")
            result = subprocess.run(
                ["uv", "run", "ruff", "check", "--select", "TID251", str(tmp_file)],
                capture_output=True,
                text=True,
                cwd=str(project_root),
            )
            return result
        finally:
            tmp_file.unlink(missing_ok=True)
            tmp_dir.rmdir()

    def test_banned_import_detected(self, project_root: Path):
        """Importing from a deprecated shim triggers TID251."""
        source = textwrap.dedent("""\
            from src.card.session_config import SessionConfig
        """)
        result = self._run_ruff(source, project_root)
        assert result.returncode != 0, f"Expected ruff to fail but got rc=0: {result.stdout}"
        assert "TID251" in result.stdout

    def test_valid_import_passes(self, project_root: Path):
        """Importing from the canonical path does NOT trigger TID251."""
        source = textwrap.dedent("""\
            from src.card.session.config import SessionConfig
        """)
        result = self._run_ruff(source, project_root)
        # Should pass (rc=0) or at least not contain TID251
        assert "TID251" not in result.stdout
