"""Backward-compatible re-export — canonical location: src.card.timers.scheduler

DEPRECATED (deprecated in v0.1.0): This shim will be removed after 2026-06-01.
Import directly from ``src.card.timers.scheduler`` instead.
"""

import datetime
import warnings

__all__ = ["TimerHandle", "TimerScheduler", "get_timer_scheduler", "_reset_global_scheduler"]

_CANONICAL = "src.card.timers.scheduler"
_DEADLINE = datetime.date(2026, 6, 1)


def __getattr__(name: str):  # PEP 562
    if name in __all__:
        if datetime.date.today() > _DEADLINE:
            raise ImportError(
                f"{__name__}.{name} has been removed (deadline {_DEADLINE}). "
                f"Use {_CANONICAL}.{name} instead."
            )
        from src.card.timers.scheduler import (  # noqa: F401
            TimerHandle,
            TimerScheduler,
            get_timer_scheduler,
            _reset_global_scheduler,
        )

        _map = {
            "TimerHandle": TimerHandle,
            "TimerScheduler": TimerScheduler,
            "get_timer_scheduler": get_timer_scheduler,
            "_reset_global_scheduler": _reset_global_scheduler,
        }
        warnings.warn(
            f"{__name__}.{name} is deprecated (deprecated in v0.1.0), use {_CANONICAL}.{name} instead. "
            "This shim will be removed after 2026-06-01.",
            DeprecationWarning,
            stacklevel=2,
        )
        globals()[name] = _map[name]
        return _map[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
