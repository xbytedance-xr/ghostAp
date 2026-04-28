from __future__ import annotations

import contextvars
import logging
import threading
import time
from typing import Callable, Optional

from .models import ThreadContext

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 86400 * 7
_CLEANUP_INTERVAL = 3600


class ThreadContextManager:

    def __init__(self, ttl: float = _DEFAULT_TTL, cleanup_interval: float = _CLEANUP_INTERVAL, on_evict: Optional[Callable[[ThreadContext], None]] = None):
        self._contexts: dict[str, ThreadContext] = {}
        self._aliases: dict[str, str] = {}
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._ttl = ttl
        self._on_evict = on_evict
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            args=(cleanup_interval,),
            daemon=True,
            name="thread-ctx-cleanup",
        )
        self._cleanup_thread.start()

    def register(
        self,
        thread_root_id: str,
        chat_id: str,
        project_id: str,
        mode: str = "smart",
        tool_name: Optional[str] = None,
        model_name: Optional[str] = None,
        alias_keys: Optional[list[str]] = None,
    ) -> ThreadContext:
        ctx = ThreadContext(
            thread_root_id=thread_root_id,
            chat_id=chat_id,
            project_id=project_id,
            mode=mode,
            tool_name=tool_name,
            model_name=model_name,
        )
        with self._lock:
            self._contexts[thread_root_id] = ctx
            for alias in (alias_keys or []):
                if alias and alias != thread_root_id:
                    self._aliases[alias] = thread_root_id
                    self._contexts[alias] = ctx
        alias_info = ",".join((alias_keys or [])[:3]) if alias_keys else "none"
        logger.info(
            "[Thread] Registered: root=%s aliases=%s chat=%s project=%s mode=%s tool=%s model=%s",
            thread_root_id[:12],
            alias_info[:36],
            chat_id[:12],
            project_id,
            mode,
            tool_name,
            model_name,
        )
        return ctx

    def get(self, thread_root_id: str) -> Optional[ThreadContext]:
        with self._lock:
            ctx = self._contexts.get(thread_root_id)
            if ctx:
                ctx.touch()
        return ctx

    def get_by_chat(self, chat_id: str) -> list[ThreadContext]:
        with self._lock:
            seen: set[int] = set()
            result: list[ThreadContext] = []
            for c in self._contexts.values():
                if c.chat_id == chat_id and id(c) not in seen:
                    seen.add(id(c))
                    result.append(c)
            return result

    def update_mode(self, thread_root_id: str, mode: str) -> bool:
        with self._lock:
            ctx = self._contexts.get(thread_root_id)
            if not ctx:
                return False
            ctx.mode = mode
            ctx.touch()
        return True

    def update_tool(self, thread_root_id: str, tool_name: Optional[str], model_name: Optional[str] = None) -> bool:
        with self._lock:
            ctx = self._contexts.get(thread_root_id)
            if not ctx:
                return False
            if tool_name is not None:
                ctx.tool_name = tool_name
            if model_name is not None:
                ctx.model_name = model_name
            ctx.touch()
        return True

    def remove(self, thread_root_id: str) -> Optional[ThreadContext]:
        with self._lock:
            canonical = self._aliases.get(thread_root_id, thread_root_id)
            ctx = self._contexts.pop(canonical, None)
            if canonical != thread_root_id:
                self._contexts.pop(thread_root_id, None)
                self._aliases.pop(thread_root_id, None)
            alias_keys = [k for k, v in self._aliases.items() if v == canonical]
            for k in alias_keys:
                self._aliases.pop(k, None)
                self._contexts.pop(k, None)
        if ctx and self._on_evict:
            try:
                self._on_evict(ctx)
            except Exception:
                logger.debug("[Thread] on_evict callback error", exc_info=True)
        return ctx

    def remove_by_chat(self, chat_id: str) -> int:
        removed: list[ThreadContext] = []
        with self._lock:
            to_remove = {k for k, c in self._contexts.items() if c.chat_id == chat_id}
            seen_ids: set[int] = set()
            for k in to_remove:
                ctx = self._contexts.pop(k, None)
                if ctx and id(ctx) not in seen_ids:
                    seen_ids.add(id(ctx))
                    removed.append(ctx)
            stale_aliases = [a for a, c in self._aliases.items() if c in to_remove or a in to_remove]
            for a in stale_aliases:
                self._aliases.pop(a, None)
        if self._on_evict:
            for ctx in removed:
                try:
                    self._on_evict(ctx)
                except Exception:
                    logger.debug("[Thread] on_evict callback error", exc_info=True)
        return len(removed)

    @property
    def active_count(self) -> int:
        with self._lock:
            return len({id(c) for c in self._contexts.values()})

    def close(self) -> None:
        self._cleanup_stop.set()
        remaining: list[ThreadContext] = []
        with self._lock:
            seen: set[int] = set()
            for ctx in self._contexts.values():
                if id(ctx) not in seen:
                    seen.add(id(ctx))
                    remaining.append(ctx)
            self._contexts.clear()
            self._aliases.clear()
        if self._on_evict:
            for ctx in remaining:
                try:
                    self._on_evict(ctx)
                except Exception:
                    logger.debug("[Thread] on_evict callback error", exc_info=True)

    def _cleanup_loop(self, interval: float) -> None:
        while not self._cleanup_stop.wait(timeout=interval):
            try:
                self._evict_expired()
            except Exception:
                logger.debug("[Thread] Cleanup error", exc_info=True)

    def _evict_expired(self) -> None:
        now = time.time()
        evicted: list[ThreadContext] = []
        with self._lock:
            expired = {k for k, c in self._contexts.items() if (now - c.last_active) > self._ttl}
            seen: set[int] = set()
            for k in expired:
                ctx = self._contexts.pop(k, None)
                if ctx and id(ctx) not in seen:
                    seen.add(id(ctx))
                    evicted.append(ctx)
            stale_aliases = [a for a in self._aliases if a in expired or self._aliases[a] in expired]
            for a in stale_aliases:
                self._aliases.pop(a, None)
        if evicted:
            logger.info("[Thread] Evicted %d expired thread contexts", len(evicted))
        if self._on_evict:
            for ctx in evicted:
                try:
                    self._on_evict(ctx)
                except Exception:
                    logger.debug("[Thread] on_evict callback error", exc_info=True)


