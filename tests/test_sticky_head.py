"""sticky_head builder tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.card.render.sticky_head import STICKY_HEAD_MAX_NODES, build_sticky_head
from src.card.state.models import CardMetadata, CardState
from src.card.state.runtime_stats import RuntimeStats


def _state_with(*, has_task_list: bool, has_activity: bool, runtime: RuntimeStats) -> MagicMock:
    state = MagicMock(spec=CardState)
    state.metadata = CardMetadata(mode_name="Deep", mode_emoji="🧠", engine_type="deep")
    state.runtime_stats = runtime
    state.blocks = ()
    state.task_list = MagicMock()
    state.task_list.tasks = ({"task_id": "t1", "name": "x", "status": "in_progress"},) if has_task_list else ()
    state.task_list.current_task_id = "t1"
    state.task_list.block_id = "tl"
    state.activity = MagicMock()
    state.activity.has_data = has_activity
    return state


def test_sticky_head_minimum_phase_banner_only():
    runtime = RuntimeStats(elapsed_seconds=10.0, deep_phase="executing")
    state = _state_with(has_task_list=False, has_activity=False, runtime=runtime)
    sticky = build_sticky_head(state, state.metadata)
    assert len(sticky) == 1
    assert sticky[0].kind == "phase_banner"
    assert "Deep" in sticky[0].content


def test_sticky_head_includes_task_list_when_present():
    runtime = RuntimeStats(elapsed_seconds=5.0, deep_phase="executing")
    state = _state_with(has_task_list=True, has_activity=False, runtime=runtime)
    sticky = build_sticky_head(state, state.metadata)
    assert [a.kind for a in sticky] == ["phase_banner", "task_list"]


def test_sticky_head_omits_activity_summary_when_present():
    runtime = RuntimeStats(elapsed_seconds=5.0, deep_phase="executing")
    state = _state_with(has_task_list=True, has_activity=True, runtime=runtime)
    sticky = build_sticky_head(state, state.metadata)
    assert [a.kind for a in sticky] == ["phase_banner", "task_list"]


def test_sticky_head_node_cap_keeps_required_atoms_only():
    runtime = RuntimeStats(elapsed_seconds=5.0, deep_phase="executing")
    state = _state_with(has_task_list=True, has_activity=True, runtime=runtime)
    sticky = build_sticky_head(state, state.metadata, _force_total_nodes=STICKY_HEAD_MAX_NODES + 5)
    assert "phase_banner" in [a.kind for a in sticky]
    assert "activity_summary" not in [a.kind for a in sticky]
    assert sum(a.node_count for a in sticky) <= STICKY_HEAD_MAX_NODES
