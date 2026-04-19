"""Unit tests for src/utils/review_helpers.py shared functions."""

import pytest

from src.utils.review_helpers import (
    build_review_error_suggestion,
    compute_adaptive_timeout,
    compute_exponential_cooldown,
)


# ---------------------------------------------------------------------------
# build_review_error_suggestion
# ---------------------------------------------------------------------------

class TestBuildReviewErrorSuggestion:
    def test_timeout_returns_timeout_text(self):
        result = build_review_error_suggestion(fail_reason="timeout")
        assert "超时" in result
        assert "审查超时" in result

    def test_empty_error_text_returns_retry(self):
        result = build_review_error_suggestion(fail_reason="exception", error_text="", err_repr="")
        assert result == "审查执行异常，将在下一轮重试"

    def test_empty_message_marker_returns_retry(self):
        result = build_review_error_suggestion(
            fail_reason="exception",
            error_text="TimeoutError (empty message)",
            err_repr="",
        )
        assert result == "审查执行异常，将在下一轮重试"

    def test_empty_message_in_err_repr_returns_retry(self):
        result = build_review_error_suggestion(
            fail_reason="exception",
            error_text="",
            err_repr="TimeoutError (empty message)",
        )
        assert result == "审查执行异常，将在下一轮重试"

    def test_real_error_text_included(self):
        result = build_review_error_suggestion(
            fail_reason="exception",
            error_text="Connection refused",
            err_repr="",
        )
        assert result == "审查执行异常: Connection refused"

    def test_err_repr_used_when_error_text_empty(self):
        result = build_review_error_suggestion(
            fail_reason="exception",
            error_text="",
            err_repr="RuntimeError('bad state')",
        )
        assert "审查执行异常: RuntimeError('bad state')" == result

    def test_all_defaults_returns_retry(self):
        result = build_review_error_suggestion()
        assert result == "审查执行异常，将在下一轮重试"

    def test_whitespace_only_error_text(self):
        result = build_review_error_suggestion(fail_reason="exception", error_text="   ", err_repr="   ")
        assert result == "审查执行异常，将在下一轮重试"


# ---------------------------------------------------------------------------
# compute_exponential_cooldown
# ---------------------------------------------------------------------------

class TestComputeExponentialCooldown:
    def test_level_0(self):
        assert compute_exponential_cooldown(0, base_cooldown=3, max_cooldown=12) == 3

    def test_level_1(self):
        assert compute_exponential_cooldown(1, base_cooldown=3, max_cooldown=12) == 6

    def test_level_2(self):
        assert compute_exponential_cooldown(2, base_cooldown=3, max_cooldown=12) == 12

    def test_level_3_capped(self):
        assert compute_exponential_cooldown(3, base_cooldown=3, max_cooldown=12) == 12

    def test_level_10_capped(self):
        assert compute_exponential_cooldown(10, base_cooldown=3, max_cooldown=12) == 12

    def test_negative_level_treated_as_0(self):
        assert compute_exponential_cooldown(-1, base_cooldown=3, max_cooldown=12) == 3

    def test_custom_base_and_max(self):
        assert compute_exponential_cooldown(0, base_cooldown=5, max_cooldown=20) == 5
        assert compute_exponential_cooldown(1, base_cooldown=5, max_cooldown=20) == 10
        assert compute_exponential_cooldown(2, base_cooldown=5, max_cooldown=20) == 20
        assert compute_exponential_cooldown(3, base_cooldown=5, max_cooldown=20) == 20


# ---------------------------------------------------------------------------
# compute_adaptive_timeout
# ---------------------------------------------------------------------------

class TestComputeAdaptiveTimeout:
    def test_n0_returns_base(self):
        assert compute_adaptive_timeout(0, base_timeout=120, min_timeout=30) == 120

    def test_n1_halves(self):
        assert compute_adaptive_timeout(1, base_timeout=120, min_timeout=30) == 60

    def test_n2_quarters(self):
        assert compute_adaptive_timeout(2, base_timeout=120, min_timeout=30) == 30

    def test_n3_floored(self):
        assert compute_adaptive_timeout(3, base_timeout=120, min_timeout=30) == 30

    def test_n10_floored(self):
        assert compute_adaptive_timeout(10, base_timeout=120, min_timeout=30) == 30

    def test_negative_n_treated_as_0(self):
        assert compute_adaptive_timeout(-1, base_timeout=120, min_timeout=30) == 120

    def test_custom_base_and_min(self):
        assert compute_adaptive_timeout(0, base_timeout=60, min_timeout=10) == 60
        assert compute_adaptive_timeout(1, base_timeout=60, min_timeout=10) == 30
        assert compute_adaptive_timeout(2, base_timeout=60, min_timeout=10) == 15
        assert compute_adaptive_timeout(3, base_timeout=60, min_timeout=10) == 10
