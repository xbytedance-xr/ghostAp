from unittest.mock import MagicMock, patch

import pytest

from src.acp.models import PromptResult
from src.engine_base import PerspectiveReview, ReviewPerspective
from src.spec_engine.cycle_budget import CycleBudget
from src.spec_engine.lint_gate import LintGateDecision, LintGateSeverity
from src.spec_engine.perspective_worker import PerspectiveOutcome
from src.spec_engine.review_artifacts import ReviewArtifacts
from src.spec_engine.review_pipeline import _inject_lint_hints, run_review_pipeline
from src.utils.lightweight_lint import LintIssue, LintResult


@pytest.fixture
def artifacts():
    return ReviewArtifacts(
        cycle_number=1,
        requirement="test requirement",
        cwd="/tmp/test",
        touched_files=["a.py"],
    )


@pytest.fixture
def budget():
    return CycleBudget(total_seconds=60)


def _make_lint_result(issues=None, files_checked=1):
    """Build a real LintResult instead of MagicMock to avoid int comparison errors."""
    return LintResult(issues=issues or [], files_checked=files_checked)


def _raw_review_output(perspective: str, verdict: str = "PASS") -> str:
    """Build raw text matching REVIEW_SECTION_PATTERN for the parser."""
    tag = perspective.upper()
    return f"[{tag}]\n{verdict}\n"


# ────────────────────────────────────────────────────────────────────
# 1. Lint gate short-circuit
# ────────────────────────────────────────────────────────────────────

def test_pipeline_lint_gate_short_circuit(artifacts, budget):
    """If lint gate finds syntax errors, pipeline should short-circuit immediately."""
    lint_result = _make_lint_result(
        issues=[LintIssue(file="a.py", line=1, source="ast", message="SyntaxError: invalid syntax")],
        files_checked=1,
    )
    decision = LintGateDecision(
        should_short_circuit=True,
        severity=LintGateSeverity.SYNTAX,
        lint_result=lint_result,
        summary="syntax error found",
        files_checked=1,
    )
    with patch("src.spec_engine.review_pipeline.evaluate_lint_gate", return_value=decision):
        outcomes = run_review_pipeline(artifacts, budget)
        # Should return synthetic FAIL outcomes for all 5 perspectives
        assert len(outcomes) == len(ReviewPerspective)
        assert all(not o.review.passed for o in outcomes)
        assert all(o.error == "lint_gate_short_circuit" for o in outcomes)
        assert all("syntax error found" in o.review.suggestions[0] for o in outcomes)


def test_pipeline_lint_gate_no_short_circuit_when_clean(artifacts, budget):
    """Clean lint should NOT short-circuit."""
    decision = LintGateDecision(
        should_short_circuit=False,
        severity=LintGateSeverity.CLEAN,
        lint_result=None,
        summary="clean",
    )
    mock_session = MagicMock()
    raw = _raw_review_output("ARCHITECT", "PASS")
    mock_session.send_prompt.return_value = PromptResult(stop_reason="end_turn", text=raw)

    with patch("src.spec_engine.review_pipeline.evaluate_lint_gate", return_value=decision), \
         patch("src.spec_engine.review_pipeline.EphemeralReviewSession") as mock_eph:
        mock_eph.return_value.__enter__.return_value = mock_session
        outcomes = run_review_pipeline(
            artifacts, budget, perspectives=[ReviewPerspective.ARCHITECT], max_parallel=2
        )
        assert len(outcomes) == 1
        assert outcomes[0].error is None


# ────────────────────────────────────────────────────────────────────
# 2. Full parallel run
# ────────────────────────────────────────────────────────────────────

def test_pipeline_full_parallel_run(artifacts, budget):
    """If lint gate passes, pipeline should run all perspectives via parallel sessions."""
    decision = LintGateDecision(
        should_short_circuit=False,
        severity=LintGateSeverity.CLEAN,
        lint_result=None,
        summary="clean",
    )

    # Each perspective's session will return a matching [TAG]\nPASS block
    def _make_session_for(p: ReviewPerspective):
        s = MagicMock()
        s.send_prompt.return_value = PromptResult(
            stop_reason="end_turn",
            text=_raw_review_output(p.name, "PASS"),
        )
        return s

    sessions_created = []

    def _fake_ephemeral(agent_type, cwd, model_name=None):
        ctx = MagicMock()
        # Return the next session; we don't know the perspective in advance
        # so return a generic PASS for architect/product
        s = MagicMock()
        # Dynamically return correct perspective based on prompt content
        def _dynamic_send(prompt, on_event=None, timeout=None):
            for p in ReviewPerspective:
                tag = p.name
                if f"[{tag}]" in prompt or p.display_name in prompt:
                    return PromptResult(
                        stop_reason="end_turn",
                        text=_raw_review_output(tag, "PASS"),
                    )
            return PromptResult(stop_reason="end_turn", text=_raw_review_output("ARCHITECT", "PASS"))

        s.send_prompt.side_effect = _dynamic_send
        ctx.__enter__ = MagicMock(return_value=s)
        ctx.__exit__ = MagicMock(return_value=False)
        sessions_created.append(ctx)
        return ctx

    with patch("src.spec_engine.review_pipeline.evaluate_lint_gate", return_value=decision), \
         patch("src.spec_engine.review_pipeline.EphemeralReviewSession", side_effect=_fake_ephemeral):

        perspectives = [ReviewPerspective.ARCHITECT, ReviewPerspective.PRODUCT]
        outcomes = run_review_pipeline(artifacts, budget, perspectives=perspectives, max_parallel=2)

        assert len(outcomes) == 2
        # Outcomes are sorted by ReviewPerspective enum order
        assert outcomes[0].perspective == ReviewPerspective.ARCHITECT
        assert outcomes[1].perspective == ReviewPerspective.PRODUCT
        assert outcomes[0].review.passed is True
        assert outcomes[1].review.passed is True
        assert outcomes[0].error is None
        assert outcomes[1].error is None
        # Two ephemeral sessions were created
        assert len(sessions_created) == 2


