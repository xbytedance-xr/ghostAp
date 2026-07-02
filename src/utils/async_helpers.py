"""Async helper utilities — safe wrappers around asyncio primitives."""

from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, TypeVar

__all__ = ["run_async", "safe_wait_for"]

_T = TypeVar("_T")

# ---------------------------------------------------------------------------
# Thread-safe async bridge — replaces scattered _get_shared_loop() / asyncio.run()
# ---------------------------------------------------------------------------

_BRIDGE_LOOP: asyncio.AbstractEventLoop | None = None
_BRIDGE_LOCK = threading.Lock()


def _get_bridge_loop() -> asyncio.AbstractEventLoop:
    """Get or create the singleton bridge event loop running in a daemon thread."""
    global _BRIDGE_LOOP
    with _BRIDGE_LOCK:
        if _BRIDGE_LOOP is not None and _BRIDGE_LOOP.is_running():
            return _BRIDGE_LOOP

        loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, name="async-bridge", daemon=True)
        t.start()
        _BRIDGE_LOOP = loop
        return loop


def run_async(coro: Awaitable[_T], *, timeout: float | None = None) -> _T:
    """Run an async coroutine from synchronous code, thread-safely.

    Uses a shared daemon-thread event loop. Safe to call from any thread
    (including ThreadPoolExecutor workers).

    Parameters
    ----------
    coro:
        The coroutine to execute.
    timeout:
        Optional timeout in seconds. Raises TimeoutError if exceeded.
    """
    loop = _get_bridge_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()
        raise


async def safe_wait_for(
    coro: "asyncio.coroutines" | asyncio.Future,  # type: ignore[type-arg]
    timeout: float,
    *,
    action: str = "",
) -> _T:
    """``asyncio.wait_for`` wrapper that guarantees a non-empty TimeoutError message.

    ``asyncio.wait_for`` raises ``asyncio.TimeoutError()`` with **no message**
    (``str(e) == ""``).  Down-stream formatting helpers (``fmt_error``,
    ``get_error_detail``) already guard against this, but wrapping at source is
    the cheapest defence.

    Parameters
    ----------
    coro:
        Awaitable to run with a deadline.
    timeout:
        Seconds before cancellation.
    action:
        Human-readable label injected into the ``TimeoutError`` message when
        the original exception carries no text.  Example: ``"ACP 健康检查"``.
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        msg = str(exc).strip()
        if not msg:
            label = action or "操作"
            msg = f"{label}超时 ({timeout}s)"
        raise TimeoutError(msg) from exc
