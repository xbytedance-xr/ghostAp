"""Non-blocking synchronous callback bridge for async hire activities."""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future as ConcurrentFuture
from typing import Any


class CallbackBridgeError(RuntimeError):
    """A bridged callback failed without exposing callback arguments."""


Callback = Callable[..., object | Awaitable[object]]


class AsyncCallbackBridge:
    """Schedule synchronous SDK callbacks and durably drain them before return.

    Lark invokes registration callbacks synchronously.  The bridge therefore
    only enqueues callback work from that stack; the owning async activity
    later calls :meth:`drain` before it returns.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._mutex = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._pending: set[asyncio.Future[Any] | ConcurrentFuture[Any]] = set()

    def callback(
        self,
        target: Callback | None,
        *prefix: object,
    ) -> Callable[..., None]:
        """Return a synchronous, non-blocking callback accepted by the SDK."""

        def enqueue(*args: object) -> None:
            if target is None:
                return
            self._submit(target, *prefix, *args)

        return enqueue

    def _submit(self, target: Callback, *args: object) -> None:
        async def invoke() -> None:
            if inspect.iscoroutinefunction(target):
                result = target(*args)
            else:
                result = await asyncio.to_thread(target, *args)
            if inspect.isawaitable(result):
                await result

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self._loop:
            future: asyncio.Future[Any] | ConcurrentFuture[Any] = (
                self._loop.create_task(invoke())
            )
        else:
            future = asyncio.run_coroutine_threadsafe(invoke(), self._loop)
        with self._mutex:
            self._pending.add(future)

    async def drain(self) -> None:
        """Wait for every callback queued before the activity completes."""

        while True:
            with self._mutex:
                pending = tuple(self._pending)
                self._pending.clear()
            if not pending:
                return
            awaitables = [
                future
                if isinstance(future, asyncio.Future)
                else asyncio.wrap_future(future, loop=self._loop)
                for future in pending
            ]
            try:
                await asyncio.gather(*awaitables)
            except Exception:
                raise CallbackBridgeError("registration callback failed") from None


__all__ = ["AsyncCallbackBridge", "CallbackBridgeError"]
