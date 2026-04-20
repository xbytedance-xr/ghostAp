"""Tests for Step 6 — L1 Lint Gate."""

from __future__ import annotations

import os

import pytest

from src.engine_base import ReviewPerspective
from src.spec_engine.lint_gate import (
    LintGateSeverity,
    build_lint_gate_outcomes,
    evaluate_lint_gate,
)
from src.spec_engine.review_artifacts import ReviewArtifacts


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _artifacts(tmpdir, files: list[str]) -> ReviewArtifacts:
    return ReviewArtifacts(
        cycle_number=1,
        requirement="X",
        cwd=str(tmpdir),
        touched_files=files,
    )


def test_gate_no_touched_files_is_clean(tmp_path):
    art = _artifacts(tmp_path, [])
    d = evaluate_lint_gate(art)
    assert not d.should_short_circuit
    assert d.severity == LintGateSeverity.CLEAN
    assert d.files_checked == 0


def test_gate_filters_non_python_files(tmp_path):
    _write(str(tmp_path / "a.md"), "# md\n")
    art = _artifacts(tmp_path, ["a.md"])
    d = evaluate_lint_gate(art)
    assert not d.should_short_circuit
    assert d.severity == LintGateSeverity.CLEAN
    assert d.files_checked == 0


def test_gate_clean_python_proceeds(tmp_path):
    _write(str(tmp_path / "ok.py"), "x = 1\n")
    art = _artifacts(tmp_path, ["ok.py"])
    d = evaluate_lint_gate(art, include_ruff=False)
    assert not d.should_short_circuit
    assert d.severity == LintGateSeverity.CLEAN
    assert d.files_checked == 1


def test_gate_syntax_error_short_circuits(tmp_path):
    _write(str(tmp_path / "bad.py"), "def f(:\n    pass\n")  # SyntaxError
    art = _artifacts(tmp_path, ["bad.py"])
    d = evaluate_lint_gate(art, include_ruff=False)
    assert d.should_short_circuit is True
    assert d.severity == LintGateSeverity.SYNTAX
    assert d.files_checked == 1
    assert "语法错误" in d.summary


def test_gate_absolute_path_handled(tmp_path):
    bad = str(tmp_path / "broken.py")
    _write(bad, "def :(\n")
    art = _artifacts(tmp_path, [bad])
    d = evaluate_lint_gate(art, include_ruff=False)
    assert d.should_short_circuit
    assert d.severity == LintGateSeverity.SYNTAX


def test_gate_missing_file_skipped(tmp_path):
    art = _artifacts(tmp_path, ["ghost.py"])
    d = evaluate_lint_gate(art, include_ruff=False)
    assert not d.should_short_circuit
    assert d.files_checked == 0


def test_gate_max_files_cap(tmp_path):
    files = []
    for i in range(5):
        name = f"f{i}.py"
        _write(str(tmp_path / name), "x = 1\n")
        files.append(name)
    art = _artifacts(tmp_path, files)
    d = evaluate_lint_gate(art, max_files=2, include_ruff=False)
    assert d.files_checked == 2


def test_build_outcomes_no_short_circuit_returns_empty(tmp_path):
    _write(str(tmp_path / "ok.py"), "x = 1\n")
    art = _artifacts(tmp_path, ["ok.py"])
    d = evaluate_lint_gate(art, include_ruff=False)
    assert build_lint_gate_outcomes(d) == []


def test_build_outcomes_short_circuit_emits_all_perspectives(tmp_path):
    _write(str(tmp_path / "bad.py"), "def f(:\n    pass\n")
    art = _artifacts(tmp_path, ["bad.py"])
    d = evaluate_lint_gate(art, include_ruff=False)
    outs = build_lint_gate_outcomes(d)
    assert len(outs) == len(list(ReviewPerspective))
    for o in outs:
        assert o.review.passed is False
        assert o.review.summary == "lint-gate 阻断"
        assert o.error == "lint_gate_short_circuit"
        assert any("bad.py" in s for s in o.review.suggestions)


def test_build_outcomes_truncates_issue_list(tmp_path):
    # Write 3 syntactically broken files to exceed default max_issues_in_suggestion=5? No, 3 <5.
    # Use max_issues_in_suggestion=1 to force truncation.
    for i in range(3):
        _write(str(tmp_path / f"b{i}.py"), "def :(\n")
    art = _artifacts(tmp_path, [f"b{i}.py" for i in range(3)])
    d = evaluate_lint_gate(art, include_ruff=False)
    outs = build_lint_gate_outcomes(d, max_issues_in_suggestion=1)
    arch = outs[0]
    # Should have summary + 1 issue line + truncation notice = 3 suggestions
    truncation_lines = [s for s in arch.review.suggestions if "其余" in s]
    assert len(truncation_lines) == 1


def test_build_outcomes_restricted_perspectives(tmp_path):
    _write(str(tmp_path / "bad.py"), "def :(\n")
    art = _artifacts(tmp_path, ["bad.py"])
    d = evaluate_lint_gate(art, include_ruff=False)
    outs = build_lint_gate_outcomes(d, perspectives=[ReviewPerspective.ARCHITECT])
    assert len(outs) == 1
    assert outs[0].perspective == ReviewPerspective.ARCHITECT
