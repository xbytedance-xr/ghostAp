"""Environment variable utilities for subprocess spawning."""

from __future__ import annotations

import os
from typing import Optional


# Keys that must be removed to avoid nested-session guard crashes
# when spawning ACP / CLI subprocesses from within a wrapper environment.
_GUARD_KEYS = ("CLAUDECODE",)


def build_clean_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Return a copy of *base* (default: ``os.environ``) with guard keys removed.

    This centralises the ``os.environ.copy(); env.pop("CLAUDECODE", None)``
    pattern used across ACP, agent-session, and provider modules.
    """
    env = dict(base) if base is not None else os.environ.copy()
    for key in _GUARD_KEYS:
        env.pop(key, None)
    return env
