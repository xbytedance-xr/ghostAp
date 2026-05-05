"""CardSession lifecycle hooks.

Provides the SessionHook protocol and concrete implementations for
Feishu-specific side effects (emoji reactions, context persistence).

Hooks are injected at CardSession construction time and called at:
- on_dispatched(event, state): after reduce, before deliver
- on_terminal(state, reason): after terminal event delivery succeeds

All hook callbacks are wrapped in try/except by the caller (CardSession),
so a failing hook never blocks the dispatch pipeline.
Terminal hooks have a 5s timeout to prevent network hangs from blocking the pipeline.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from src.card.engine_meta import engine_type_to_cmd

from src.card.events import CardEvent, CardEventType
from src.card.state.models import CardState, TerminalReason
from src.card.ui_text import UI_TEXT

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Timeout for terminal hooks (network-calling hooks like emoji/persistence)
HOOK_TIMEOUT_SECONDS = 5.0

# Timeout for dispatched hooks (lightweight, sync by nature)
DISPATCHED_HOOK_TIMEOUT = 3.0

# Threshold: rebuild executor after this many consecutive timeouts
_MAX_CONSECUTIVE_TIMEOUTS = 2


class _HookExecutorManager:
    """Manages the hook thread pool with lazy rebuild on consecutive timeouts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._executor = self._create_executor()
        self._consecutive_timeouts = 0
        self._semaphore = threading.BoundedSemaphore(self._get_max_workers() * 4)

    @staticmethod
    def _get_max_workers() -> int:
        max_workers = 4  # default
        try:
            from src.config import get_settings
            val = getattr(get_settings(), "hook_pool_max_workers", None)
            if isinstance(val, int) and val > 0:
                max_workers = val
        except Exception:
            pass
        return max_workers

    @staticmethod
    def _create_executor() -> concurrent.futures.ThreadPoolExecutor:
        max_workers = _HookExecutorManager._get_max_workers()
        return concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="hook")

    def submit(self, fn, *args):
        """Submit a callable to the executor with backpressure protection.

        If all worker slots are occupied, degrades to fire-and-forget (skips submission).
        """
        if not self._semaphore.acquire(blocking=False):
            logger.warning("Hook executor: backpressure triggered, skipping hook submission (all %d slots occupied)", self._get_max_workers())
            return None
        with self._lock:
            try:
                future = self._executor.submit(self._wrap_with_semaphore_release(fn), *args)
                return future
            except Exception:
                self._semaphore.release()
                raise

    def _wrap_with_semaphore_release(self, fn):
        """Wrap fn so semaphore is released after completion."""
        def wrapper(*args):
            try:
                return fn(*args)
            finally:
                self._semaphore.release()
        return wrapper

    def record_success(self) -> None:
        """Reset consecutive timeout counter on success."""
        with self._lock:
            self._consecutive_timeouts = 0

    def record_timeout(self) -> None:
        """Record a timeout and rebuild executor if threshold reached."""
        old_executor = None
        with self._lock:
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= _MAX_CONSECUTIVE_TIMEOUTS:
                logger.warning("Hook executor: %d consecutive timeouts, rebuilding thread pool", self._consecutive_timeouts)
                old_executor = self._executor
                self._executor = self._create_executor()
                self._consecutive_timeouts = 0
        # Shutdown old executor outside lock (non-blocking)
        if old_executor is not None:
            try:
                old_executor.shutdown(wait=False)
            except Exception:
                pass

    def shutdown(self) -> None:
        """Gracefully shut down the executor thread pool with timeout protection.

        Uses shutdown(wait=False, cancel_futures=True) then joins threads with
        an explicit timeout to prevent hanging the process exit.

        Called during process shutdown to ensure clean thread cleanup.
        """
        _SHUTDOWN_TIMEOUT = 10.0
        with self._lock:
            executor = self._executor
        # Signal shutdown and cancel pending futures immediately
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:
            logger.warning("Hook executor shutdown signal failed: %s", repr(exc))
            return
        # Join worker threads with timeout to avoid indefinite blocking
        for t in getattr(executor, "_threads", set()):
            try:
                t.join(timeout=_SHUTDOWN_TIMEOUT)
            except Exception:
                pass


_hook_executor_manager = _HookExecutorManager()


def shutdown_hook_executor() -> None:
    """Shut down the global hook executor (called during graceful shutdown)."""
    _hook_executor_manager.shutdown()


def _reset_hook_executor() -> None:
    """Reset the global hook executor manager (test isolation only).

    Rebuilds the executor and resets the consecutive timeout counter.
    Should be called via a pytest fixture to prevent cross-test state leakage.
    """
    global _hook_executor_manager
    _hook_executor_manager.shutdown()
    _hook_executor_manager = _HookExecutorManager()


