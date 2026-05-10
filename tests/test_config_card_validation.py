"""Tests for CardSessionConfig field validation (max_task_cards bounds + ticker_interval)."""

import pytest
from pydantic import ValidationError

from src.config import CardSessionConfig


class TestMaxTaskCardsValidation:
    """AC13: max_task_cards must be in [1, 20]."""

    def test_zero_raises_validation_error(self):
        with pytest.raises(ValidationError, match="card_max_task_cards 必须在"):
            CardSessionConfig(max_task_cards=0)

    def test_negative_raises_validation_error(self):
        with pytest.raises(ValidationError, match="card_max_task_cards 必须在"):
            CardSessionConfig(max_task_cards=-1)

    def test_one_is_valid(self):
        cfg = CardSessionConfig(max_task_cards=1)
        assert cfg.max_task_cards == 1

    def test_twenty_is_valid(self):
        cfg = CardSessionConfig(max_task_cards=20)
        assert cfg.max_task_cards == 20

    def test_twenty_one_raises_validation_error(self):
        """AC13: max_task_cards > 20 should raise ValidationError."""
        with pytest.raises(ValidationError, match="card_max_task_cards 必须在"):
            CardSessionConfig(max_task_cards=21)

    def test_large_value_raises_validation_error(self):
        with pytest.raises(ValidationError, match="card_max_task_cards 必须在"):
            CardSessionConfig(max_task_cards=100)

    def test_default_is_eight(self):
        cfg = CardSessionConfig()
        assert cfg.max_task_cards == 8

    def test_non_numeric_string_raises_friendly_error(self):
        """AC11: 'abc' as max_task_cards should raise with friendly message."""
        with pytest.raises(ValidationError, match="必须为有效整数"):
            CardSessionConfig(max_task_cards="abc")

    def test_none_raises_friendly_error(self):
        """None as max_task_cards should raise with friendly message."""
        with pytest.raises(ValidationError, match="必须为有效整数"):
            CardSessionConfig(max_task_cards=None)


class TestTickerIntervalValidation:
    """ticker_interval must be > 0 (gt=0 Pydantic constraint)."""

    def test_ticker_interval_default_is_1_2(self):
        config = CardSessionConfig()
        assert config.ticker_interval == 1.2

    def test_ticker_interval_zero_raises(self):
        with pytest.raises(ValidationError):
            CardSessionConfig(ticker_interval=0)

    def test_ticker_interval_negative_raises(self):
        with pytest.raises(ValidationError):
            CardSessionConfig(ticker_interval=-1.0)
