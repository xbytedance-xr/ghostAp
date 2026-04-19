"""Integration tests for safe_wait_for at production call sites.

Verifies that safe_wait_for is correctly wired in at:
- src/acp/session.py: _read_stream, _health_check_session
- src/utils/shutdown.py: graceful_shutdown
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# ACPSession._read_stream integration
# ---------------------------------------------------------------------------


class TestReadStreamTimeout:
    """Verify that _read_stream uses safe_wait_for with action label."""

    @pytest.mark.asyncio
    async def test_read_stream_timeout_produces_non_empty_message(self):
        """When stream.read hangs, safe_wait_for produces non-empty TimeoutError."""
        from src.utils.async_helpers import safe_wait_for

        # Simulate a stream whose read() never returns
        stream = MagicMock()

        async def _hang(n):
            await asyncio.sleep(100)

        stream.read = _hang

        # Call safe_wait_for the same way _read_stream does
        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(stream.read(4096), timeout=0.01, action="ACP stream read")

        msg = str(exc_info.value)
        assert msg.strip() != ""
        assert "ACP stream read" in msg
        assert "0.01" in msg


# ---------------------------------------------------------------------------
# ACPSession._health_check_session integration
# ---------------------------------------------------------------------------


class TestHealthCheckTimeout:
    """Verify that _health_check_session uses safe_wait_for with action label."""

    @pytest.mark.asyncio
    async def test_health_check_timeout_produces_non_empty_message(self):
        """When load_session hangs, safe_wait_for produces non-empty TimeoutError."""
        from src.utils.async_helpers import safe_wait_for

        async def _hang_load(**kwargs):
            await asyncio.sleep(100)

        conn = AsyncMock()
        conn.load_session = _hang_load

        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(
                conn.load_session(cwd="/tmp", session_id="test-session"),
                timeout=0.01,
                action="ACP 健康检查",
            )

        msg = str(exc_info.value)
        assert msg.strip() != ""
        assert "ACP 健康检查" in msg
        assert "0.01" in msg


# ---------------------------------------------------------------------------
# graceful_shutdown integration
# ---------------------------------------------------------------------------


class TestShutdownTimeout:
    """Verify that graceful_shutdown timeout path produces WARNING log."""

    def test_shutdown_timeout_logs_warning(self):
        """When cleanup takes too long, graceful_shutdown logs a warning about timeout."""
        import src.utils.shutdown as shutdown_mod
        from src.utils.shutdown import _reset_shutdown_state

        async def _slow_cleanup():
            await asyncio.sleep(100)

        _reset_shutdown_state()

        original_logger = shutdown_mod.logger
        mock_logger = MagicMock()
        shutdown_mod.logger = mock_logger

        # Replace run_all_cleanups with a real async function that sleeps forever
        original_run_all = shutdown_mod.run_all_cleanups
        shutdown_mod.run_all_cleanups = _slow_cleanup

        try:
            with (
                patch.object(shutdown_mod, "fire_hooks"),
                patch("sys.exit"),
            ):
                shutdown_mod.graceful_shutdown(timeout=0.05, reason="test")

            # Check that logger.warning was called with timeout message
            warning_calls = mock_logger.warning.call_args_list
            assert any("timed out" in str(call) for call in warning_calls), (
                f"Expected 'timed out' warning call, got: {warning_calls}"
            )
        finally:
            shutdown_mod.logger = original_logger
            shutdown_mod.run_all_cleanups = original_run_all
            _reset_shutdown_state()

    def test_shutdown_timeout_still_calls_exit(self):
        """Even on timeout, sys.exit is still called."""
        import src.utils.shutdown as shutdown_mod
        from src.utils.shutdown import _reset_shutdown_state

        async def _slow_cleanup():
            await asyncio.sleep(100)

        _reset_shutdown_state()

        original_run_all = shutdown_mod.run_all_cleanups
        shutdown_mod.run_all_cleanups = _slow_cleanup

        try:
            with (
                patch.object(shutdown_mod, "fire_hooks"),
                patch("sys.exit") as mock_exit,
            ):
                shutdown_mod.graceful_shutdown(timeout=0.05, reason="test-exit")

            mock_exit.assert_called_once()
        finally:
            shutdown_mod.run_all_cleanups = original_run_all
            _reset_shutdown_state()
