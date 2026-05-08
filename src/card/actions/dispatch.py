"""Action dispatch: centralized action_id constants + action registries.

All action_id strings used in reducers, handlers, and action registries
are defined here. Registry factories (e.g. build_worktree_action_registry)
map action_ids to CardEvent constructors for CardSession injection.
"""
from __future__ import annotations

from collections.abc import Callable

# ---------------------------------------------------------------------------
# Approval actions
# ---------------------------------------------------------------------------
APPROVE_ACTION = "approve_action"  # User approves a pending approval request
REJECT_ACTION = "reject_action"  # User rejects a pending approval request

# ---------------------------------------------------------------------------
# Spec engine actions
# ---------------------------------------------------------------------------
SPEC_STOP = "spec_stop"  # Force-stop current Spec engine execution
SPEC_SKIP_RETRY = "spec_skip_retry"  # Skip retry and accept current cycle result
SPEC_RESUME = "spec_resume"  # Resume/retry Spec engine after failure

# ---------------------------------------------------------------------------
# Deep engine actions
# ---------------------------------------------------------------------------
DEEP_RESUME = "deep_resume"  # Resume/retry Deep engine after failure
DEEP_STOP = "deep_stop"  # Force-stop current Deep engine execution

# ---------------------------------------------------------------------------
# Generic engine actions
# ---------------------------------------------------------------------------
ENGINE_STOP = "engine_stop"  # Generic stop — routed by engine_type at dispatch time
ENGINE_RESTART = "engine_restart"  # Restart engine after TTL timeout or completion
TTL_KEEP_ALIVE = "ttl_keep_alive"  # User requests to keep session alive (reset idle timer)
MODE_FULL = "mode_full"  # Switch card to full (detailed) view mode
MODE_COMPACT = "mode_compact"  # Switch card to compact (minimal) view mode

# ---------------------------------------------------------------------------
# Loop engine actions
# ---------------------------------------------------------------------------
LOOP_RESUME = "loop_resume"  # Resume/retry Loop engine after failure
LOOP_STOP = "loop_stop"  # Force-stop current Loop engine execution

# ---------------------------------------------------------------------------
# Worktree actions
# ---------------------------------------------------------------------------
WORKTREE_FINISH_SELECTION = "worktree_finish_selection"  # Confirm tool selection and proceed to config
WORKTREE_CONFIRM_START = "worktree_confirm_start"  # Confirm config and start parallel execution
WORKTREE_MERGE = "worktree_merge"  # Trigger merge of all worktree branches into base
WORKTREE_CLEANUP = "worktree_cleanup"  # Delete worktree branches and local refs (irreversible)
WORKTREE_RETRY_FAILED = "worktree_retry_failed"  # Retry only failed execution units
WORKTREE_RETRY_ALL = "worktree_retry_all"  # Retry all execution units from scratch
WORKTREE_CANCEL = "worktree_cancel"  # Cancel worktree flow before execution starts
WORKTREE_SELECT_TOOL = "worktree_select_tool"  # Toggle selection of a specific tool in the tool list
WORKTREE_SELECT_MODEL = "worktree_select_model"  # Select a model for the chosen tool
WORKTREE_REMOVE_ITEM = "worktree_remove_item"  # Remove a single item from the selected list
WORKTREE_CLEAR_ITEMS = "worktree_clear_items"  # Clear all selected items and restart selection
WORKTREE_EXECUTE_ACTION = "worktree_execute_action"  # Trigger execution of ready units
SHOW_WORKTREE_MENU = "show_worktree_menu"  # Return to tool selection menu
SHOW_WORKTREE_MERGE_ENTRY = "show_worktree_merge_entry"  # Show merge entry card with branch details

# ---------------------------------------------------------------------------
# Global / status actions
# ---------------------------------------------------------------------------
SHOW_STATUS = "show_status"  # Show current session status card
SHOW_BOARD = "show_board"  # Show project dashboard
REFRESH_BOARD = "refresh_board"  # Refresh dashboard data
SWITCH_PROJECT = "switch_project"  # Switch active project context
SWITCH_BOARD_PAGE = "switch_board_page"  # Navigate between dashboard pages
SHOW_DETAIL = "show_detail"  # Show detailed info for a specific item
SWITCH_TO = "switch_to"  # Switch to a different programming mode
CONTINUE_DEV = "continue_dev"  # Continue development in current session
LIST_FILES = "list_files"  # List project files
NEW_PROJECT_PROMPT = "new_project_prompt"  # Prompt user to create a new project
SHOW_HELP_MENU = "show_help_menu"  # Display help menu card
ENTER_DEEP_PROMPT = "enter_deep_prompt"  # Quick-enter Deep mode from status card
SHOW_DEEP_STATUS = "show_deep_status"  # Show Deep engine execution status
RETRY_COMMAND = "retry_command"  # Retry the last failed command
HELP_CATEGORY = "help_category"  # Navigate to a specific help category