@runtime_checkable
class SessionHook(Protocol):
    """Protocol for CardSession lifecycle hooks.

    Implementations may define either or both methods. Missing methods
    are treated as no-ops by the CardSession.
    """

    def on_dispatched(self, event: CardEvent, state: CardState) -> None:
        """Called after reduce+render, before deliver.

        Args:
            event: The event that was just dispatched.
            state: The new state after reduce.
        """
        ...

    def on_terminal(self, state: CardState, reason: TerminalReason) -> None:
        """Called after a terminal event (COMPLETED/FAILED/CANCELLED) is delivered.

        Args:
            state: The final card state.
            reason: Terminal reason (see TerminalReason Literal type).
        """
        ...


# ---------------------------------------------------------------------------
# Concrete hook implementations
# ---------------------------------------------------------------------------


class EmojiHook:
    """Adds emoji reactions on terminal events.

    - COMPLETED → success emoji (e.g. PARTY)
    - FAILED → error emoji (e.g. SOB)
    - CANCELLED / TTL_EXPIRED → stop emoji (⏹)

    Args:
        add_reaction: Callable(message_id, emoji_type) to add a Feishu reaction.
        message_id: The message to react to.
        success_emoji: Emoji type string for success (default: PARTY).
        error_emoji: Emoji type string for failure (default: SOB).
        stop_emoji: Emoji type string for cancelled/ttl_expired (default: STOP).
    """

    SUCCESS_EMOJI_DEFAULT = "PARTY"
    ERROR_EMOJI_DEFAULT = "SOB"
    STOP_EMOJI_DEFAULT = "STOP"

    def __init__(
        self,
        add_reaction: Callable[[str, str], Any],
        message_id: str,
        *,
        chat_id: str | None = None,
        success_emoji: str | None = None,
        error_emoji: str | None = None,
        stop_emoji: str | None = None,
    ) -> None:
        self._add_reaction = add_reaction
        self._message_id = message_id
        self._chat_id = chat_id
        self._success_emoji = success_emoji or self.SUCCESS_EMOJI_DEFAULT
        self._error_emoji = error_emoji or self.ERROR_EMOJI_DEFAULT
        self._stop_emoji = stop_emoji or self.STOP_EMOJI_DEFAULT

    def on_dispatched(self, event: CardEvent, state: CardState) -> None:
        """No-op for dispatched events."""

    def on_terminal(self, state: CardState, reason: TerminalReason) -> None:
        """Add emoji reaction based on terminal reason.

        Archived sessions (rotated out) get no emoji reaction.
        When message_id is empty, gracefully skip (degraded mode for worktree without reply_to).
        """
        if reason == "archived":
            return
        if not self._message_id:
            logger.debug("EmojiHook: skipping reaction (no message_id, chat_id=%s)", self._chat_id)
            return
        if reason in ("completed", "completed_empty"):
            self._add_reaction(self._message_id, self._success_emoji)
        elif reason in ("failed",):
            self._add_reaction(self._message_id, self._error_emoji)
        elif reason in ("cancelled", "ttl_expired"):
            self._add_reaction(self._message_id, self._stop_emoji)


class ContextPersistenceHook:
    """Persists engine results to project context on successful completion.

    On terminal(COMPLETED): calls update_context and optionally create_version.

    Args:
        update_fn: Callable to update the project context.
            Signature: update_fn(state) -> None
            The callable receives the final CardState and should extract
            whatever data it needs (this keeps the hook decoupled from
            specific context_manager APIs).
        notify_callback: Optional callable(chat_id, text) to notify user on failure.
            If provided, sends a text message when persistence fails.
        chat_id: Chat ID for notify_callback (required if notify_callback is set).
    """

    def __init__(
        self,
        update_fn: Callable[[CardState], None],
        notify_callback: Callable[[str, str], None] | None = None,
        chat_id: str | None = None,
        engine_type: str | None = None,
    ) -> None:
        self._update_fn = update_fn
        self._notify_callback = notify_callback
        self._chat_id = chat_id
        self._engine_type = engine_type

    def on_dispatched(self, event: CardEvent, state: CardState) -> None:
        """No-op for dispatched events."""

    def on_terminal(self, state: CardState, reason: TerminalReason) -> None:
        """Persist context on successful completion."""
        if reason in ("completed", "completed_empty"):
            try:
                self._update_fn(state)
            except Exception as exc:
                logger.warning("ContextPersistenceHook: persistence failed: %s", repr(exc))
                if self._notify_callback and self._chat_id:
                    try:
                        engine_cmd = engine_type_to_cmd(self._engine_type, fallback=UI_TEXT["card_session_fallback_cmd"])
                        self._notify_callback(
                            self._chat_id,
                            UI_TEXT["hook_persistence_failed_notice"].format(engine_cmd=engine_cmd),
                        )
                    except Exception as notify_exc:
                        logger.debug("ContextPersistenceHook: notify_callback failed: %s", repr(notify_exc))


