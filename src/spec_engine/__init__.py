"""Spec Engine — ACP-driven structured development with iterative review.

Follows spec-kit methodology: spec → plan → task → build → review,
with review-driven iteration cycles.
"""

from .engine import SpecEngine, SpecEngineCallbacks
from .manager import SpecEngineManager
from .models import (
    SpecCycle,
    SpecPhase,
    SpecProject,
    SpecProjectStatus,
    SpecTask,
    SpecTaskStatus,
)
from .reporter import SpecReporter
from .task_persistence import (
    SPEC_TASKS_DIR,
    SpecTaskState,
    delete_task_state,
    generate_task_id,
    list_pending_tasks,
    load_task_state,
    save_task_state,
)
from .tracker import PhaseTracker

__all__ = [
    # Engine
    "SpecEngine",
    "SpecEngineManager",
    "SpecEngineCallbacks",
    # Models
    "SpecProject",
    "SpecProjectStatus",
    "SpecPhase",
    "SpecCycle",
    "SpecTask",
    "SpecTaskStatus",
    # Tracker
    "PhaseTracker",
    # Reporter
    "SpecReporter",
    # Task Persistence
    "SPEC_TASKS_DIR",
    "SpecTaskState",
    "generate_task_id",
    "save_task_state",
    "load_task_state",
    "delete_task_state",
    "list_pending_tasks",
]
