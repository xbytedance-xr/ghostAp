"""Integration tests: conduct_review circuit-open branch includes lint fallback."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from src.spec_engine.review import ReviewCircuitState, conduct_review
from src.utils.review_diagnostics import normalize_review_diagnostics


def _make_settings(**overrides):
    defaults = {
        "spec_review_timeout": 120,
        "spec_review_failure_circuit_enabled": True,
        "spec_review_failure_max_consecutive": 3,
        "spec_review_failure_cooldown_cycles": 3,
        "spec_review_failure_max_cooldown_cycles": 12,
        "spec_review_min_timeout": 30,
        "spec_review_enabled": True,
        "review_circuit_lint_fallback_enabled": True,
        "review_circuit_lint_timeout": 10,
    }
    defaults.update(overrides)
    s = MagicMock()
    for k, v in defaults.items():
        setattr(s, k, v)
    return s


class TestSpecLintFallbackIntegration:
    """Spec conduct_review: when circuit is open, lint fallback should produce suggestions."""

    def test_circuit_open_includes_lint_summary(self, tmp_path):
        """When circuit is open and project has Python files, lint summary appears."""
        # Create a Python file with a syntax error
        bad_file = tmp_path / "broken.py"
        bad_file.write_text("def f(\n")

        settings = _make_settings()
        project = MagicMock()
        project.requirement = "test"
        project.root_path = str(tmp_path)

        circuit = ReviewCircuitState()
        circuit.review_circuit_open_until_cycle = 10  # circuit is open
        circuit.review_failure_consecutive = 3

        result = conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=lambda *a, **kw: None,
            build_review_exception_diagnostics_fn=lambda e, cycle: {},
            circuit=circuit,
            cycle=5,  # <= open_until=10
        )

        all_suggestions = []
        for pr in result.reviews:
            all_suggestions.extend(pr.suggestions)

        # Should contain both circuit message and lint message
        assert any("熔断" in s for s in all_suggestions)
        assert any("降级 lint" in s for s in all_suggestions)
        assert any("发现" in s for s in all_suggestions)

    def test_circuit_open_no_issues(self, tmp_path):
        """When circuit is open and all files are valid, lint reports no issues."""
        good_file = tmp_path / "ok.py"
        good_file.write_text("x = 1\n")

        settings = _make_settings()
        project = MagicMock()
        project.requirement = "test"
        project.root_path = str(tmp_path)

        circuit = ReviewCircuitState()
        circuit.review_circuit_open_until_cycle = 10
        circuit.review_failure_consecutive = 3

        result = conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=lambda *a, **kw: None,
            build_review_exception_diagnostics_fn=lambda e, cycle: {},
            circuit=circuit,
            cycle=5,
        )

        all_suggestions = []
        for pr in result.reviews:
            all_suggestions.extend(pr.suggestions)

        assert any("降级 lint" in s for s in all_suggestions)
        assert any("未发现" in s for s in all_suggestions)

    def test_lint_disabled_by_config(self, tmp_path):
        """When lint fallback is disabled, no lint summary in suggestions."""
        bad_file = tmp_path / "broken.py"
        bad_file.write_text("def f(\n")

        settings = _make_settings(review_circuit_lint_fallback_enabled=False)
        project = MagicMock()
        project.requirement = "test"
        project.root_path = str(tmp_path)

        circuit = ReviewCircuitState()
        circuit.review_circuit_open_until_cycle = 10
        circuit.review_failure_consecutive = 3

        result = conduct_review(
            session=MagicMock(),
            settings=settings,
            project=project,
            send_prompt_with_retry_fn=lambda *a, **kw: None,
            build_review_exception_diagnostics_fn=lambda e, cycle: {},
            circuit=circuit,
            cycle=5,
        )

        all_suggestions = []
        for pr in result.reviews:
            all_suggestions.extend(pr.suggestions)

        assert any("熔断" in s for s in all_suggestions)
        assert not any("降级 lint" in s for s in all_suggestions)

    def test_lint_exception_does_not_crash(self, tmp_path):
        """If lint raises, circuit fallback still works."""
        settings = _make_settings()
        project = MagicMock()
        project.requirement = "test"
        project.root_path = str(tmp_path)

        circuit = ReviewCircuitState()
        circuit.review_circuit_open_until_cycle = 10
        circuit.review_failure_consecutive = 3

        with patch(
            "src.utils.lightweight_lint.run_lightweight_lint",
            side_effect=RuntimeError("lint crash"),
        ):
            result = conduct_review(
                session=MagicMock(),
                settings=settings,
                project=project,
                send_prompt_with_retry_fn=lambda *a, **kw: None,
                build_review_exception_diagnostics_fn=lambda e, cycle: {},
                circuit=circuit,
                cycle=5,
            )

        # Should still return a valid result
        assert len(result.reviews) > 0
        assert any("熔断" in pr.suggestions[0] for pr in result.reviews)
