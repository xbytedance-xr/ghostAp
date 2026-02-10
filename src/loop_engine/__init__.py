"""Loop Engine — subprocess-driven iterative closed-loop development.

Uses session.send_prompt_streaming() to iterate until acceptance criteria
are satisfied.
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
    # Reporter
    "LoopReporter",
]
