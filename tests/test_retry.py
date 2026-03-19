import pytest

from src.utils.retry import (
    NON_RETRYABLE_ERROR_PATTERNS,
    RETRYABLE_ERROR_PATTERNS,
    RetryPolicy,
    get_retry_delay,
    should_retry,
)


class TestRetryPatterns:
    def test_retryable_patterns_count(self):
        assert len(RETRYABLE_ERROR_PATTERNS) == 8

    def test_non_retryable_patterns_count(self):
        assert len(NON_RETRYABLE_ERROR_PATTERNS) == 3


class TestRetryPolicy:
    def test_default_values(self):
        policy = RetryPolicy()
        assert policy.max_retries == 3
        assert policy.retry_delay == 2.0
        assert policy.backoff_multiplier == 1.5

    def test_custom_values(self):
        policy = RetryPolicy(max_retries=5, retry_delay=1.0, backoff_multiplier=2.0)
        assert policy.max_retries == 5
        assert policy.retry_delay == 1.0
        assert policy.backoff_multiplier == 2.0


class TestShouldRetry:
    @pytest.mark.parametrize(
        "error_msg",
        [
            "Invalid params: xyz",
            "Connection timeout",
            "connection reset by peer",
            "rate limit exceeded",
            "ratelimit hit",
            "too many requests",
            "server is overloaded",
            "server error 500",
            "internal error occurred",
        ],
    )
    def test_retryable_errors(self, error_msg):
        assert should_retry(error_msg) is True

    @pytest.mark.parametrize(
        "error_msg",
        [
            "directory /tmp/xyz not found",
            "directory does not exist",
            "Permission denied: /etc/passwd",
            "Authentication failed for user",
        ],
    )
    def test_non_retryable_errors(self, error_msg):
        assert should_retry(error_msg) is False

    def test_unknown_error_returns_false(self):
        assert should_retry("unknown error") is False

    def test_exception_input(self):
        assert should_retry(Exception("timeout")) is True
        assert should_retry(ValueError("permission denied")) is False


class TestGetRetryDelay:
    def test_delay_calculation(self):
        policy = RetryPolicy(retry_delay=2.0, backoff_multiplier=1.5)
        assert get_retry_delay(0, policy) == 2.0
        assert get_retry_delay(1, policy) == 3.0
        assert get_retry_delay(2, policy) == 4.5

    def test_with_different_policy(self):
        policy = RetryPolicy(retry_delay=1.0, backoff_multiplier=2.0)
        assert get_retry_delay(0, policy) == 1.0
        assert get_retry_delay(1, policy) == 2.0
        assert get_retry_delay(2, policy) == 4.0
        assert get_retry_delay(3, policy) == 8.0
