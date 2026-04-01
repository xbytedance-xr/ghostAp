from __future__ import annotations

import asyncio
import threading
from typing import Awaitable, Callable

__all__ = ["register_cleanup", "run_all_cleanups", "cleanup_count"]

_lock = threading.Lock()
_cleanup_fns: set[Callable[[], Awaitable[None]]] = set()


def register_cleanup(fn: Callable[[], Awaitable[None]]) -> Callable[[], None]:
    with _lock:
        _cleanup_fns.add(fn)

    def _unregister() -> None:
        with _lock:
            _cleanup_fns.discard(fn)

    return _unregister


async def run_all_cleanups() -> None:
    with _lock:
        fns = list(_cleanup_fns)
        _cleanup_fns.clear()
    if fns:
        await asyncio.gather(*(fn() for fn in fns), return_exceptions=True)


def cleanup_count() -> int:
    return len(_cleanup_fns)
