"""Unit tests for WorktreeSelectionController (extracted from WorktreeManager)."""
from src.project.context import ProjectContext
from src.worktree_engine.models import WorktreeSelectionStage, WorktreeUnit
from src.worktree_engine.selection import WorktreeToolOption
from src.worktree_engine.selection_controller import WorktreeSelectionController


def _make_project():
    return ProjectContext(project_id="p1", project_name="Test", root_path="/tmp/test")


def test_start_selection_sets_stage_and_goal():
    """start_selection should activate selection with TOOL_SELECT stage and store goal."""
    ctrl = WorktreeSelectionController()
    project = _make_project()

    state = ctrl.start_selection(project, goal="实现登录功能")

    assert state.selection.active is True
    assert state.selection.stage == WorktreeSelectionStage.TOOL_SELECT
    assert state.selection.pending_goal == "实现登录功能"


def test_select_tool_advances_stage_to_model_select():
    """Selecting a tool that supports_model should advance to MODEL_SELECT."""
    ctrl = WorktreeSelectionController()
    project = _make_project()

    ctrl.start_selection(project)
    state = ctrl.select_tool(
        project,
        WorktreeToolOption(
            provider="acp", tool_name="coco", display_name="Coco",
            description="test", supports_model=True,
        ),
    )

    assert state.selection.stage == WorktreeSelectionStage.MODEL_SELECT
    assert state.selection.pending_item is not None


def test_select_tool_skips_model_when_not_supported():
    """Selecting a tool without model support should go directly to REVIEW."""
    ctrl = WorktreeSelectionController()
    project = _make_project()

    ctrl.start_selection(project)
    state = ctrl.select_tool(
        project,
        WorktreeToolOption(
            provider="cli", tool_name="claude", display_name="Claude",
            description="test", supports_model=False,
        ),
    )

    assert state.selection.stage == WorktreeSelectionStage.REVIEW


def test_finalize_selection_marks_enabled_and_ready():
    """finalize_selection with items should set enabled=True and stage=READY."""
    ctrl = WorktreeSelectionController()
    project = _make_project()

    ctrl.start_selection(project)
    ctrl.select_tool(
        project,
        WorktreeToolOption(
            provider="acp", tool_name="coco", display_name="Coco",
            description="test", supports_model=False,
        ),
    )
    ctrl.add_pending_item(project)
    state = ctrl.finalize_selection(project)

    assert state.enabled is True
    assert state.selection.stage == WorktreeSelectionStage.READY
    assert len(state.selection.selected_items) == 1


def test_set_pending_goal_encapsulates_mutation():
    """set_pending_goal should update state without handler needing direct access."""
    ctrl = WorktreeSelectionController()
    project = _make_project()

    ctrl.start_selection(project)
    ctrl.set_pending_goal(project, "  新目标  ")

    state = ctrl._get_state(project)
    assert state.selection.pending_goal == "新目标"


def test_mark_units_ready_sets_all_units():
    """mark_units_ready should set all unit statuses to 'ready'."""
    ctrl = WorktreeSelectionController()
    project = _make_project()

    state = ctrl._get_state(project)
    state.units = [
        WorktreeUnit(unit_id="u1", status="pending"),
        WorktreeUnit(unit_id="u2", status="planned"),
    ]

    ctrl.mark_units_ready(project)

    assert all(u.status == "ready" for u in state.units)
