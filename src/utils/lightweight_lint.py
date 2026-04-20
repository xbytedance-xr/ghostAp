"""Lightweight local lint for review circuit-breaker fallback.

When the review circuit breaker is open (LLM review skipped), this module
runs fast, local-only static checks so the user gets *some* review feedback
instead of nothing.

Checks performed:
1. ``ast.parse`` — catches syntax errors in Python files
2. ``ruff check --select E,F`` — basic PEP-8 and pyflakes (optional, graceful
   degradation if ruff is not installed)
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
from dataclasses import dataclass, field

from .errors import get_error_detail

logger = logging.getLogger(__name__)


@dataclass
class LintIssue:
    """A single lint finding."""

    file: str
    line: int
    message: str
    source: str = "ast"  # "ast" or "ruff"


@dataclass
class LintResult:
    """Aggregated lint result for one or more files."""

    issues: list[LintIssue] = field(default_factory=list)
    files_checked: int = 0
    ruff_available: bool = False
    error: str = ""

    @property
    def issue_count(self) -> int:
        return len(self.issues)

    def summary(self) -> str:
        """One-line Chinese summary for injection into review suggestions."""
        if not self.issues:
            return f"[降级 lint] 检查 {self.files_checked} 个文件，未发现语法/风格问题"
        return (
            f"[降级 lint] 发现 {self.issue_count} 个语法/风格问题"
            f"（{self.files_checked} 个文件）"
        )


def _check_syntax(file_path: str) -> list[LintIssue]:
    """Parse a single Python file and return syntax errors."""
    issues: list[LintIssue] = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
        ast.parse(source, filename=file_path)
    except SyntaxError as e:
        issues.append(LintIssue(
            file=file_path,
            line=e.lineno or 0,
            message=f"SyntaxError: {get_error_detail(e)}",
            source="ast",
        ))
    except Exception as e:
        issues.append(LintIssue(
            file=file_path,
            line=0,
            message=f"Parse error: {get_error_detail(e)}",
            source="ast",
        ))
    return issues


def _check_ruff(file_paths: list[str], timeout: int = 10) -> tuple[list[LintIssue], bool]:
    """Run ``ruff check --select E,F`` on files.  Returns ``(issues, ruff_available)``."""
    issues: list[LintIssue] = []
    try:
        result = subprocess.run(
            ["ruff", "check", "--select", "E,F", "--output-format", "text", "--no-fix", *file_paths],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        for line in (result.stdout or "").splitlines():
            # Format: "file.py:10:5: E302 expected 2 blank lines, found 1"
            parts = line.split(":", 3)
            if len(parts) >= 4:
                try:
                    issues.append(LintIssue(
                        file=parts[0].strip(),
                        line=int(parts[1].strip()),
                        message=parts[3].strip(),
                        source="ruff",
                    ))
                except (ValueError, IndexError):
                    pass
        return issues, True
    except FileNotFoundError:
        logger.debug("ruff not installed, skipping ruff check")
        return [], False
    except subprocess.TimeoutExpired:
        logger.debug("ruff check timed out after %ds", timeout)
        return [], True
    except Exception as e:
        logger.debug("ruff check failed: %s", get_error_detail(e))
        return [], False


def run_lightweight_lint(
    file_paths: list[str],
    *,
    timeout: int = 10,
    include_ruff: bool = True,
) -> LintResult:
    """Run lightweight lint on the given file paths.

    Parameters
    ----------
    file_paths : list[str]
        Absolute or relative paths to Python files.
    timeout : int
        Timeout for ruff subprocess in seconds.
    include_ruff : bool
        Whether to also run ruff (set ``False`` to only do syntax check).

    Returns
    -------
    LintResult
    """
    if not file_paths:
        return LintResult(files_checked=0)

    # Filter to existing Python files
    py_files = [f for f in file_paths if f.endswith(".py") and os.path.isfile(f)]
    if not py_files:
        return LintResult(files_checked=0)

    all_issues: list[LintIssue] = []
    ruff_available = False

    # 1. ast.parse syntax check
    for fp in py_files:
        all_issues.extend(_check_syntax(fp))

    # 2. ruff (optional)
    if include_ruff and py_files:
        ruff_issues, ruff_available = _check_ruff(py_files, timeout=timeout)
        all_issues.extend(ruff_issues)

    return LintResult(
        issues=all_issues,
        files_checked=len(py_files),
        ruff_available=ruff_available,
    )
