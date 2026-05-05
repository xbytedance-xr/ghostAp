"""Backward-compatible re-export — canonical location: src.card.actions.dispatch

DEPRECATED (deprecated in v0.1.0): This shim will be removed after 2026-06-01.
Import directly from ``src.card.actions.dispatch`` instead.

Design choice: uses ``__getattr__`` + ``__dir__`` lazy-deprecation pattern so that
merely importing the module does NOT emit a warning. The DeprecationWarning fires
only when the caller actually accesses a name, giving library consumers time to
migrate without noisy import-time warnings polluting test output.
"""

from __future__ import annotations

import datetime
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

_DEADLINE = datetime.date(2026, 6, 1)

__all__ = [
    "APPROVE_ACTION",
    "CANCEL_FORCE_RELEASE",
    "CANCEL_LOCK",
    "CONFIRM_FORCE_RELEASE",
    "CONFIRM_LOCK",
    "CONTINUE_DEV",
    "CardEvent",
    "CardEventType",
    "DEEP_RESUME",
    "DEEP_STOP",
    "ENGINE_RESTART",
    "ENGINE_STOP",
    "ENTER_DEEP_PROMPT",
    "FORCE_RELEASE_REPO_LOCK",
    "HELP_CATEGORY",
    "LIST_FILES",
    "LOOP_RESUME",
    "LOOP_STOP",
    "MODE_COMPACT",
    "MODE_FULL",
    "NEW_PROJECT_PROMPT",
    "REFRESH_ACP_MODELS",
    "REFRESH_BOARD",
    "REFRESH_TTADK_MODELS",
    "REJECT_ACTION",
    "RETRY_COMMAND",
    "SELECT_ACP_MODEL",
    "SELECT_ACP_TOOL",
    "SELECT_TTADK_COMBINED",
    "SELECT_TTADK_COMBINED_TOOL",
    "SELECT_TTADK_MODEL",
    "SELECT_TTADK_TOOL",
    "SHOW_ACP_MENU",
    "SHOW_BOARD",
    "SHOW_DEEP_STATUS",
    "SHOW_DETAIL",
    "SHOW_HELP_MENU",
    "SHOW_STATUS",
    "SHOW_TTADK_MENU",
    "SHOW_WORKTREE_MENU",
    "SHOW_WORKTREE_MERGE_ENTRY",
    "SPEC_RESUME",
    "SPEC_SKIP_RETRY",
    "SPEC_STOP",
    "SWITCH_BOARD_PAGE",
    "SWITCH_PROJECT",
    "SWITCH_TO",
    "TOGGLE_TTADK_YOLO",
    "TTL_KEEP_ALIVE",
    "WORKTREE_CANCEL",
    "WORKTREE_CLEANUP",
    "WORKTREE_CONFIRM_START",
    "WORKTREE_EXECUTE_ACTION",
    "WORKTREE_FINISH_SELECTION",
    "WORKTREE_MERGE",
    "WORKTREE_RETRY_ALL",
    "WORKTREE_RETRY_FAILED",
    "WORKTREE_SELECT_MODEL",
    "WORKTREE_SELECT_TOOL",
    "build_worktree_action_registry",
]


def __getattr__(name: str) -> "Any":
    if name in __all__:
        if datetime.date.today() > _DEADLINE:
            raise ImportError(
                f"{__name__}.{name} has been removed (deadline {_DEADLINE}). "
                "Use src.card.actions.dispatch instead."
            )
        warnings.warn(
            f"Accessing '{name}' from src.card.action_ids is deprecated (deprecated in v0.1.0), "
            "use src.card.actions.dispatch instead. "
            "This shim will be removed after 2026-06-01.",
            DeprecationWarning,
            stacklevel=2,
        )
        import src.card.actions.dispatch as _dispatch

        obj = getattr(_dispatch, name)
        globals()[name] = obj  # Cache: subsequent access skips __getattr__
        return obj
    raise AttributeError(f"module 'src.card.action_ids' has no attribute {name!r}")


def __dir__() -> list[str]:
    return __all__
