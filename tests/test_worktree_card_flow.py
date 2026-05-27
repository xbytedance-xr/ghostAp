"""Tests for the worktree selection-loop card flow (state transitions).

These tests exercise WorktreeManager's selection state machine directly,
without involving Feishu API or card rendering.
"""

from unittest.mock import MagicMock

from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.selection import WorktreeToolOption, format_selection_lines

from src.project.context import ProjectContext
from src.worktree_engine.models import WorktreeSelectionStage, WorktreeUnit
from src.worktree_engine.selection_controller import WorktreeSelectionController


def _make_project():
    p = MagicMock()
    p.project_id = "proj-test"
    p.project_name = "TestProject"
    p.root_path = "/tmp/test"
    return p


def _make_manager():
    pm = MagicMock()
    return WorktreeManager(pm)


TOOL_ACP = WorktreeToolOption(provider="acp", tool_name="coco", display_name="Coco", supports_model=False)
TOOL_TTADK = WorktreeToolOption(provider="ttadk", tool_name="codex", display_name="Codex", supports_model=True)
TOOL_CLI = WorktreeToolOption(provider="cli", tool_name="claude", display_name="Claude", supports_model=False)


def test_selection_items_expose_agent_tool_model_tuple_with_default_model():
    """Selected tools should expose the programming tuple: agent + tool + model."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    mgr.select_tool(
        project,
        WorktreeToolOption(provider="acp", tool_name="coco", display_name="Coco", supports_model=True),
    )
    mgr.add_pending_item(project, model_name="doubao-pro", model_display_name="Doubao Pro")

    mgr.back_to_tool_selection(project)
    mgr.select_tool(
        project,
        WorktreeToolOption(provider="cli", tool_name="claude", display_name="Claude", supports_model=False),
    )
    mgr.add_pending_item(project)

    mgr.back_to_tool_selection(project)
    mgr.select_tool(
        project,
        WorktreeToolOption(provider="ttadk", tool_name="codex", display_name="Codex", supports_model=True),
    )
    state, added, _ = mgr.add_pending_item(
        project, model_name="gpt-5.2", model_display_name="GPT-5.2",
    )

    assert added is True
    exported = [item.to_dict() for item in state.selection.selected_items]
    assert [
        (item["agent_name"], item["tool_name"], item["effective_model_display_name"], item["display_label"])
        for item in exported
    ] == [
        ("", "coco", "Doubao Pro", "Coco / Doubao Pro"),
        ("", "claude", "默认模型", "Claude / 默认模型"),
        ("ttadk", "codex", "GPT-5.2", "TTADK · Codex / GPT-5.2"),
    ]
    assert [item["selection_key"] for item in exported] == [
        "acp:coco:doubao-pro",
        "cli:claude:default",
        "ttadk:codex:gpt-5.2",
    ]
    assert exported[1]["model_name"] is None
    assert exported[1]["effective_model_name"] == "default"


def test_skip_model_for_non_ttadk():
    """supports_model=False skips model_select → goes directly to review."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    state = mgr.select_tool(project, TOOL_ACP)
    assert state.selection.stage == "review"
    assert state.selection.pending_item is not None


def test_model_select_for_ttadk():
    """supports_model=True goes to model_select."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    state = mgr.select_tool(project, TOOL_TTADK)
    assert state.selection.stage == "model_select"


def test_selection_lines_update():
    """Each add_pending_item updates the numbered list correctly."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    mgr.select_tool(project, TOOL_ACP)
    mgr.add_pending_item(project)
    state = mgr.get_state(project)
    lines = format_selection_lines(state.selection.selected_items)
    assert len(lines) == 1
    assert "1." in lines[0]

    mgr.back_to_tool_selection(project)
    mgr.select_tool(project, TOOL_CLI)
    mgr.add_pending_item(project)
    state = mgr.get_state(project)
    lines = format_selection_lines(state.selection.selected_items)
    assert len(lines) == 2
    assert "2." in lines[1]


def test_duplicate_selection_ignored():
    """Duplicate tool-model pair should be ignored."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    mgr.select_tool(project, TOOL_ACP)
    mgr.add_pending_item(project)

    mgr.back_to_tool_selection(project)
    mgr.select_tool(project, TOOL_ACP)
    _, added, _ = mgr.add_pending_item(project)
    assert added is False

    state = mgr.get_state(project)
    assert len(state.selection.selected_items) == 1


def test_finalize_empty_selection_fails():
    """Finalize without any selection does not enable worktree mode."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)

    state = mgr.finalize_selection(project)
    assert state.enabled is False
    assert state.selection.stage == "tool_select"  # stays at tool_select


def test_reset_selection_clears_all():
    """reset_selection should clear all previous selections."""
    mgr = _make_manager()
    project = _make_project()
    mgr.start_selection(project)
    mgr.select_tool(project, TOOL_ACP)
    mgr.add_pending_item(project)
    assert len(mgr.get_state(project).selection.selected_items) == 1

    state = mgr.reset_selection(project)
    assert len(state.selection.selected_items) == 0
    assert state.selection.stage == "tool_select"


# ---------------------------------------------------------------------------
# Tests merged from test_worktree_selection_controller.py
# ---------------------------------------------------------------------------


def _make_project_ctx():
    return ProjectContext(project_id="p1", project_name="Test", root_path="/tmp/test")


def test_start_selection_sets_stage_and_goal():
    """start_selection should activate selection with TOOL_SELECT stage and store goal."""
    ctrl = WorktreeSelectionController()
    project = _make_project_ctx()

    state = ctrl.start_selection(project, goal="实现登录功能")

    assert state.selection.active is True
    assert state.selection.stage == WorktreeSelectionStage.TOOL_SELECT
    assert state.selection.pending_goal == "实现登录功能"


def test_select_tool_advances_stage_to_model_select():
    """Selecting a tool that supports_model should advance to MODEL_SELECT."""
    ctrl = WorktreeSelectionController()
    project = _make_project_ctx()

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
    project = _make_project_ctx()

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
    project = _make_project_ctx()

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
    project = _make_project_ctx()

    ctrl.start_selection(project)
    ctrl.set_pending_goal(project, "  新目标  ")

    state = ctrl._get_state(project)
    assert state.selection.pending_goal == "新目标"


def test_mark_units_ready_sets_all_units():
    """mark_units_ready should set all unit statuses to 'ready'."""
    ctrl = WorktreeSelectionController()
    project = _make_project_ctx()

    state = ctrl._get_state(project)
    state.units = [
        WorktreeUnit(unit_id="u1", status="pending"),
        WorktreeUnit(unit_id="u2", status="planned"),
    ]

    ctrl.mark_units_ready(project)

    assert all(u.status == "ready" for u in state.units)
