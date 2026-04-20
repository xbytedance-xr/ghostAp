"""L1 Lint Gate: run fast local lint *before* dispatching an ACP review.

Step 6 of the review refactor. Motivation: when the build phase produces
syntactically broken Python, firing 5 perspective workers (each a full ACP
round-trip) to eventually say "code is broken" wastes minutes. A cheap
local `ast.parse` + `ruff` pass catches that in milliseconds.

Gate policy:
    * Syntax errors present         → SHORT_CIRCUIT. Emit synthetic FAIL
                                      PerspectiveOutcomes for all perspectives
                                      with the lint findings as suggestions.
                                      Skip ACP entirely for this cycle.
    * Only ruff (style) findings    → PROCEED. Lint summary is returned so
                                      the pipeline can inject it as a hint;
                                      full ACP review still runs.
    * Clean / no files / no tools   → PROCEED. No-op.

This module is standalone — no session/engine dependency. Wired into
ReviewPipeline at Step 7.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..engine_base import PerspectiveReview, ReviewPerspective
from ..utils.lightweight_lint import LintResult, run_lightweight_lint
from .perspective_worker import PerspectiveOutcome
from .review_artifacts import ReviewArtifacts

logger = logging.getLogger(__name__)

__all__ = [
    "LintGateSeverity",
    "LintGateDecision",
    "evaluate_lint_gate",
    "build_lint_gate_outcomes",
]


class LintGateSeverity(str, Enum):
    CLEAN = "clean"
    STYLE = "style"
    SYNTAX = "syntax"


@dataclass
class LintGateDecision:
    should_short_circuit: bool
    severity: LintGateSeverity
    lint_result: Optional[LintResult]
    summary: str = ""
    files_checked: int = 0


def _resolve_touched_files(artifacts: ReviewArtifacts, max_files: int) -> list[str]:
    """Resolve relative touched_files against artifacts.cwd, keep Python only."""
    if not artifacts.touched_files:
        return []
    cwd = artifacts.cwd or ""
    out: list[str] = []
    for rel in artifacts.touched_files:
        if not rel or not rel.endswith(".py"):
            continue
        path = rel if os.path.isabs(rel) else os.path.join(cwd, rel)
        if os.path.isfile(path):
            out.append(path)
        if len(out) >= max_files:
            break
    return out


def evaluate_lint_gate(
    artifacts: ReviewArtifacts,
    *,
    max_files: int = 50,
    timeout: int = 10,
    include_ruff: bool = True,
) -> LintGateDecision:
    """Run lightweight lint and classify the result into a gate decision."""
    files = _resolve_touched_files(artifacts, max_files)
    if not files:
        return LintGateDecision(
            should_short_circuit=False,
            severity=LintGateSeverity.CLEAN,
            lint_result=None,
            summary="[lint-gate] no touched Python files",
            files_checked=0,
        )

    result = run_lightweight_lint(files, timeout=timeout, include_ruff=include_ruff)

    if result.files_checked == 0:
        return LintGateDecision(
            should_short_circuit=False,
            severity=LintGateSeverity.CLEAN,
            lint_result=result,
            summary=result.summary(),
            files_checked=0,
        )

    syntax_issues = [i for i in result.issues if i.source == "ast"]
    if syntax_issues:
        sample = syntax_issues[0]
        summary = (
            f"[lint-gate] 语法错误 {len(syntax_issues)} 个（共 {result.issue_count} 问题，"
            f"{result.files_checked} 文件），首个：{sample.file}:{sample.line} {sample.message}"
        )
        logger.warning("[LintGate] syntax errors found, short-circuiting review: %s", summary)
        return LintGateDecision(
            should_short_circuit=True,
            severity=LintGateSeverity.SYNTAX,
            lint_result=result,
            summary=summary,
            files_checked=result.files_checked,
        )

    if result.issues:
        summary = (
            f"[lint-gate] 风格问题 {result.issue_count} 个（{result.files_checked} 文件），"
            f"不阻断评审"
        )
        return LintGateDecision(
            should_short_circuit=False,
            severity=LintGateSeverity.STYLE,
            lint_result=result,
            summary=summary,
            files_checked=result.files_checked,
        )

    return LintGateDecision(
        should_short_circuit=False,
        severity=LintGateSeverity.CLEAN,
        lint_result=result,
        summary=result.summary(),
        files_checked=result.files_checked,
    )


def build_lint_gate_outcomes(
    decision: LintGateDecision,
    perspectives: Optional[list[ReviewPerspective]] = None,
    *,
    max_issues_in_suggestion: int = 5,
) -> list[PerspectiveOutcome]:
    """Build synthetic FAIL outcomes for all perspectives when the gate short-circuits.

    Each perspective receives the same lint findings as suggestions. Caller
    should NOT invoke this unless `decision.should_short_circuit` is True.
    """
    if not decision.should_short_circuit or not decision.lint_result:
        return []

    perspectives = perspectives or list(ReviewPerspective)

    issues = decision.lint_result.issues[:max_issues_in_suggestion]
    suggestions = [decision.summary]
    for issue in issues:
        suggestions.append(f"- {issue.file}:{issue.line} [{issue.source}] {issue.message}")
    if decision.lint_result.issue_count > max_issues_in_suggestion:
        suggestions.append(
            f"...（其余 {decision.lint_result.issue_count - max_issues_in_suggestion} 个问题略）"
        )

    outcomes: list[PerspectiveOutcome] = []
    for p in perspectives:
        outcomes.append(
            PerspectiveOutcome(
                perspective=p,
                review=PerspectiveReview(
                    perspective=p,
                    passed=False,
                    suggestions=list(suggestions),
                    summary="lint-gate 阻断",
                ),
                elapsed_ms=0,
                error="lint_gate_short_circuit",
            )
        )
    return outcomes
