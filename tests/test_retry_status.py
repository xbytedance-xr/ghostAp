"""Tests for src/spec_engine/retry_status.py — RetryStatus enum and RetryEvent dataclass."""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.spec_engine.retry_status import RetryEvent, RetryStatus


class TestRetryStatusEnum:
    """RetryStatus enum completeness and value stability."""

    def test_has_five_members(self):
        assert len(RetryStatus) == 5

    def test_expected_members(self):
        expected = {"WAITING", "EXECUTING", "SUCCEEDED", "EXHAUSTED", "NO_RETRY"}
        actual = {m.name for m in RetryStatus}
        assert actual == expected

    def test_values_are_strings(self):
        for member in RetryStatus:
            assert isinstance(member.value, str)


class TestRetryEvent:
    """RetryEvent dataclass construction and immutability."""

    def test_construction(self):
        event = RetryEvent(status=RetryStatus.WAITING, detail="7")
        assert event.status == RetryStatus.WAITING
        assert event.detail == "7"

    def test_frozen(self):
        event = RetryEvent(status=RetryStatus.EXECUTING, detail="1/2")
        with pytest.raises(Exception):  # FrozenInstanceError
            event.status = RetryStatus.SUCCEEDED  # type: ignore[misc]

    def test_equality(self):
        a = RetryEvent(status=RetryStatus.SUCCEEDED, detail="")
        b = RetryEvent(status=RetryStatus.SUCCEEDED, detail="")
        assert a == b

    def test_different_detail(self):
        a = RetryEvent(status=RetryStatus.WAITING, detail="5")
        b = RetryEvent(status=RetryStatus.WAITING, detail="10")
        assert a != b


class TestRendererMapping:
    """Verify spec_renderer's RetryStatus → UI_TEXT mapping covers all enum values."""

    def test_mapping_covers_all_statuses(self):
        from src.card.ui_text import UI_TEXT

        # The mapping used in spec_renderer (replicated here for verification).
        # SUCCEEDED is handled by early-return (no card push), so it has no UI_TEXT mapping.
        mapping = {
            RetryStatus.WAITING: "retry_waiting",
            RetryStatus.EXECUTING: "retry_executing",
            RetryStatus.EXHAUSTED: "retry_exhausted",
            RetryStatus.NO_RETRY: "retry_no_retry",
        }
        # Every mapped status has a corresponding UI_TEXT key
        for status, key in mapping.items():
            assert key in UI_TEXT, f"UI_TEXT['{key}'] missing"
        # SUCCEEDED is intentionally excluded from UI_TEXT (never rendered)
        assert "retry_succeeded" not in UI_TEXT


# ---------------------------------------------------------------------------
# AC-R09 / AC-R10: EXHAUSTED and NO_RETRY emission via on_retry_status callback
# ---------------------------------------------------------------------------


