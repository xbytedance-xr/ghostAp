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

_shutdown_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
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
    # Fence the delivery pool first: reject new submissions so drain is bounded
    try:
        from src.card.delivery.pool import shutdown_delivery_pool
        shutdown_delivery_pool(wait=False)
    except Exception:
        logger.debug("delivery pool fence skipped or failed")
    # Drain in-flight deliveries: wait while instances are still alive
    try:
        from src.card.delivery.registry import delivery_registry
        delivery_registry.drain_in_flight(timeout=5.0)
    except Exception:
        logger.debug("delivery drain skipped or failed")
    # Terminate all pending timers before shutting down delivery —
    # prevents TTL callbacks from firing against already-closed deliveries
    try:
        from src.card.timers.scheduler import get_timer_scheduler
        get_timer_scheduler().shutdown(timeout=2.0)
    except Exception:
        logger.debug("TimerScheduler shutdown skipped or failed")
    # Then shut down CardDelivery (discard instances, stop eviction threads)
    try:
        from src.card.delivery.registry import delivery_registry as _reg
        _reg.shutdown_all()
    except Exception:
        logger.debug("CardDelivery shutdown skipped or failed")
    # Final cleanup: wait for any remaining pool threads to finish
    try:
        from src.card.delivery.pool import shutdown_delivery_pool as _final_pool_shutdown
        _final_pool_shutdown(wait=True)
    except Exception:
        logger.debug("delivery pool final shutdown skipped or failed")
    # Then shut down hook executor thread pool (all pending hooks should be drained)
    try:
        from src.card.hooks import shutdown_hook_executor
        shutdown_hook_executor()
    except Exception:
        logger.debug("hook executor shutdown skipped or failed")
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
    with _shutdown_lock:
        _shutdown_in_progress = False