_manager: Optional[ThreadContextManager] = None
_manager_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

_current_thread_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("current_thread_id", default=None)
_current_sender_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("current_sender_id", default=None)
_current_sender_name: contextvars.ContextVar[str] = contextvars.ContextVar("current_sender_name", default="")
_current_is_p2p: contextvars.ContextVar[bool] = contextvars.ContextVar("current_is_p2p", default=False)


def get_thread_manager() -> ThreadContextManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ThreadContextManager()
    return _manager


def _reset_thread_manager_for_testing() -> None:
    """Reset the global ThreadContextManager singleton. **Test-only.**"""
    global _manager
    with _manager_lock:
        _manager = None


def set_current_thread_id(thread_id: Optional[str]) -> None:
    _current_thread_id.set(thread_id)


def get_current_thread_id() -> Optional[str]:
    return _current_thread_id.get()


def set_current_sender_id(sender_id: Optional[str]) -> None:
    _current_sender_id.set(sender_id)


def get_current_sender_id() -> Optional[str]:
    return _current_sender_id.get()


def set_current_is_p2p(is_p2p: bool) -> None:
    _current_is_p2p.set(is_p2p)


def get_current_is_p2p() -> bool:
    return _current_is_p2p.get()


def set_current_sender_name(name: str) -> None:
    _current_sender_name.set(name)


def get_current_sender_name() -> str:
    return _current_sender_name.get()