# ---------------------------------------------------------------------------
# TTADK actions
# ---------------------------------------------------------------------------
SELECT_TTADK_TOOL = "select_ttadk_tool"  # Select a TTADK tool from the list
TOGGLE_TTADK_YOLO = "toggle_ttadk_yolo"  # Toggle YOLO (auto-approve) mode for TTADK
SELECT_TTADK_MODEL = "select_ttadk_model"  # Select a model for the TTADK tool
REFRESH_TTADK_MODELS = "refresh_ttadk_models"  # Refresh available TTADK model list
SELECT_TTADK_COMBINED = "select_ttadk_combined"  # Select combined tool+model in one action
SELECT_TTADK_COMBINED_TOOL = "select_ttadk_combined_tool"  # Select tool in combined selection flow
SHOW_TTADK_MENU = "show_ttadk_menu"  # Show TTADK tool/model selection menu

# ---------------------------------------------------------------------------
# ACP actions
# ---------------------------------------------------------------------------
SHOW_ACP_MENU = "show_acp_menu"  # Show ACP tool/model selection menu
SELECT_ACP_TOOL = "select_acp_tool"  # Select an ACP-capable tool
SELECT_ACP_MODEL = "select_acp_model"  # Select a model for the ACP tool
REFRESH_ACP_MODELS = "refresh_acp_models"  # Refresh available ACP model list

# ---------------------------------------------------------------------------
# Lock actions
# ---------------------------------------------------------------------------
FORCE_RELEASE_REPO_LOCK = "force_release_repo_lock"  # Force-release repo lock (may interrupt other user)
CONFIRM_LOCK = "confirm_lock"  # Confirm acquiring a contested lock
CANCEL_LOCK = "cancel_lock"  # Cancel lock acquisition request
CONFIRM_FORCE_RELEASE = "confirm_force_release"  # Double-confirm force release (danger action)
CANCEL_FORCE_RELEASE = "cancel_force_release"  # Cancel force release request


# ---------------------------------------------------------------------------
# Action registries
# ---------------------------------------------------------------------------

from src.card.events import CardEvent, CardEventType


def build_common_action_registry() -> dict[str, Callable[[dict], CardEvent]]:
    """Build action registry entries shared across all engine sessions.

    Includes: mode toggle, generic engine stop.
    """
    return {
        MODE_FULL: lambda p: CardEvent.mode_toggled(compact=False),
        MODE_COMPACT: lambda p: CardEvent.mode_toggled(compact=True),
        ENGINE_STOP: lambda p: CardEvent(type=CardEventType.STOPPING),
    }


def build_worktree_action_registry() -> dict[str, Callable[[dict], CardEvent]]:
    """Build the worktree-specific action_id → CardEvent factory registry."""
    return {
        **build_common_action_registry(),
        WORKTREE_FINISH_SELECTION: lambda p: CardEvent(type=CardEventType.WORKTREE_CONFIRM, payload=p),
        WORKTREE_CONFIRM_START: lambda p: CardEvent(type=CardEventType.STARTED, payload=p),
        WORKTREE_MERGE: lambda p: CardEvent(type=CardEventType.WORKTREE_MERGE, payload=p),
        WORKTREE_CLEANUP: lambda p: CardEvent(type=CardEventType.WORKTREE_CLEANUP, payload=p),
        WORKTREE_RETRY_FAILED: lambda p: CardEvent(type=CardEventType.WORKTREE_PROGRESS, payload=p),
        WORKTREE_RETRY_ALL: lambda p: CardEvent(type=CardEventType.WORKTREE_PROGRESS, payload=p),
        SHOW_WORKTREE_MENU: lambda p: CardEvent(type=CardEventType.WORKTREE_TOOL_SELECT, payload=p),
        WORKTREE_CANCEL: lambda p: CardEvent(type=CardEventType.CANCELLED, payload={"reason": "user_cancel"}),
    }
