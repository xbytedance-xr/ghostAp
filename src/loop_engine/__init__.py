"""Loop Engine — ACP-driven iterative closed-loop development.

Uses ACP session's multi-turn prompt capability to iterate until
acceptance criteria are satisfied.
"""

from .engine import LoopEngine, LoopEngineManager, LoopEngineCallbacks
from .models import (
    LoopProject,
    LoopProjectStatus,
    LoopRequirement,
    LoopRole,
    IterationRecord,
    IterationState,
    IterationStatus,
    CriteriaTracker,
    TerminationSignal,
    TerminationResult,
    RoleSelection,
    LoopContextManager,
)
from .tracker import IterationTracker
from .reporter import LoopReporter

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
