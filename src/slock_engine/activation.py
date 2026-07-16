"""Shared serialization boundary for Slock channel activation identity."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import wraps

_SLOCK_ACTIVATION_LOCK = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock


@contextmanager
def slock_activation_guard():
    """Freeze all activated-channel bindings for a short dispatch commit."""

    with _SLOCK_ACTIVATION_LOCK:
        yield


def activation_serialized(function):
    """Make every direct engine activation mutation share the public guard."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        with slock_activation_guard():
            return function(*args, **kwargs)

    return wrapped


__all__ = ["activation_serialized", "slock_activation_guard"]
