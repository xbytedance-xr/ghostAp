from __future__ import annotations

import asyncio
import logging
import signal
import sys
import threading

from .async_helpers import safe_wait_for
from .cleanup import run_all_cleanups
from .hooks import HookEvent, fire_hooks

logger = logging.getLogger(__name__)

__all__ = ["is_shutting_down", "graceful_shutdown", "install_signal_handlers"]

_shutdown_lock = threading.Lock()
_shutdown_in_progress: bool = False


def is_shutting_down() -> bool:
    return _shutdown_in_progress


def graceful_shutdown(exit_code: int = 0, *, reason: str = "", timeout: float = 10.0) -> None:
    global _shutdown_in_progress
    with _shutdown_lock:
        if _shutdown_in_progress:
            return
        _shutdown_in_progress = True
    logger.info("graceful shutdown initiated%s", f": {reason}" if reason else "")
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(safe_wait_for(run_all_cleanups(), timeout=timeout, action="graceful shutdown cleanup"))
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("cleanup timed out after %.1fs", timeout)
        finally:
            loop.close()
    except Exception:
        logger.exception("error during cleanup")
    fire_hooks(HookEvent.SESSION_END)
    sys.exit(exit_code)


def install_signal_handlers() -> None:
    def _handler(signum: int, _frame: object) -> None:
        graceful_shutdown(reason=f"signal {signal.Signals(signum).name}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handler)
    if sys.platform == "darwin":
        signal.signal(signal.SIGHUP, _handler)


def _reset_shutdown_state() -> None:
    global _shutdown_in_progress
    _shutdown_in_progress = False
