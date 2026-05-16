"""Tests for src.utils.async_helpers — safe_wait_for."""

from __future__ import annotations

import asyncio

import pytest

from src.utils.async_helpers import safe_wait_for

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fast_return(value: str = "ok") -> str:
    return value


async def _slow_return(delay: float = 10.0) -> str:
    await asyncio.sleep(delay)
    return "done"


async def _raise_value_error() -> None:
    raise ValueError("boom")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSafeWaitFor:
    """Core functionality of safe_wait_for."""

    @pytest.mark.asyncio
    async def test_normal_return(self):
        """Coroutine completes within timeout → returns value."""
        result = await safe_wait_for(_fast_return("hello"), timeout=5.0)
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_timeout_raises_with_default_message(self):
        """Timeout → TimeoutError with non-empty default message."""
        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(_slow_return(), timeout=0.01)

        msg = str(exc_info.value)
        assert msg  # 非空
        assert "超时" in msg
        assert "0.01" in msg  # 包含超时秒数

    @pytest.mark.asyncio
    async def test_timeout_raises_with_custom_action(self):
        """Timeout with custom action label → label appears in message."""
        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(_slow_return(), timeout=0.01, action="ACP 健康检查")

        msg = str(exc_info.value)
        assert "ACP 健康检查" in msg
        assert "0.01" in msg

    @pytest.mark.asyncio
    async def test_non_timeout_exception_passes_through(self):
        """Non-timeout exceptions propagate unchanged."""
        with pytest.raises(ValueError, match="boom"):
            await safe_wait_for(_raise_value_error(), timeout=5.0)

    @pytest.mark.asyncio
    async def test_timeout_error_is_not_asyncio_timeout(self):
        """Raised exception is builtin TimeoutError (not asyncio.TimeoutError)."""
        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(_slow_return(), timeout=0.01)

        # 确保是 builtin TimeoutError
        assert type(exc_info.value) is TimeoutError

    @pytest.mark.asyncio
    async def test_timeout_chained_from_original(self):
        """The __cause__ chain preserves the original asyncio.TimeoutError."""
        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(_slow_return(), timeout=0.01)

        assert exc_info.value.__cause__ is not None

    @pytest.mark.asyncio
    async def test_str_never_empty(self):
        """str() of the raised TimeoutError is never empty."""
        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(_slow_return(), timeout=0.01)

        assert str(exc_info.value).strip() != ""

    @pytest.mark.asyncio
    async def test_future_support(self):
        """Works with asyncio.Future as well as coroutines."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut.set_result(42)
        result = await safe_wait_for(fut, timeout=5.0)
        assert result == 42

    # --- Boundary timeout tests ---

    @pytest.mark.asyncio
    async def test_tiny_timeout_raises_non_empty(self):
        """Extremely small timeout (0.001s) still produces a non-empty TimeoutError."""
        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(_slow_return(), timeout=0.001)

        msg = str(exc_info.value)
        assert msg.strip() != ""
        assert "超时" in msg
        assert "0.001" in msg

    @pytest.mark.asyncio
    async def test_tiny_timeout_with_action(self):
        """Tiny timeout with custom action label."""
        with pytest.raises(TimeoutError) as exc_info:
            await safe_wait_for(_slow_return(), timeout=0.001, action="边界测试")

        msg = str(exc_info.value)
        assert "边界测试" in msg
        assert "0.001" in msg

    # --- Cancellation behaviour tests ---

    @pytest.mark.asyncio
    async def test_coroutine_cancelled_on_timeout(self):
        """After timeout, the underlying coroutine's task is cancelled."""
        cancel_observed = False

        async def _trackable():
            nonlocal cancel_observed
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancel_observed = True
                raise

        with pytest.raises(TimeoutError):
            await safe_wait_for(_trackable(), timeout=0.01)

        # Give event loop a tick to process the cancellation
        await asyncio.sleep(0.05)
        assert cancel_observed, "Underlying coroutine was not cancelled after timeout"

    @pytest.mark.asyncio
    async def test_timeout_does_not_cancel_fast_coro(self):
        """A coroutine that finishes in time is NOT cancelled."""
        result = await safe_wait_for(_fast_return("ok"), timeout=5.0)
        assert result == "ok"
