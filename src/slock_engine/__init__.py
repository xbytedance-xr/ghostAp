"""Slock Engine — Multi-Agent collaboration engine using mouthpiece pattern."""

from __future__ import annotations

import importlib as _importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .engine import SlockEngine, SlockEngineCallbacks
    from .manager import SlockEngineManager
    from .models import AgentIdentity, AgentStatus, SlockChannel, SlockMemory, SlockTask, TaskStatus

__all__ = [
    "SlockEngine",
    "SlockEngineCallbacks",
    "SlockEngineManager",
    "AgentIdentity",
    "AgentStatus",
    "SlockChannel",
    "SlockMemory",
    "SlockTask",
    "TaskStatus",
]

# Lazy attribute access — defer heavy imports until actually needed.
_SUBMODULE_MAP: dict[str, tuple[str, str]] = {
    "SlockEngine": (".engine", "SlockEngine"),
    "SlockEngineCallbacks": (".engine", "SlockEngineCallbacks"),
    "SlockEngineManager": (".manager", "SlockEngineManager"),
    "AgentIdentity": (".models", "AgentIdentity"),
    "AgentStatus": (".models", "AgentStatus"),
    "SlockChannel": (".models", "SlockChannel"),
    "SlockMemory": (".models", "SlockMemory"),
    "SlockTask": (".models", "SlockTask"),
    "TaskStatus": (".models", "TaskStatus"),
}


def __getattr__(name: str):
    if name in _SUBMODULE_MAP:
        module_path, attr = _SUBMODULE_MAP[name]
        mod = _importlib.import_module(module_path, __package__)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