class TestExhaustedEmittedOnRetryFailure:
    """AC-R09: RetryStatus.EXHAUSTED is emitted when retry attempts are exhausted."""

    def _make_settings(self, max_attempts=2):
        return SimpleNamespace(
            spec_review_retry_max_attempts=max_attempts,
            spec_review_retry_max_delay=1,
            spec_review_min_timeout=10,
            spec_review_timeout=30,
            spec_review_hard_floor=5,
            spec_review_max_parallel=3,
        )

    def _make_circuit(self):
        from src.spec_engine.review_types import ReviewCircuitState
        circuit = ReviewCircuitState()
        circuit.consecutive_timeouts = 2
        return circuit

    def _make_outcomes(self):
        from src.engine_base import PerspectiveReview, ReviewPerspective
        from src.spec_engine.perspective_worker import PerspectiveOutcome, ReviewErrorCode
        p = ReviewPerspective.ARCHITECT
        return [
            PerspectiveOutcome(
                perspective=p,
                review=PerspectiveReview(perspective=p, passed=False, suggestions=["timeout"], summary="timeout"),
                error="timeout",
                error_code=ReviewErrorCode.TIMEOUT,
            ),
        ]

    def test_exhausted_emitted_on_retry_failure(self):
        """When all retry attempts fail, EXHAUSTED is emitted via on_retry_status."""
        from src.card.ui_text import UI_TEXT
        from src.engine_base import ReviewResult
        from src.spec_engine.review_retry import PipelineRetryContext, handle_pipeline_errors_with_retry

        outcomes = self._make_outcomes()
        review_result = ReviewResult(reviews=[o.review for o in outcomes], iteration=1)
        settings = self._make_settings(max_attempts=2)
        circuit = self._make_circuit()

        status_log = []

        def pipeline_fn(*args, **kwargs):
            """Always fails with timeout."""
            return self._make_outcomes()

        from src.spec_engine.cycle_budget import CycleBudget

        ctx = PipelineRetryContext(
            cancel_event=threading.Event(),
            on_retry_status=lambda event: status_log.append((event.status, event.detail)),
            base_timeout=30,
            multiplier=1,
            pipeline_fn=pipeline_fn,
            budget_cls=CycleBudget,
            artifacts=SimpleNamespace(bindings=[], review_prompt="test"),
            agent_type="coco",
            model_name=None,
        )

        handle_pipeline_errors_with_retry(
            outcomes=outcomes,
            review_result=review_result,
            circuit=circuit,
            settings=settings,
            cycle=1,
            ctx=ctx,
            retry_texts={
                "retry_no_retry": UI_TEXT["retry_no_retry"],
                "retry_exhausted": UI_TEXT["retry_exhausted"],
            },
        )

        # EXHAUSTED must be in the emitted statuses
        emitted_statuses = [s for s, _ in status_log]
        assert RetryStatus.EXHAUSTED in emitted_statuses, (
            f"Expected EXHAUSTED in {emitted_statuses}"
        )


class TestNoRetryEmittedWhenDisabled:
    """AC-R10: RetryStatus.NO_RETRY is emitted when retry is disabled (max_attempts=0)."""

    def _make_settings(self, max_attempts=0):
        return SimpleNamespace(
            spec_review_retry_max_attempts=max_attempts,
            spec_review_retry_max_delay=1,
            spec_review_min_timeout=10,
            spec_review_timeout=30,
            spec_review_hard_floor=5,
            spec_review_max_parallel=3,
        )

    def _make_circuit(self):
        from src.spec_engine.review_types import ReviewCircuitState
        circuit = ReviewCircuitState()
        circuit.consecutive_timeouts = 0
        return circuit

    def _make_outcomes(self):
        from src.engine_base import PerspectiveReview, ReviewPerspective
        from src.spec_engine.perspective_worker import PerspectiveOutcome, ReviewErrorCode
        p = ReviewPerspective.ARCHITECT
        return [
            PerspectiveOutcome(
                perspective=p,
                review=PerspectiveReview(perspective=p, passed=False, suggestions=["timeout"], summary="timeout"),
                error="timeout",
                error_code=ReviewErrorCode.TIMEOUT,
            ),
        ]

    def test_no_retry_emitted_when_disabled(self):
        """When max_attempts=0, NO_RETRY is emitted without attempting retry."""
        from src.card.ui_text import UI_TEXT
        from src.engine_base import ReviewResult
        from src.spec_engine.review_retry import PipelineRetryContext, handle_pipeline_errors_with_retry

        outcomes = self._make_outcomes()
        review_result = ReviewResult(reviews=[o.review for o in outcomes], iteration=1)
        settings = self._make_settings(max_attempts=0)
        circuit = self._make_circuit()

        status_log = []

        ctx = PipelineRetryContext(
            cancel_event=threading.Event(),
            on_retry_status=lambda event: status_log.append((event.status, event.detail)),
            base_timeout=30,
            multiplier=1,
            pipeline_fn=MagicMock(),  # Should never be called
            budget_cls=MagicMock(),
            artifacts=SimpleNamespace(bindings=[], review_prompt="test"),
            agent_type="coco",
            model_name=None,
        )

        handle_pipeline_errors_with_retry(
            outcomes=outcomes,
            review_result=review_result,
            circuit=circuit,
            settings=settings,
            cycle=1,
            ctx=ctx,
            retry_texts={
                "retry_no_retry": UI_TEXT["retry_no_retry"],
                "retry_exhausted": UI_TEXT["retry_exhausted"],
            },
        )

        # NO_RETRY must be emitted since max_attempts=0
        emitted_statuses = [s for s, _ in status_log]
        assert RetryStatus.NO_RETRY in emitted_statuses, (
            f"Expected NO_RETRY in {emitted_statuses}"
        )
        # pipeline_fn should NOT have been called
        ctx.pipeline_fn.assert_not_called()


