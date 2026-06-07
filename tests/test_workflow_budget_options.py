"""Tests for budget validation helpers in workflow_engine.constants."""

from __future__ import annotations

import pytest

from src.workflow_engine.constants import BUDGET_OPTIONS_VALUES, is_valid_budget


@pytest.mark.parametrize(
    "value",
    [
        500_000,
        1_500_000,
        2_000_000,
        5_000_000,
    ],
)
def test_valid_tier_values_return_true(value: int) -> None:
    assert is_valid_budget(value) is True


@pytest.mark.parametrize(
    "value",
    [
        1_234_567,
        99_999_999,
        0,
        -1,
        None,
        "2000000",
        2_000_000.5,
    ],
)
def test_invalid_values_return_false(value) -> None:
    assert is_valid_budget(value) is False


def test_budget_options_values_has_exactly_four_items() -> None:
    assert len(BUDGET_OPTIONS_VALUES) == 4
