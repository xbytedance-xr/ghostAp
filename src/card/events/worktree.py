"""Worktree-specific CardEvent factory functions.

Extracted from CardEvent classmethods to maintain SRP — the core CardEvent
class stays focused on common event factories, while worktree-specific
construction logic lives here.
"""

from __future__ import annotations

from .factories import CardEvent, VALIDATE_PAYLOAD
from .payloads import (
    WorktreeCleanupPayload,
    WorktreeCompletedNoChangePayload,
    WorktreeConfirmPayload,
    WorktreeMergePayload,
    WorktreeProgressPayload,
    WorktreeToolSelectPayload,
)
from .types import CardEventType


def worktree_progress(
    units: list[dict], project_id: str = "", message: str = "", silent: bool = False
) -> CardEvent:
    """Worktree execution progress update.

    Payload:
        units: list of {name, status, summary?} dicts for each work unit.
        project_id: associated project identifier.
        message: optional status message.
        silent: whether in silent throttle mode.
    Triggered when: worktree units change status during execution.
    """
    if not isinstance(units, list):
        raise TypeError(f"units must be a list, got {type(units).__name__}")
    if VALIDATE_PAYLOAD:
        for u in units:
            if not (isinstance(u, dict) and "status" in u):
                raise ValueError(f"each unit must be a dict with 'status', got {u!r}")
    payload: WorktreeProgressPayload = {
        "units": units, "project_id": project_id, "message": message, "silent": silent,
    }
    return CardEvent(type=CardEventType.WORKTREE_PROGRESS, payload=payload)


def worktree_tool_select(
    tools: list[dict], selected: list[str] | None = None,
    project_id: str = "", message: str = "",
    select_action: str = "worktree_select_tool",
) -> CardEvent:
    """Worktree tool selection card state.

    Payload:
        tools: list of {id, name, description} available tools.
        selected: list of currently selected tool IDs.
        project_id: associated project identifier.
        message: optional prompt message.
        select_action: action emitted by option buttons.
    Triggered when: user enters worktree flow or toggles tool selection.
    """
    if not isinstance(tools, list):
        raise TypeError(f"tools must be a list, got {type(tools).__name__}")
    if VALIDATE_PAYLOAD:
        for t in tools:
            if not isinstance(t, dict):
                raise TypeError(f"each tool must be a dict, got {type(t).__name__}")
    payload: WorktreeToolSelectPayload = {
        "tools": tools, "selected": selected or [],
        "project_id": project_id, "message": message,
        "select_action": select_action,
    }
    return CardEvent(type=CardEventType.WORKTREE_TOOL_SELECT, payload=payload)


def worktree_confirm(
    selected_items: list[dict], goal: str = "",
    project_id: str = "", message: str = "",
) -> CardEvent:
    """Worktree execution confirmation card state.

    Payload:
        selected_items: list of {tool, model} selections to confirm.
        goal: user-provided execution goal text.
        project_id: associated project identifier.
        message: optional prompt message.
    Triggered when: tool selection is finalized, awaiting user confirmation.
    """
    if not isinstance(selected_items, list):
        raise TypeError(f"selected_items must be a list, got {type(selected_items).__name__}")
    payload: WorktreeConfirmPayload = {
        "selected_items": selected_items, "goal": goal,
        "project_id": project_id, "message": message,
    }
    return CardEvent(type=CardEventType.WORKTREE_CONFIRM, payload=payload)


def worktree_cleanup(
    merge_notes: list[dict], base_branch: str = "main",
    merge_results: list[dict] | None = None,
    project_id: str = "", units: list[dict] | None = None,
    cleanup_phase: str = "summary",
) -> CardEvent:
    """Worktree cleanup/merge action card state.

    Payload:
        merge_notes: list of {branch, status, summary} for merge candidates.
        base_branch: target branch for merge.
        merge_results: results from merge attempts (if any).
        project_id: associated project identifier.
        units: work unit data for retry context.
        cleanup_phase: "summary" (default, shows merge only) or "actions" (full controls).
    Triggered when: execution completes and merge/cleanup options are shown.
    """
    if not isinstance(merge_notes, list):
        raise TypeError(f"merge_notes must be a list, got {type(merge_notes).__name__}")
    if cleanup_phase not in ("summary", "actions"):
        raise ValueError(f"cleanup_phase must be 'summary' or 'actions', got {cleanup_phase!r}")
    if VALIDATE_PAYLOAD:
        for mn in merge_notes:
            if "branch" not in mn or "status" not in mn:
                raise ValueError(f"each merge_note must have 'branch' and 'status', got {mn!r}")
    payload: WorktreeCleanupPayload = {
        "merge_notes": merge_notes, "base_branch": base_branch,
        "merge_results": merge_results, "project_id": project_id,
        "units": units, "cleanup_phase": cleanup_phase,
    }
    return CardEvent(type=CardEventType.WORKTREE_CLEANUP, payload=payload)


def worktree_merge(
    merge_notes: list[dict], base_branch: str = "main",
    project_id: str = "",
) -> CardEvent:
    """Worktree merge entry card state.

    Payload:
        merge_notes: list of {branch, status, summary} for pending merges.
        base_branch: target branch for merge.
        project_id: associated project identifier.
    Triggered when: merge-ready items are available for user action.
    """
    if not isinstance(merge_notes, list):
        raise TypeError(f"merge_notes must be a list, got {type(merge_notes).__name__}")
    if VALIDATE_PAYLOAD:
        for mn in merge_notes:
            if "branch" not in mn or "status" not in mn:
                raise ValueError(f"each merge_note must have 'branch' and 'status', got {mn!r}")
    payload: WorktreeMergePayload = {
        "merge_notes": merge_notes, "base_branch": base_branch,
        "project_id": project_id,
    }
    return CardEvent(type=CardEventType.WORKTREE_MERGE, payload=payload)


def worktree_completed_no_change(
    units: list[dict], project_id: str = "", message: str = ""
) -> CardEvent:
    """Worktree execution completed but no file changes detected.

    Payload:
        units: list of {name, status, summary?} dicts for each work unit.
        project_id: associated project identifier.
        message: explanatory message for the user.
    Triggered when: all units finish but none produced mergeable changes.
    """
    if not isinstance(units, list):
        raise TypeError(f"units must be a list, got {type(units).__name__}")
    payload: WorktreeCompletedNoChangePayload = {
        "units": units, "project_id": project_id, "message": message,
    }
    return CardEvent(type=CardEventType.WORKTREE_COMPLETED_NO_CHANGE, payload=payload)
