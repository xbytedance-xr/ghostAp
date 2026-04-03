from __future__ import annotations

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
        self._lock = threading.Lock()
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
        logger.info(
            "[Thread] Registered: root=%s chat=%s project=%s mode=%s tool=%s model=%s",
            thread_root_id[:12],
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
            return [c for c in self._contexts.values() if c.chat_id == chat_id]

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
            ctx = self._contexts.pop(thread_root_id, None)
        if ctx and self._on_evict:
            try:
                self._on_evict(ctx)
            except Exception:
                logger.debug("[Thread] on_evict callback error", exc_info=True)
        return ctx

    def remove_by_chat(self, chat_id: str) -> int:
        removed: list[ThreadContext] = []
        with self._lock:
            to_remove = [k for k, c in self._contexts.items() if c.chat_id == chat_id]
            for k in to_remove:
                removed.append(self._contexts.pop(k))
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
            return len(self._contexts)

    def close(self) -> None:
        self._cleanup_stop.set()
        remaining: list[ThreadContext] = []
        with self._lock:
            remaining = list(self._contexts.values())
            self._contexts.clear()
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
            expired = [k for k, c in self._contexts.items() if (now - c.last_active) > self._ttl]
            for k in expired:
                ctx = self._contexts.pop(k)
                evicted.append(ctx)
        if evicted:
            logger.info("[Thread] Evicted %d expired thread contexts", len(evicted))
        if self._on_evict:
            for ctx in evicted:
                try:
                    self._on_evict(ctx)
                except Exception:
                    logger.debug("[Thread] on_evict callback error", exc_info=True)


_manager: Optional[ThreadContextManager] = None
_manager_lock = threading.Lock()

_current_thread_id: threading.local = threading.local()


def get_thread_manager() -> ThreadContextManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ThreadContextManager()
    return _manager


def set_current_thread_id(thread_id: Optional[str]) -> None:
    _current_thread_id.value = thread_id


def get_current_thread_id() -> Optional[str]:
    return getattr(_current_thread_id, "value", None)
