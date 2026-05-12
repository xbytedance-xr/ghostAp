"""Card event types — unified event abstraction for card state management.

This package re-exports all public symbols for backwards compatibility.
Import pattern: ``from src.card.events import CardEvent, CardEventType``
"""

from src.card.events.acp_adapter import card_event_from_acp
from src.card.events.factories import CardEvent, VALIDATE_PAYLOAD
from src.card.events.payloads import (
    BlockedPayload,
    CompletedPayload,
    CriteriaUpdatedPayload,
    CycleDonePayload,
    CycleStartedPayload,
    FailedPayload,
    PhaseDonePayload,
    PhaseStartedPayload,
    PlanUpdatedPayload,
    ProgressPayload,
    ReasoningBlockPayload,
    ReviewRetryPayload,
    TextBlockPayload,
    ToolDeltaPayload,
    ToolDonePayload,
    ToolFailedPayload,
    ToolModelChangedPayload,
    ToolStartedPayload,
    WarningPayload,
    WorktreeCleanupPayload,
    WorktreeConfirmPayload,
    WorktreeMergePayload,
    WorktreeProgressPayload,
    WorktreeToolSelectPayload,
    WorktreeUnitPayload,
)
from src.card.events.types import CardEventType
from src.card.events.worktree import (
    worktree_cleanup,
    worktree_completed_no_change,
    worktree_confirm,
    worktree_merge,
    worktree_progress,
    worktree_tool_select,
)

__all__ = [
    "CardEvent",
    "CardEventType",
    "card_event_from_acp",
    "VALIDATE_PAYLOAD",
    "worktree_cleanup",
    "worktree_completed_no_change",
    "worktree_confirm",
    "worktree_merge",
    "worktree_progress",
    "worktree_tool_select",
    # Payloads
    "BlockedPayload",
    "CompletedPayload",
    "CriteriaUpdatedPayload",
    "CycleDonePayload",
    "CycleStartedPayload",
    "FailedPayload",
    "PhaseDonePayload",
    "PhaseStartedPayload",
    "PlanUpdatedPayload",
    "ProgressPayload",
    "ReasoningBlockPayload",
    "ReviewRetryPayload",
    "TextBlockPayload",
    "ToolDeltaPayload",
    "ToolDonePayload",
    "ToolFailedPayload",
    "ToolModelChangedPayload",
    "ToolStartedPayload",
    "WarningPayload",
    "WorktreeCleanupPayload",
    "WorktreeConfirmPayload",
    "WorktreeMergePayload",
    "WorktreeProgressPayload",
    "WorktreeToolSelectPayload",
    "WorktreeUnitPayload",
]
