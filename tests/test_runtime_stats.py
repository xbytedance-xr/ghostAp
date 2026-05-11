"""RuntimeStats dataclass tests."""
from __future__ import annotations

import dataclasses

from src.card.state.runtime_stats import RuntimeStats


def test_runtime_stats_defaults():
    rs = RuntimeStats()
    assert rs.elapsed_seconds == 0.0
    assert rs.deep_phase is None
    assert rs.spec_cycle is None
    assert rs.spec_perspective is None
    assert rs.worktree_subagent is None


def test_runtime_stats_construction():
    rs = RuntimeStats(
        elapsed_seconds=83.5,
        deep_phase="executing",
        spec_cycle=1,
        spec_perspective="code",
        worktree_subagent="aiden",
    )
    assert rs.elapsed_seconds == 83.5
    assert rs.deep_phase == "executing"
    assert rs.spec_cycle == 1
    assert rs.spec_perspective == "code"
    assert rs.worktree_subagent == "aiden"


def test_runtime_stats_is_frozen():
    rs = RuntimeStats()
    raised = False
    try:
        rs.elapsed_seconds = 999.0  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised
