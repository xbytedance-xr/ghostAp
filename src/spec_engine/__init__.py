"""Spec Engine — ACP-driven structured development with iterative review.

Follows spec-kit methodology: spec → plan → task → build → review,
with review-driven iteration cycles.
"""

from .engine import SpecEngine, SpecEngineManager, SpecEngineCallbacks
from .models import (
    SpecProject,
    SpecProjectStatus,
    SpecPhase,
    SpecCycle,
    SpecTask,
    SpecTaskStatus,
)
from .tracker import PhaseTracker
from .reporter import SpecReporter

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
]
