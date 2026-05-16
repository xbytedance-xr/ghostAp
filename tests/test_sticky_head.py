"""sticky_head builder tests."""
from __future__ import annotations

from unittest.mock import MagicMock

from src.card.events import CardEvent, CardEventType
from src.card.render.sticky_head import STICKY_HEAD_MAX_NODES, build_sticky_head
from src.card.state.models import CardMetadata, CardState
from src.card.state.reducer import reduce_card_state
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


def test_sticky_head_omits_plain_programming_phase_banner():
    state = MagicMock(spec=CardState)
    state.metadata = CardMetadata(mode_name="Coco", mode_emoji="🤖", engine_type=None)
    state.runtime_stats = RuntimeStats(elapsed_seconds=10.0)
    state.blocks = ()
    state.task_list = None

    sticky = build_sticky_head(state, state.metadata)

    assert sticky == ()


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


def test_sticky_head_uses_reducer_runtime_stats_for_spec_banner():
    metadata = CardMetadata(
        mode_name="Spec",
        mode_emoji="📋",
        engine_type="spec",
        session_started_at=100.0,
    )
    state = reduce_card_state(
        None,
        CardEvent(type=CardEventType.STARTED, payload={"_now": 100.0}),
        metadata=metadata,
    )
    state = reduce_card_state(
        state,
        CardEvent(type=CardEventType.CYCLE_STARTED, payload={"cycle_num": 2, "max_cycles": 500, "_now": 120.0}),
    )
    state = reduce_card_state(
        state,
        CardEvent(type=CardEventType.PHASE_STARTED, payload={"cycle_num": 2, "phase": "review", "_now": 188.0}),
    )

    sticky = build_sticky_head(state, state.metadata)

    assert sticky[0].content == "📋 Spec · cycle 2/review · 1m28s"
    assert "?/—" not in sticky[0].content
