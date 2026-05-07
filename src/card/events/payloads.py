"""Card event payload TypedDict definitions."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict


# ---------------------------------------------------------------------------
# Lifecycle payload TypedDicts
# ---------------------------------------------------------------------------

class CompletedPayload(TypedDict, total=False):
    """Payload for COMPLETED event."""
    summary: str


class FailedPayload(TypedDict):
    """Payload for FAILED event."""
    error: str


class BlockedPayload(TypedDict, total=False):
    """Payload for BLOCKED event."""
    reason: str


# ---------------------------------------------------------------------------
# Content block payload TypedDicts
# ---------------------------------------------------------------------------

class TextBlockPayload(TypedDict):
    """Payload for TEXT_STARTED/TEXT_DELTA/TEXT_DONE events."""
    block_id: str
    text: NotRequired[str]


class ReasoningBlockPayload(TypedDict):
    """Payload for REASONING_STARTED/REASONING_DELTA/REASONING_DONE events."""
    block_id: str
    text: NotRequired[str]


class ToolStartedPayload(TypedDict):
    """Payload for TOOL_STARTED event."""
    block_id: str
    tool_name: str
    tool_input: str


class ToolDeltaPayload(TypedDict):
    """Payload for TOOL_DELTA event."""
    block_id: str
    content: str


class ToolDonePayload(TypedDict):
    """Payload for TOOL_DONE event."""
    block_id: str
    tool_output: str
    tool_summary: str


class ToolFailedPayload(TypedDict):
    """Payload for TOOL_FAILED event."""
    block_id: str
    error: str


class PlanUpdatedPayload(TypedDict):
    """Payload for PLAN_UPDATED event."""
    content: str


# ---------------------------------------------------------------------------
# Meta payload TypedDicts
# ---------------------------------------------------------------------------

class ToolModelChangedPayload(TypedDict, total=False):
    """Payload for TOOL_MODEL_CHANGED event."""
    tool_name: str | None
    model_name: str | None


class ProgressPayload(TypedDict):
    """Payload for PROGRESS_UPDATED event."""
    current: int
    total: int
    label: str


# ---------------------------------------------------------------------------
# Engine lifecycle payload TypedDicts
# ---------------------------------------------------------------------------

class CycleStartedPayload(TypedDict):
    """Payload for CYCLE_STARTED event."""
    cycle_num: int
    max_cycles: int


class CycleDonePayload(TypedDict):
    """Payload for CYCLE_DONE event."""
    cycle_num: int
    status: str


class PhaseStartedPayload(TypedDict):
    """Payload for PHASE_STARTED event."""
    cycle_num: int
    phase: str


class PhaseDonePayload(TypedDict):
    """Payload for PHASE_DONE event."""
    cycle_num: int
    phase: str
    output: str


class ReviewRetryPayload(TypedDict):
    """Payload for REVIEW_RETRY event."""
    cycle_num: int
    attempt: int
    max_attempts: int
    status: str
    delay_sec: float


class CriteriaUpdatedPayload(TypedDict):
    """Payload for CRITERIA_UPDATED event."""
    content: str
    satisfied_count: int
    total_count: int


class WarningPayload(TypedDict):
    """Payload for WARNING_UPDATED event."""
    warning: str


# ---------------------------------------------------------------------------
# Worktree payload TypedDicts
# ---------------------------------------------------------------------------

class WorktreeToolItem(TypedDict, total=False):
    """Single tool item in worktree tool selection."""
    name: str
    display_name: str
    provider: str
    available: bool


class WorktreeMergeNote(TypedDict, total=False):
    """Single merge note describing a branch to merge."""
    branch: str
    worktree_path: str
    status: str
    summary: str
    unit_id: str


class WorktreeSelectedItem(TypedDict, total=False):
    """A selected tool-model combination for worktree execution."""
    tool: str
    model: str
    display_name: str


class WorktreeMergeResult(TypedDict, total=False):
    """Result of a single merge operation."""
    branch: str
    status: str
    message: str


class WorktreeUnitPayload(TypedDict, total=False):
    """Single work unit in worktree progress. Flexible schema — only 'status' is required."""
    status: str  # required (total=False still lets us annotate intent)
    name: str
    unit_id: str
    display_name: str
    summary: str


class WorktreeProgressPayload(TypedDict, total=False):
    """Payload for WORKTREE_PROGRESS event."""
    units: list[WorktreeUnitPayload]
    project_id: str
    message: str
    silent: bool


class WorktreeToolSelectPayload(TypedDict):
    """Payload for WORKTREE_TOOL_SELECT event."""
    tools: list[dict]  # tool dicts vary by source (ACP vs TTADK)
    selected: list[str] | list[dict]
    project_id: str
    message: str


class WorktreeConfirmPayload(TypedDict):
    """Payload for WORKTREE_CONFIRM event."""
    selected_items: list[dict]
    goal: str
    project_id: str
    message: str


class WorktreeCleanupPayload(TypedDict):
    """Payload for WORKTREE_CLEANUP event."""
    merge_notes: list[dict]
    base_branch: str
    merge_results: NotRequired[list[dict] | None]
    project_id: str
    units: NotRequired[list[dict] | None]
    cleanup_phase: Literal["summary", "actions"]


class WorktreeMergePayload(TypedDict):
    """Payload for WORKTREE_MERGE event."""
    merge_notes: list[dict]
    base_branch: str
    project_id: str


class WorktreeCompletedNoChangePayload(TypedDict):
    """Payload for WORKTREE_COMPLETED_NO_CHANGE event."""
    units: list[WorktreeUnitPayload]
    project_id: str
    message: str


# ---------------------------------------------------------------------------
# Task-level card management payload TypedDicts
# ---------------------------------------------------------------------------

class TaskSnapshotPayload(TypedDict):
    """Single task item in task list payload."""
    task_id: str
    name: str
    status: Literal["pending", "in_progress", "completed", "failed"]


class TaskListUpdatedPayload(TypedDict):
    """Payload for TASK_LIST_UPDATED event."""
    tasks: list[TaskSnapshotPayload]
    current_task_id: str