# ---------------------------------------------------------------------------
# HookFirer: encapsulates hook dispatch logic extracted from CardSession
# ---------------------------------------------------------------------------


class HookFirer:
    """Fires hooks on behalf of CardSession.

    Extracted to keep CardSession focused on dispatch→reduce→render→deliver.
    Thread-safety: all methods are safe to call from any thread.
    """

    def __init__(
        self,
        hooks: tuple[SessionHook, ...],
        session_id: str,
        *,
        executor: _HookExecutorManager | None = None,
    ) -> None:
        self._hooks = hooks
        self._session_id = session_id
        self._executor = executor or _hook_executor_manager
        self._fired = threading.Event()  # ensures fire_terminal executes at most once
        self._fire_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    @property
    def has_hooks(self) -> bool:
        return bool(self._hooks)

    def fire_dispatched(self, event: CardEvent, state: CardState | None) -> None:
        """Fire on_dispatched hooks (fire-and-forget).

        Hooks are submitted to the shared executor and return immediately —
        no synchronous wait on the dispatch hot path.  Exceptions are logged
        asynchronously via Future done-callbacks.  Hooks exceeding
        DISPATCHED_HOOK_TIMEOUT trigger executor health tracking.
        """
        if not self._hooks or state is None:
            return
        sid = self._session_id
        executor = self._executor
        import time
        submit_time = time.monotonic()
        for hook in self._hooks:
            future = executor.submit(hook.on_dispatched, event, state)
            if future is not None:
                hook_name = type(hook).__name__
                future.add_done_callback(
                    lambda f, _sid=sid, _name=hook_name, _t0=submit_time, _ex=executor: self._on_dispatched_done(f, _sid, _name, _t0, _ex)
                )

    @staticmethod
    def _on_dispatched_done(f: concurrent.futures.Future, sid: str, hook_name: str, submit_time: float, executor: _HookExecutorManager) -> None:
        """Done-callback for dispatched hooks: log errors and detect timeouts."""
        import time
        exc = f.exception()
        if exc is not None:
            logger.warning("HookFirer %s: on_dispatched failed (%s): %s", sid, hook_name, repr(exc))
        elapsed = time.monotonic() - submit_time
        if elapsed > DISPATCHED_HOOK_TIMEOUT:
            logger.warning(
                "HookFirer %s: on_dispatched slow (%s): %.2fs > %.1fs threshold",
                sid, hook_name, elapsed, DISPATCHED_HOOK_TIMEOUT,
            )
            executor.record_timeout()
        elif exc is None:
            executor.record_success()

    def fire_terminal(self, state: CardState | None, reason: str) -> None:
        """Fire on_terminal hooks in parallel with unified timeout.

        All hooks are submitted concurrently, then waited on with a single
        HOOK_TIMEOUT_SECONDS deadline. This bounds worst-case blocking to
        HOOK_TIMEOUT_SECONDS regardless of hook count (not N × timeout).

        Execution order is NOT guaranteed across hooks.
        Timeouts and exceptions are logged and swallowed.

        Exactly-once: subsequent calls are no-ops after the first successful invocation.
        """
        # Exactly-once guard: only the first caller proceeds
        with self._fire_lock:
            if self._fired.is_set():
                logger.debug("HookFirer %s: fire_terminal already fired, skipping", self._session_id)
                return
            self._fired.set()

        if not self._hooks or state is None:
            logger.debug("HookFirer %s: fire_terminal skipped (no hooks or state=None)", self._session_id)
            return

        # Submit all hooks in parallel
        future_to_hook: dict[concurrent.futures.Future, SessionHook] = {}
        for hook in self._hooks:
            try:
                future = self._executor.submit(hook.on_terminal, state, reason)
                if future is not None:
                    future_to_hook[future] = hook
            except Exception as exc:
                logger.warning(
                    "HookFirer %s: on_terminal submit failed (%s): %s",
                    self._session_id, type(hook).__name__, repr(exc),
                )

        if not future_to_hook:
            return

        # Wait for all futures with a single timeout deadline
        done, not_done = concurrent.futures.wait(
            future_to_hook.keys(), timeout=HOOK_TIMEOUT_SECONDS
        )

        # Process completed futures
        has_timeout = False
        for future in done:
            hook = future_to_hook[future]
            try:
                future.result(timeout=0)  # Already done, just check for exceptions
            except Exception as exc:
                logger.warning(
                    "HookFirer %s: on_terminal failed (%s): %s",
                    self._session_id, type(hook).__name__, repr(exc),
                )

        # Handle timed-out futures
        for future in not_done:
            hook = future_to_hook[future]
            future.cancel()
            has_timeout = True
            logger.warning(
                "HookFirer %s: on_terminal timed out (%s) after %.1fs",
                self._session_id, type(hook).__name__, HOOK_TIMEOUT_SECONDS,
            )

        # Update executor health tracking
        if has_timeout:
            self._executor.record_timeout()
        elif done:
            self._executor.record_success()
