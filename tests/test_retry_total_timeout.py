"""Tests for RetryPolicy.total_timeout and prompt_with_retry total_timeout kwarg.

Covers:
(a) total_timeout 到期后停止重试并抛出 TimeoutError
(b) 累计耗时 < total_timeout 时正常重试
(c) total_timeout=None 时保持原有行为不变
(d) total_timeout 与 cancel_event 交互正确
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from src.utils.retry import RetryPolicy, prompt_with_retry


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _RetryableError(Exception):
    """Error whose str matches the 'timeout' retryable pattern."""

    def __init__(self):
        super().__init__("timeout error for test")


class _NonRetryableError(Exception):
    def __init__(self):
        super().__init__("permission denied")


def _make_cancel_event() -> threading.Event:
    return threading.Event()


# ---------------------------------------------------------------------------
# (a) total_timeout 到期后停止重试
# ---------------------------------------------------------------------------


class TestTotalTimeoutExpired:
    """When cumulative time exceeds total_timeout, retry must stop."""

    def test_total_timeout_via_kwarg(self):
        """Explicit total_timeout kwarg triggers TimeoutError."""
        call_count = 0

        def action():
            nonlocal call_count
            call_count += 1
            time.sleep(0.15)
            raise _RetryableError()

        policy = RetryPolicy(max_retries=10, retry_delay=0.01, jitter_factor=0)
        with pytest.raises(TimeoutError, match="总耗时"):
            prompt_with_retry(
                action,
                _make_cancel_event(),
                retry_policy=policy,
                total_timeout=0.3,
            )
        # Should have attempted a few times but NOT all 11
        assert 1 <= call_count <= 5

    def test_total_timeout_via_policy_field(self):
        """total_timeout set on RetryPolicy itself also works."""
        call_count = 0

        def action():
            nonlocal call_count
            call_count += 1
            time.sleep(0.15)
            raise _RetryableError()

        policy = RetryPolicy(
            max_retries=10, retry_delay=0.01, jitter_factor=0, total_timeout=0.3,
        )
        with pytest.raises(TimeoutError, match="总耗时"):
            prompt_with_retry(action, _make_cancel_event(), retry_policy=policy)
        assert 1 <= call_count <= 5

    def test_kwarg_overrides_policy_field(self):
        """Explicit kwarg total_timeout takes precedence over policy field."""
        call_count = 0

        def action():
            nonlocal call_count
            call_count += 1
            time.sleep(0.1)
            raise _RetryableError()

        # Policy says 999s but kwarg says 0.2s → should timeout quickly
        policy = RetryPolicy(
            max_retries=10, retry_delay=0.01, jitter_factor=0, total_timeout=999,
        )
        with pytest.raises(TimeoutError, match="总耗时"):
            prompt_with_retry(
                action,
                _make_cancel_event(),
                retry_policy=policy,
                total_timeout=0.2,
            )
        assert call_count <= 5


# ---------------------------------------------------------------------------
# (b) 累计耗时 < total_timeout 时正常重试
# ---------------------------------------------------------------------------


class TestTotalTimeoutNotExpired:
    """When total_timeout is generous, retries proceed normally."""

    def test_succeeds_within_budget(self):
        """Action succeeds on 3rd attempt well within total_timeout."""
        call_count = 0

        def action():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _RetryableError()
            return "ok"

        policy = RetryPolicy(max_retries=5, retry_delay=0.01, jitter_factor=0)
        result = prompt_with_retry(
            action,
            _make_cancel_event(),
            retry_policy=policy,
            total_timeout=10.0,
        )
        assert result == "ok"
        assert call_count == 3

    def test_non_retryable_error_still_raises(self):
        """Non-retryable errors raise immediately regardless of total_timeout."""
        call_count = 0

        def action():
            nonlocal call_count
            call_count += 1
            raise _NonRetryableError()

        policy = RetryPolicy(max_retries=5, retry_delay=0.01, jitter_factor=0)
        with pytest.raises(_NonRetryableError):
            prompt_with_retry(
                action,
                _make_cancel_event(),
                retry_policy=policy,
                total_timeout=10.0,
            )
        assert call_count == 1


# ---------------------------------------------------------------------------
# (c) total_timeout=None 时保持原有行为不变
# ---------------------------------------------------------------------------


class TestTotalTimeoutNone:
    """When total_timeout is None, behaviour is identical to before."""

    def test_none_allows_all_retries(self):
        """All max_retries attempts are made when total_timeout is None."""
        call_count = 0

        def action():
            nonlocal call_count
            call_count += 1
            raise _RetryableError()

        policy = RetryPolicy(max_retries=3, retry_delay=0.01, jitter_factor=0)
        with pytest.raises(_RetryableError):
            prompt_with_retry(action, _make_cancel_event(), retry_policy=policy)
        assert call_count == 4  # initial + 3 retries

    def test_default_policy_has_none_total_timeout(self):
        """Default RetryPolicy has total_timeout=None."""
        p = RetryPolicy()
        assert p.total_timeout is None


# ---------------------------------------------------------------------------
# (d) total_timeout 与 cancel_event 交互正确
# ---------------------------------------------------------------------------


class TestTotalTimeoutCancelInteraction:
    """cancel_event still works when total_timeout is set."""

    def test_cancel_event_set_during_delay(self):
        """Setting cancel_event during retry delay raises the last error."""
        call_count = 0
        cancel = threading.Event()

        def action():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Schedule cancel during the delay phase
                threading.Timer(0.05, cancel.set).start()
            raise _RetryableError()

        policy = RetryPolicy(max_retries=10, retry_delay=5.0, jitter_factor=0)
        with pytest.raises(_RetryableError):
            prompt_with_retry(
                action,
                cancel,
                retry_policy=policy,
                total_timeout=60.0,
            )
        # Cancel was set after first attempt, so should stop early
        assert call_count <= 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestTotalTimeoutEdgeCases:
    def test_total_timeout_zero_allows_first_attempt(self):
        """total_timeout=0 still allows the first attempt (check is attempt>0)."""
        call_count = 0

        def action():
            nonlocal call_count
            call_count += 1
            raise _RetryableError()

        policy = RetryPolicy(max_retries=5, retry_delay=0.01, jitter_factor=0)
        with pytest.raises((TimeoutError, _RetryableError)):
            prompt_with_retry(
                action,
                _make_cancel_event(),
                retry_policy=policy,
                total_timeout=0.0,
            )
        # At least the first attempt should have been made
        assert call_count >= 1

    def test_before_retry_still_called(self):
        """before_retry callback is invoked even with total_timeout set."""
        call_count = 0
        before_retry_mock = MagicMock()

        def action():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _RetryableError()
            return "done"

        policy = RetryPolicy(max_retries=5, retry_delay=0.01, jitter_factor=0)
        result = prompt_with_retry(
            action,
            _make_cancel_event(),
            retry_policy=policy,
            total_timeout=10.0,
            before_retry=before_retry_mock,
        )
        assert result == "done"
        assert before_retry_mock.call_count == 2