class TestBuildDiagnosticsNoFalseExhausted:
    """AC-R04: retry_attempted=False + max_attempts>0 should NOT use retry_exhausted."""

    def test_no_false_exhausted(self):
        from src.card.ui_text import UI_TEXT
        from src.engine_base import PerspectiveReview, ReviewPerspective
        from src.spec_engine.perspective_worker import PerspectiveOutcome, ReviewErrorCode
        from src.spec_engine.review_retry import build_retry_diagnostics
        from src.spec_engine.review_types import ReviewCircuitState

        p = ReviewPerspective.ARCHITECT
        outcomes = [
            PerspectiveOutcome(
                perspective=p,
                review=PerspectiveReview(perspective=p, passed=False, suggestions=["timeout"], summary="timeout"),
                error="timeout",
                error_code=ReviewErrorCode.TIMEOUT,
            ),
        ]
        circuit = ReviewCircuitState()
        settings = SimpleNamespace(spec_review_retry_max_attempts=2)

        diag = build_retry_diagnostics(
            outcomes=outcomes,
            failed_workers=outcomes,
            circuit=circuit,
            settings=settings,
            cycle=1,
            err_type_val="timeout",
            all_timeout=True,
            retry_attempted=False,
            retry_texts={
                "retry_no_retry": UI_TEXT["retry_no_retry"],
                "retry_exhausted": UI_TEXT["retry_exhausted"],
            },
        )

        # Should use retry_no_retry, NOT retry_exhausted
        assert diag["err_type"] == UI_TEXT["retry_no_retry"]
        assert "已重试" not in diag["err_type"]


class TestBuildDiagnosticsRetryTextsFromUIText:
    """build_retry_diagnostics uses retry_texts from UI_TEXT (mandatory parameter)."""

    def test_no_retry_text_from_ui(self):
        from types import SimpleNamespace

        from src.card.ui_text import UI_TEXT
        from src.engine_base import ReviewPerspective
        from src.spec_engine.perspective_worker import PerspectiveOutcome, PerspectiveReview, ReviewErrorCode
        from src.spec_engine.review_retry import build_retry_diagnostics
        from src.spec_engine.review_types import ReviewCircuitState

        p = ReviewPerspective.ARCHITECT
        outcomes = [
            PerspectiveOutcome(
                perspective=p,
                review=PerspectiveReview(perspective=p, passed=False, suggestions=["timeout"], summary="timeout"),
                error="timeout",
                error_code=ReviewErrorCode.TIMEOUT,
            ),
        ]
        circuit = ReviewCircuitState()
        settings = SimpleNamespace(spec_review_retry_max_attempts=2)

        diag = build_retry_diagnostics(
            outcomes=outcomes,
            failed_workers=outcomes,
            circuit=circuit,
            settings=settings,
            cycle=1,
            err_type_val="timeout",
            all_timeout=True,
            retry_attempted=False,
            retry_texts={
                "retry_no_retry": UI_TEXT["retry_no_retry"],
                "retry_exhausted": UI_TEXT["retry_exhausted"],
            },
        )

        # Should use retry_no_retry text from UI_TEXT
        assert diag["err_type"] == UI_TEXT["retry_no_retry"]


class TestReviewRetryNoCardImport:
    """Guard: src.spec_engine.review_retry must NOT import from src.card."""

    def test_no_card_layer_import(self):
        import ast
        from pathlib import Path

        source = Path(__file__).parent.parent / "src" / "spec_engine" / "review_retry.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("src.card") and "card" not in node.module.split("."), (
                    f"review_retry.py must not import from card layer, found: from {node.module}"
                )

