import logging
import threading
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = ["HookEvent", "register_hook", "fire_hooks", "clear_hooks"]


class HookEvent(str, Enum):
    PRE_SHELL_EXECUTE = "pre_shell_execute"
    POST_SHELL_EXECUTE = "post_shell_execute"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    ENGINE_START = "engine_start"
    ENGINE_STOP = "engine_stop"
    ITERATION_DONE = "iteration_done"


_hooks: dict[HookEvent, list[Callable]] = {}
_hooks_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock


def register_hook(event: HookEvent, callback: Callable) -> Callable[[], None]:
    with _hooks_lock:
        _hooks.setdefault(event, []).append(callback)

    def unregister() -> None:
        with _hooks_lock:
            try:
                _hooks[event].remove(callback)
            except (KeyError, ValueError):
                pass

    return unregister


def fire_hooks(event: HookEvent, **kwargs: Any) -> None:
    with _hooks_lock:
        callbacks = list(_hooks.get(event, []))
    for cb in callbacks:
        try:
            cb(**kwargs)
        except Exception:
            logger.warning("hook %s callback %r failed", event.value, cb, exc_info=True)


def clear_hooks() -> None:
    with _hooks_lock:
        _hooks.clear()
