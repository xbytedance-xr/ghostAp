"""Loop Engine — ACP-driven iterative closed-loop development.

Uses ACP session's multi-turn prompt capability to iterate until
acceptance criteria are satisfied.
"""

from .engine import LoopEngine, LoopEngineCallbacks, LoopEngineManager
from .models import (
    CriteriaTracker,
    IterationRecord,
    IterationState,
    IterationStatus,
    LoopContextManager,
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
    LoopRole,
    PerspectiveReview,
    ReviewPerspective,
    ReviewResult,
    RoleSelection,
    TerminationResult,
    TerminationSignal,
)
from .reporter import LoopReporter
from .tracker import IterationTracker

__all__ = [
    # Engine
    "LoopEngine",
    "LoopEngineManager",
    "LoopEngineCallbacks",
    # Models
    "LoopProject",
    "LoopProjectStatus",
    "LoopRequirement",
    "LoopRole",
    "ReviewPerspective",
    "PerspectiveReview",
    "ReviewResult",
    "IterationRecord",
    "IterationState",
    "IterationStatus",
    "CriteriaTracker",
    "TerminationSignal",
    "TerminationResult",
    "RoleSelection",
    "LoopContextManager",
    # Tracker
    "IterationTracker",
    # Reporter
    "LoopReporter",
]
