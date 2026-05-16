"""Tests for src/utils/lightweight_lint.py — lightweight local lint fallback."""

from __future__ import annotations

from unittest.mock import patch

from src.utils.lightweight_lint import (
    LintResult,
    _check_syntax,
    run_lightweight_lint,
)


class TestCheckSyntax:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "good.py"
        f.write_text("x = 1\n")
        issues = _check_syntax(str(f))
        assert issues == []

    def test_syntax_error(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def f(\n")
        issues = _check_syntax(str(f))
        assert len(issues) == 1
        assert "SyntaxError" in issues[0].message
        assert issues[0].source == "ast"
        assert issues[0].file == str(f)

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        issues = _check_syntax(str(f))
        assert issues == []


class TestRunLightweightLint:
    def test_no_files(self):
        result = run_lightweight_lint([])
        assert result.files_checked == 0
        assert result.issue_count == 0

    def test_non_python_files_skipped(self, tmp_path):
        f = tmp_path / "readme.txt"
        f.write_text("hello")
        result = run_lightweight_lint([str(f)])
        assert result.files_checked == 0

    def test_nonexistent_files_skipped(self):
        result = run_lightweight_lint(["/nonexistent/file.py"])
        assert result.files_checked == 0

    def test_valid_python_file(self, tmp_path):
        f = tmp_path / "ok.py"
        f.write_text("x = 1\n")
        result = run_lightweight_lint([str(f)], include_ruff=False)
        assert result.files_checked == 1
        assert result.issue_count == 0

    def test_syntax_error_detected(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def f(\n")
        result = run_lightweight_lint([str(f)], include_ruff=False)
        assert result.issue_count == 1
        assert "SyntaxError" in result.issues[0].message

    def test_summary_no_issues(self, tmp_path):
        f = tmp_path / "ok.py"
        f.write_text("x = 1\n")
        result = run_lightweight_lint([str(f)], include_ruff=False)
        assert "未发现" in result.summary()

    def test_summary_with_issues(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_text("def f(\n")
        result = run_lightweight_lint([str(f)], include_ruff=False)
        assert "发现" in result.summary()
        assert "1" in result.summary()


class TestRuffDegradation:
    def test_ruff_not_installed(self, tmp_path):
        """When ruff is not available, gracefully degrade to ast-only."""
        f = tmp_path / "ok.py"
        f.write_text("x = 1\n")
        with patch(
            "src.utils.lightweight_lint.subprocess.run",
            side_effect=FileNotFoundError("ruff not found"),
        ):
            result = run_lightweight_lint([str(f)], include_ruff=True)
        assert result.ruff_available is False
        assert result.files_checked == 1

    def test_ruff_timeout(self, tmp_path):
        """When ruff times out, gracefully degrade."""
        import subprocess

        f = tmp_path / "ok.py"
        f.write_text("x = 1\n")
        with patch(
            "src.utils.lightweight_lint.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ruff", timeout=10),
        ):
            result = run_lightweight_lint([str(f)], include_ruff=True)
        # Timeout means ruff exists but was slow
        assert result.ruff_available is True
        assert result.files_checked == 1

    def test_include_ruff_false_skips_ruff(self, tmp_path):
        f = tmp_path / "ok.py"
        f.write_text("x = 1\n")
        result = run_lightweight_lint([str(f)], include_ruff=False)
        assert result.ruff_available is False


class TestLintResult:
    def test_issue_count(self):
        from src.utils.lightweight_lint import LintIssue

        r = LintResult(
            issues=[
                LintIssue(file="a.py", line=1, message="err1"),
                LintIssue(file="b.py", line=2, message="err2"),
            ],
            files_checked=2,
        )
        assert r.issue_count == 2

    def test_summary_format(self):
        r = LintResult(files_checked=3, issues=[])
        assert "3" in r.summary()
        assert "降级 lint" in r.summary()
