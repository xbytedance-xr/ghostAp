"""Async helper utilities — safe wrappers around asyncio primitives."""

from __future__ import annotations

import asyncio
from typing import TypeVar

__all__ = ["safe_wait_for"]

_T = TypeVar("_T")


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
