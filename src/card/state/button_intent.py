"""ButtonIntent: Abstract button intents for all reducers.

Reducers emit ButtonIntent values instead of concrete action_id strings.
The render layer maps intents to action_ids via render/buttons.py.
"""

from __future__ import annotations

from enum import Enum


class ButtonIntent(str, Enum):
    """Abstract button intent identifiers used by reducers.

    These are decoupled from the concrete action_id strings used in
    Feishu card schemas. Mapping is handled by render/buttons.py.
    """
    # Worktree flow
    WORKTREE_FINISH_SELECTION = "intent.worktree.finish_selection"
    WORKTREE_CONFIRM_START = "intent.worktree.confirm_start"
    WORKTREE_MERGE = "intent.worktree.merge"
    WORKTREE_CLEANUP = "intent.worktree.cleanup"
    WORKTREE_RETRY_FAILED = "intent.worktree.retry_failed"
    WORKTREE_RETRY_ALL = "intent.worktree.retry_all"
    WORKTREE_CANCEL = "intent.worktree.cancel"
    WORKTREE_SHOW_MENU = "intent.worktree.show_menu"
    WORKTREE_MODIFY_TARGET = "intent.worktree.modify_target"

    # Engine control (shared across deep/spec)
    ENGINE_STOP = "intent.engine.stop"

    # Deep engine
    DEEP_RESUME = "intent.deep.resume"
    DEEP_STOP = "intent.deep.stop"

    # Spec engine
    SPEC_RESUME = "intent.spec.resume"
    SPEC_STOP = "intent.spec.stop"
    SPEC_SKIP_RETRY = "intent.spec.skip_retry"

    # View mode toggle
    MODE_FULL = "intent.mode.full"
    MODE_COMPACT = "intent.mode.compact"

    # Global
    SHOW_STATUS = "intent.global.show_status"

    # Approval
    APPROVE = "intent.approval.approve"
    REJECT = "intent.approval.reject"