# ────────────────────────────────────────────────────────────────────
# 3. Lint hint injection (style issues, non-blocking)
# ────────────────────────────────────────────────────────────────────

def test_pipeline_inject_lint_hints(artifacts, budget):
    """If lint gate finds style issues, they should be injected as hints into outcomes."""
    lint_result = _make_lint_result(
        issues=[LintIssue(file="a.py", line=5, source="ruff", message="E303 too many blank lines")],
        files_checked=1,
    )
    decision = LintGateDecision(
        should_short_circuit=False,
        severity=LintGateSeverity.STYLE,
        lint_result=lint_result,
        summary="style problems detected",
        files_checked=1,
    )

    mock_session = MagicMock()
    raw = _raw_review_output("ARCHITECT", "PASS")
    mock_session.send_prompt.return_value = PromptResult(stop_reason="end_turn", text=raw)

    with patch("src.spec_engine.review_pipeline.evaluate_lint_gate", return_value=decision), \
         patch("src.spec_engine.review_pipeline.EphemeralReviewSession") as mock_eph:
        mock_eph.return_value.__enter__.return_value = mock_session

        outcomes = run_review_pipeline(
            artifacts, budget, perspectives=[ReviewPerspective.ARCHITECT], max_parallel=2
        )

        assert len(outcomes) == 1
        # Check that lint hint was injected
        assert any("[lint-gate hint]" in s for s in outcomes[0].review.suggestions)
        assert any("style problems detected" in s for s in outcomes[0].review.suggestions)


# ────────────────────────────────────────────────────────────────────
# 4. Budget exhausted → synthetic timeout
# ────────────────────────────────────────────────────────────────────

def test_pipeline_budget_exhausted(artifacts):
    """If budget is already exceeded, pipeline should return synthetic timeouts."""
    budget = CycleBudget(total_seconds=10)
    budget.start()
    # Fake some elapsed time
    with patch("time.monotonic", return_value=budget.started_at + 15):
        assert budget.exceeded()

        decision = LintGateDecision(
            should_short_circuit=False,
            severity=LintGateSeverity.CLEAN,
            lint_result=None,
        )

        with patch("src.spec_engine.review_pipeline.evaluate_lint_gate", return_value=decision):
            outcomes = run_review_pipeline(
                artifacts, budget, perspectives=[ReviewPerspective.ARCHITECT]
            )
            assert len(outcomes) == 1
            assert outcomes[0].error == "cycle_budget_exceeded"
            assert outcomes[0].review.summary == "预算超时"


# ────────────────────────────────────────────────────────────────────
# 5. Session failure isolation
# ────────────────────────────────────────────────────────────────────

def test_pipeline_session_failure_isolated(artifacts, budget):
    """If one session raises, it should not crash the entire pipeline."""
    decision = LintGateDecision(
        should_short_circuit=False,
        severity=LintGateSeverity.CLEAN,
        lint_result=None,
    )

    call_count = {"n": 0}

    def _flaky_ephemeral(agent_type, cwd, model_name=None):
        ctx = MagicMock()
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First session fails on enter
            ctx.__enter__ = MagicMock(side_effect=RuntimeError("ACP startup failed"))
        else:
            s = MagicMock()
            s.send_prompt.return_value = PromptResult(
                stop_reason="end_turn",
                text=_raw_review_output("PRODUCT", "PASS"),
            )
            ctx.__enter__ = MagicMock(return_value=s)
        ctx.__exit__ = MagicMock(return_value=False)
        return ctx

    with patch("src.spec_engine.review_pipeline.evaluate_lint_gate", return_value=decision), \
         patch("src.spec_engine.review_pipeline.EphemeralReviewSession", side_effect=_flaky_ephemeral):

        perspectives = [ReviewPerspective.ARCHITECT, ReviewPerspective.PRODUCT]
        outcomes = run_review_pipeline(artifacts, budget, perspectives=perspectives, max_parallel=2)

        assert len(outcomes) == 2
        # The failed one should have an error
        architect_outcome = next(o for o in outcomes if o.perspective == ReviewPerspective.ARCHITECT)
        product_outcome = next(o for o in outcomes if o.perspective == ReviewPerspective.PRODUCT)
        assert architect_outcome.error is not None
        assert "ACP startup failed" in architect_outcome.error
        # The healthy one should succeed
        assert product_outcome.review.passed is True


# ────────────────────────────────────────────────────────────────────
# 6. _inject_lint_hints unit test
# ────────────────────────────────────────────────────────────────────

def test_inject_lint_hints_noop_on_empty_outcomes():
    """No-op when outcomes list is empty."""
    decision = LintGateDecision(
        should_short_circuit=False,
        severity=LintGateSeverity.STYLE,
        lint_result=_make_lint_result(),
        summary="some lint",
    )
    outcomes = []
    _inject_lint_hints(outcomes, decision)
    assert outcomes == []


def test_inject_lint_hints_noop_when_no_lint_result():
    """No-op when lint_result is None."""
    decision = LintGateDecision(
        should_short_circuit=False,
        severity=LintGateSeverity.CLEAN,
        lint_result=None,
    )
    outcome = PerspectiveOutcome(
        perspective=ReviewPerspective.ARCHITECT,
        review=PerspectiveReview(
            perspective=ReviewPerspective.ARCHITECT,
            passed=True,
            suggestions=[],
        ),
    )
    _inject_lint_hints([outcome], decision)
    assert outcome.review.suggestions == []
