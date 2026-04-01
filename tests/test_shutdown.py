import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.utils.shutdown import (
    _reset_shutdown_state,
    graceful_shutdown,
    is_shutting_down,
)


@pytest.fixture(autouse=True)
def _reset_state():
    _reset_shutdown_state()
    yield
    _reset_shutdown_state()


class TestShutdown:
    def test_graceful_shutdown_runs_cleanups(self):
        mock_cleanup = AsyncMock()
        with (
            patch("src.utils.shutdown.run_all_cleanups", mock_cleanup),
            patch("src.utils.shutdown.fire_hooks") as mock_hooks,
            pytest.raises(SystemExit) as exc_info,
        ):
            graceful_shutdown(exit_code=0, reason="test")
        mock_cleanup.assert_awaited_once()
        mock_hooks.assert_called_once()
        assert exc_info.value.code == 0

    def test_idempotent(self):
        call_count = 0
        original_cleanup = AsyncMock()

        async def counting_cleanup():
            nonlocal call_count
            call_count += 1
            await original_cleanup()

        with (
            patch("src.utils.shutdown.run_all_cleanups", counting_cleanup),
            patch("src.utils.shutdown.fire_hooks"),
            pytest.raises(SystemExit),
        ):
            graceful_shutdown(reason="first")

        graceful_shutdown(reason="second")

        assert call_count == 1

    def test_is_shutting_down(self):
        assert is_shutting_down() is False
        with (
            patch("src.utils.shutdown.run_all_cleanups", AsyncMock()),
            patch("src.utils.shutdown.fire_hooks"),
            pytest.raises(SystemExit),
        ):
            graceful_shutdown()
        assert is_shutting_down() is True

    def test_timeout_handling(self):
        async def slow_cleanup():
            await asyncio.sleep(100)

        with (
            patch("src.utils.shutdown.run_all_cleanups", slow_cleanup),
            patch("src.utils.shutdown.fire_hooks") as mock_hooks,
            pytest.raises(SystemExit) as exc_info,
        ):
            graceful_shutdown(exit_code=1, reason="timeout test", timeout=0.1)

        mock_hooks.assert_called_once()
        assert exc_info.value.code == 1
