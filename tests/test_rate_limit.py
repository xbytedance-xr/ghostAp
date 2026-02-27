"""Tests for rate-limit detection and RateLimitAwareSession retry logic."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.acp.models import ACPEvent, ACPEventType, PromptResult
from src.agent_session import (
    RateLimitAwareSession,
    _detect_rate_limit,
)


# ======================================================================
# _detect_rate_limit() tests
# ======================================================================


class TestDetectRateLimit:
    """Test rate-limit error detection from exception messages."""

    def test_detects_rate_limit_keyword(self):
        err = Exception("Request failed: rate_limit exceeded")
        assert _detect_rate_limit(err) is not None

    def test_detects_rate_limit_with_space(self):
        err = Exception("rate limit reached")
        assert _detect_rate_limit(err) is not None

    def test_detects_429_status(self):
        err = Exception("HTTP 429 Too Many Requests")
        result = _detect_rate_limit(err)
        assert result is not None

    def test_429_not_matched_as_substring(self):
        """429 must be a word boundary, not part of another number."""
        err = Exception("Error code 14291")
        assert _detect_rate_limit(err) is None

    def test_detects_too_many_requests(self):
        err = Exception("too many requests, please slow down")
        assert _detect_rate_limit(err) is not None

    def test_detects_overloaded(self):
        err = Exception("The server is overloaded")
        assert _detect_rate_limit(err) is not None

    def test_extracts_retry_after_seconds(self):
        err = Exception("rate_limit: retry_after=60")
        result = _detect_rate_limit(err)
        assert result == 60

    def test_extracts_retry_after_with_colon(self):
        err = Exception("Rate limit hit. Retry-After: 120")
        result = _detect_rate_limit(err)
        assert result == 120

    def test_clamps_retry_after_to_max_600(self):
        err = Exception("rate_limit retry_after=9999")
        result = _detect_rate_limit(err)
        assert result == 600

    def test_clamps_retry_after_to_min_1(self):
        err = Exception("rate_limit retry_after=0")
        result = _detect_rate_limit(err)
        assert result == 1

    def test_returns_zero_when_no_retry_after(self):
        err = Exception("rate limit exceeded")
        result = _detect_rate_limit(err)
        assert result == 0

    def test_returns_none_for_non_rate_limit_error(self):
        err = Exception("Connection refused")
        assert _detect_rate_limit(err) is None

    def test_returns_none_for_empty_message(self):
        err = Exception("")
        assert _detect_rate_limit(err) is None

    def test_case_insensitive(self):
        err = Exception("RATE LIMIT")
        assert _detect_rate_limit(err) is not None


# ======================================================================
# RateLimitAwareSession tests
# ======================================================================


def _make_mock_session(**overrides):
    """Create a mock SyncSession with sensible defaults."""
    s = MagicMock()
    s.session_id = overrides.get("session_id", "test-session-id")
    s.created_at = overrides.get("created_at", 1000.0)
    s.last_active = overrides.get("last_active", 1000.0)
    s.message_count = overrides.get("message_count", 0)
    s.last_query = overrides.get("last_query", "")
    s.is_resumed = overrides.get("is_resumed", False)
    s.send_prompt.return_value = PromptResult(stop_reason="end_turn", text="ok")
    return s


class TestRateLimitAwareSession:
    """Test RateLimitAwareSession wrapper."""

    def test_delegates_protocol_properties(self):
        inner = _make_mock_session(session_id="abc", message_count=5)
        wrapped = RateLimitAwareSession(inner)
        assert wrapped.session_id == "abc"
        assert wrapped.message_count == 5

    def test_delegates_start(self):
        inner = _make_mock_session()
        inner.start.return_value = "sid"
        wrapped = RateLimitAwareSession(inner)
        assert wrapped.start(startup_timeout=30) == "sid"
        inner.start.assert_called_once_with(startup_timeout=30)

    def test_delegates_close(self):
        inner = _make_mock_session()
        wrapped = RateLimitAwareSession(inner)
        wrapped.close()
        inner.close.assert_called_once()

    def test_delegates_cancel(self):
        inner = _make_mock_session()
        cancel_ev = threading.Event()
        wrapped = RateLimitAwareSession(inner, cancel_event=cancel_ev)
        wrapped.cancel()
        inner.cancel.assert_called_once()
        assert cancel_ev.is_set()

    def test_send_prompt_passes_through_on_success(self):
        inner = _make_mock_session()
        expected = PromptResult(stop_reason="end_turn", text="hello")
        inner.send_prompt.return_value = expected
        wrapped = RateLimitAwareSession(inner)
        result = wrapped.send_prompt("test")
        assert result == expected

    @patch("src.agent_session.get_settings")
    def test_send_prompt_bypasses_retry_when_disabled(self, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = False
        mock_settings.return_value = settings

        inner = _make_mock_session()
        inner.send_prompt.side_effect = Exception("rate_limit exceeded")
        wrapped = RateLimitAwareSession(inner)

        with pytest.raises(Exception, match="rate_limit exceeded"):
            wrapped.send_prompt("test")

    @patch("src.agent_session.get_settings")
    def test_retries_on_rate_limit_then_succeeds(self, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = True
        settings.rate_limit_max_retries = 3
        settings.rate_limit_max_wait = 300
        settings.rate_limit_base_wait = 1  # fast for test
        mock_settings.return_value = settings

        inner = _make_mock_session()
        expected = PromptResult(stop_reason="end_turn", text="ok")
        inner.send_prompt.side_effect = [
            Exception("rate_limit exceeded"),
            expected,
        ]

        callback = MagicMock()
        wrapped = RateLimitAwareSession(inner, on_rate_limit=callback)
        result = wrapped.send_prompt("test")

        assert result == expected
        assert inner.send_prompt.call_count == 2
        callback.assert_called_once()

    @patch("src.agent_session.get_settings")
    def test_raises_after_max_retries_exhausted(self, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = True
        settings.rate_limit_max_retries = 2
        settings.rate_limit_max_wait = 300
        settings.rate_limit_base_wait = 1
        mock_settings.return_value = settings

        inner = _make_mock_session()
        inner.send_prompt.side_effect = Exception("rate_limit exceeded")

        wrapped = RateLimitAwareSession(inner)
        with pytest.raises(Exception, match="rate_limit exceeded"):
            wrapped.send_prompt("test")

        # 1 initial + 2 retries = 3 total
        assert inner.send_prompt.call_count == 3

    @patch("src.agent_session.get_settings")
    def test_non_rate_limit_error_not_retried(self, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = True
        settings.rate_limit_max_retries = 3
        settings.rate_limit_max_wait = 300
        settings.rate_limit_base_wait = 1
        mock_settings.return_value = settings

        inner = _make_mock_session()
        inner.send_prompt.side_effect = Exception("Connection refused")

        wrapped = RateLimitAwareSession(inner)
        with pytest.raises(Exception, match="Connection refused"):
            wrapped.send_prompt("test")

        assert inner.send_prompt.call_count == 1

    @patch("src.agent_session.get_settings")
    def test_cancel_interrupts_rate_limit_wait(self, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = True
        settings.rate_limit_max_retries = 3
        settings.rate_limit_max_wait = 300
        settings.rate_limit_base_wait = 60  # long wait
        mock_settings.return_value = settings

        inner = _make_mock_session()
        inner.send_prompt.side_effect = Exception("rate_limit exceeded")

        cancel_ev = threading.Event()
        wrapped = RateLimitAwareSession(inner, cancel_event=cancel_ev)

        # Cancel after a short delay
        def _cancel():
            time.sleep(0.3)
            cancel_ev.set()

        t = threading.Thread(target=_cancel, daemon=True)
        t.start()

        start = time.monotonic()
        with pytest.raises(Exception, match="rate_limit"):
            wrapped.send_prompt("test")
        elapsed = time.monotonic() - start

        # Should have been interrupted well before the 60s wait
        assert elapsed < 5

    @patch("src.agent_session.get_settings")
    def test_callback_exception_does_not_break_retry(self, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = True
        settings.rate_limit_max_retries = 3
        settings.rate_limit_max_wait = 300
        settings.rate_limit_base_wait = 1
        mock_settings.return_value = settings

        inner = _make_mock_session()
        expected = PromptResult(stop_reason="end_turn", text="ok")
        inner.send_prompt.side_effect = [
            Exception("rate_limit exceeded"),
            expected,
        ]

        def bad_callback(wait_secs):
            raise RuntimeError("callback exploded")

        wrapped = RateLimitAwareSession(inner, on_rate_limit=bad_callback)
        result = wrapped.send_prompt("test")

        # Should still succeed despite callback error
        assert result == expected

    @patch("src.agent_session.get_settings")
    def test_rate_limit_until_set_during_wait(self, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = True
        settings.rate_limit_max_retries = 3
        settings.rate_limit_max_wait = 300
        settings.rate_limit_base_wait = 2
        mock_settings.return_value = settings

        inner = _make_mock_session()
        expected = PromptResult(stop_reason="end_turn", text="ok")
        inner.send_prompt.side_effect = [
            Exception("rate_limit exceeded"),
            expected,
        ]

        wrapped = RateLimitAwareSession(inner)

        # Check rate_limit_until is set during the wait window
        observed_until = []

        def _observer():
            time.sleep(0.2)
            observed_until.append(wrapped.rate_limit_until)

        t = threading.Thread(target=_observer, daemon=True)
        t.start()

        result = wrapped.send_prompt("test")
        t.join(timeout=5)

        assert result == expected
        # During the wait, rate_limit_until should have been a non-None monotonic deadline
        assert len(observed_until) == 1
        assert observed_until[0] is not None

    def test_to_snapshot_delegates(self):
        inner = _make_mock_session()
        inner.to_snapshot.return_value = {"session_id": "x", "backend": "cli"}
        wrapped = RateLimitAwareSession(inner)
        assert wrapped.to_snapshot() == {"session_id": "x", "backend": "cli"}

    def test_get_session_info_delegates(self):
        inner = _make_mock_session()
        inner.get_session_info.return_value = "info"
        wrapped = RateLimitAwareSession(inner)
        assert wrapped.get_session_info() == "info"

    def test_is_server_running_delegates(self):
        inner = _make_mock_session()
        inner.is_server_running.return_value = True
        wrapped = RateLimitAwareSession(inner)
        assert wrapped.is_server_running() is True

    def test_is_server_healthy_delegates(self):
        inner = _make_mock_session()
        inner.is_server_healthy.return_value = False
        wrapped = RateLimitAwareSession(inner)
        assert wrapped.is_server_healthy(healthcheck_timeout=5.0) is False
        inner.is_server_healthy.assert_called_once_with(healthcheck_timeout=5.0)


# ======================================================================
# create_engine_session wrapping tests
# ======================================================================


class TestCreateEngineSession:
    """Test that create_engine_session wraps with RateLimitAwareSession."""

    @patch("src.agent_session.get_settings")
    @patch("src.agent_session.SyncClaudeCLISession")
    def test_claude_wrapped_when_enabled(self, MockCLI, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = True
        settings.acp_startup_timeout = 20
        mock_settings.return_value = settings

        mock_session = MagicMock()
        MockCLI.return_value = mock_session

        from src.agent_session import create_engine_session
        result = create_engine_session("claude", "/tmp")

        assert isinstance(result, RateLimitAwareSession)
        mock_session.start.assert_called_once()

    @patch("src.agent_session.get_settings")
    @patch("src.agent_session.SyncClaudeCLISession")
    def test_claude_not_wrapped_when_disabled(self, MockCLI, mock_settings):
        settings = MagicMock()
        settings.rate_limit_retry_enabled = False
        settings.acp_startup_timeout = 20
        mock_settings.return_value = settings

        mock_session = MagicMock()
        MockCLI.return_value = mock_session

        from src.agent_session import create_engine_session
        result = create_engine_session("claude", "/tmp")

        assert not isinstance(result, RateLimitAwareSession)
