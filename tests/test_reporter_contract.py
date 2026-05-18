"""Contract tests for reporter output signatures.

These tests act as guardrails ensuring the public output signatures of
ProgressReporter (src/deep_engine/reporter.py), SpecReporter
(src/spec_engine/reporter.py), and WorktreeReporter
(src/worktree_engine/reporter.py) do not change silently during
refactoring.  They assert structural invariants — presence of required
emoji markers, key names, non-emptiness — without binding to specific
message phrasing or locale strings.

Protected regression scenarios:
- ProgressReporter: format_error contains ❌ and code-fence; format_status
  contains 📊 and project name; format_planning_start contains 🧠 and
  requirement; format_planning_done contains ✅ and project name;
  format_project_done emits 🎉/⚠️/⏸️ per status; get_error_title and
  get_status_title are non-empty; get_progress_info returns all eight
  required dict keys.
- SpecReporter: format_analyzing_start contains 📋 and requirement;
  format_analyzing_done contains ✅ when criteria non-empty; format_error
  contains ❌ and code-fence; format_status contains project name;
  format_phase_progress contains all SpecPhase display_names for every
  current_phase value; get_error_title and get_status_title are non-empty;
  get_progress_info returns the four required dict keys.
- WorktreeReporter: build_unit_summary_lines returns one line per unit
  with the unit's display_name; build_merge_notes returns dicts with
  branch/status/summary keys; format_worktree_table([]) returns the
  empty-list sentinel string.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.deep_engine.models import DeepProject, DeepProjectStatus
from src.deep_engine.reporter import ProgressReporter
from src.spec_engine.models import SpecPhase, SpecProject, SpecProjectStatus
from src.spec_engine.reporter import SpecReporter
from src.worktree_engine.models import WorktreeUnit, WorktreeUnitStatus
from src.worktree_engine.reporter import WorktreeReporter

# ---------------------------------------------------------------------------
# Minimal fake-object factories
# ---------------------------------------------------------------------------


def _deep_project(status: DeepProjectStatus = DeepProjectStatus.EXECUTING) -> MagicMock:
    """Minimal DeepProject stand-in."""
    p = MagicMock(spec=DeepProject)
    p.project_id = "dp-test-01"
    p.name = "TestDeepProject"
    p.root_path = "/tmp/testproject"
    p.status = status
    p.duration.return_value = 42
    return p


def _spec_project(status: SpecProjectStatus = SpecProjectStatus.RUNNING) -> MagicMock:
    """Minimal SpecProject stand-in with safe attribute defaults."""
    p = MagicMock(spec=SpecProject)
    p.project_id = "sp-test-01"
    p.name = "TestSpecProject"
    p.requirement = "实现一个用户登录功能"
    p.acceptance_criteria = ["用户可以注册", "用户可以登录", "密码需要加密"]
    p.status = status
    p.total_criteria = 3
    p.satisfied_count = 1
    p.current_cycle_number = 2
    p.current_cycle = None
    p.cycles = []
    p.work_items = []
    p.work_items_total = 0
    p.duration.return_value = None
    # criteria_tracker — SpecReporter.format_status iterates tracker.criteria
    tracker = MagicMock()
    tracker.criteria = []
    tracker.satisfied = {}
    p.criteria_tracker = tracker
    return p


def _worktree_unit(
    unit_id: str = "wt-01",
    display_name: str = "工作空间 A",
    status: WorktreeUnitStatus = WorktreeUnitStatus.COMPLETED,
) -> WorktreeUnit:
    """Real WorktreeUnit dataclass instance (no mock needed)."""
    return WorktreeUnit(
        unit_id=unit_id,
        display_name=display_name,
        status=status,
        has_changes=True,
        task_title="实现登录接口",
        summary="登录接口已实现",
        error="",
        branch_name=f"feature/{unit_id}",
    )


# ===========================================================================
# ProgressReporter contracts
# ===========================================================================


def test_progress_format_error_contains_marker_and_fence():
    result = ProgressReporter().format_error("SomeError: something went wrong")
    assert isinstance(result, str)
    assert "❌" in result
    assert "```" in result


def test_progress_format_error_contains_error_text():
    result = ProgressReporter().format_error("my specific error message")
    assert "my specific error message" in result


def test_progress_format_status_contains_emoji_and_name():
    project = _deep_project(DeepProjectStatus.EXECUTING)
    result = ProgressReporter().format_status(project)
    assert isinstance(result, str)
    assert "📊" in result
    assert project.name in result


def test_progress_format_planning_start_contains_requirement():
    req = "实现一个购物车功能"
    result = ProgressReporter().format_planning_start(req)
    assert isinstance(result, str)
    assert "🧠" in result
    assert req in result


def test_progress_format_planning_done_contains_checkmark_and_name():
    project = _deep_project()
    result = ProgressReporter().format_planning_done(project)
    assert isinstance(result, str)
    assert "✅" in result
    assert project.name in result


def test_progress_format_project_done_completed():
    result = ProgressReporter().format_project_done(_deep_project(DeepProjectStatus.COMPLETED))
    assert isinstance(result, str)
    assert "🎉" in result


def test_progress_format_project_done_failed():
    result = ProgressReporter().format_project_done(_deep_project(DeepProjectStatus.FAILED))
    assert isinstance(result, str)
    assert "⚠️" in result


def test_progress_format_project_done_paused():
    result = ProgressReporter().format_project_done(_deep_project(DeepProjectStatus.PAUSED))
    assert isinstance(result, str)
    assert "⏸️" in result


def test_progress_get_error_title_nonempty():
    title = ProgressReporter().get_error_title()
    assert isinstance(title, str)
    assert len(title) > 0


def test_progress_get_status_title_nonempty():
    title = ProgressReporter().get_status_title()
    assert isinstance(title, str)
    assert len(title) > 0


def test_progress_get_progress_info_required_keys():
    project = _deep_project(DeepProjectStatus.EXECUTING)
    info = ProgressReporter().get_progress_info(project, completed=3, total=10)
    assert isinstance(info, dict)
    required = {
        "progress_bar",
        "completed_count",
        "total_count",
        "status",
        "project_name",
        "project_id",
        "is_executing",
        "is_paused",
    }
    for key in required:
        assert key in info, f"get_progress_info result missing key: {key!r}"


# ===========================================================================
# SpecReporter contracts
# ===========================================================================


def test_spec_format_analyzing_start_contains_emoji_and_requirement():
    req = "构建一个支付模块"
    result = SpecReporter().format_analyzing_start(req)
    assert isinstance(result, str)
    assert "📋" in result
    assert req in result


def test_spec_format_analyzing_done_contains_checkmark_when_criteria_present():
    project = _spec_project()
    result = SpecReporter().format_analyzing_done(project)
    assert isinstance(result, str)
    assert "✅" in result


def test_spec_format_error_contains_marker_and_fence():
    result = SpecReporter().format_error("SpecPhase TASK 失败")
    assert isinstance(result, str)
    assert "❌" in result
    assert "```" in result


def test_spec_format_status_contains_project_name():
    project = _spec_project()
    result = SpecReporter().format_status(project)
    assert isinstance(result, str)
    assert project.name in result


def test_spec_format_phase_progress_contains_all_display_names():
    reporter = SpecReporter()
    for current_phase in SpecPhase:
        result = reporter.format_phase_progress(current_phase, completed=False)
        assert isinstance(result, str)
        for phase in SpecPhase:
            assert phase.display_name in result, (
                f"format_phase_progress(current={current_phase!r}) missing {phase.display_name!r}"
            )


def test_spec_format_phase_progress_completed_contains_all_display_names():
    reporter = SpecReporter()
    result = reporter.format_phase_progress(SpecPhase.BUILD, completed=True)
    assert isinstance(result, str)
    for phase in SpecPhase:
        assert phase.display_name in result


def test_spec_get_error_title_nonempty():
    title = SpecReporter().get_error_title()
    assert isinstance(title, str)
    assert len(title) > 0


def test_spec_get_status_title_nonempty():
    title = SpecReporter().get_status_title()
    assert isinstance(title, str)
    assert len(title) > 0


def test_spec_get_progress_info_required_keys():
    project = _spec_project()
    info = SpecReporter().get_progress_info(project)
    assert isinstance(info, dict)
    required = {"progress_bar", "status", "project_name", "project_id"}
    for key in required:
        assert key in info, f"get_progress_info result missing key: {key!r}"


# ===========================================================================
# WorktreeReporter contracts
# ===========================================================================


def test_worktree_build_unit_summary_lines_nonempty_for_single_unit():
    lines = WorktreeReporter().build_unit_summary_lines([_worktree_unit()])
    assert isinstance(lines, list)
    assert len(lines) == 1


def test_worktree_build_unit_summary_lines_contains_unit_name():
    unit = _worktree_unit(display_name="工作空间 A")
    lines = WorktreeReporter().build_unit_summary_lines([unit])
    assert "工作空间 A" in lines[0]


def test_worktree_build_unit_summary_lines_contains_status():
    unit = _worktree_unit(status=WorktreeUnitStatus.COMPLETED)
    lines = WorktreeReporter().build_unit_summary_lines([unit])
    # Status must appear somewhere in the line (display map or raw value)
    assert len(lines[0]) > 0
    # The raw status string "completed" or its display equivalent must be present
    assert "completed" in lines[0].lower() or "完成" in lines[0]


def test_worktree_build_unit_summary_lines_one_line_per_unit():
    units = [
        _worktree_unit("wt-01", "工作空间 A", WorktreeUnitStatus.COMPLETED),
        _worktree_unit("wt-02", "工作空间 B", WorktreeUnitStatus.FAILED),
    ]
    lines = WorktreeReporter().build_unit_summary_lines(units)
    assert len(lines) == 2
    assert "工作空间 A" in lines[0]
    assert "工作空间 B" in lines[1]


def test_worktree_build_merge_notes_structure():
    unit = _worktree_unit()
    notes = WorktreeReporter().build_merge_notes([unit], base_branch="main")
    assert isinstance(notes, list)
    assert len(notes) > 0
    for note in notes:
        assert isinstance(note, dict)
        assert "branch" in note
        assert "status" in note
        assert "summary" in note


def test_worktree_build_merge_notes_branch_matches_unit():
    unit = _worktree_unit("wt-03")
    notes = WorktreeReporter().build_merge_notes([unit], base_branch="develop")
    assert len(notes) == 1
    assert notes[0]["branch"] == "feature/wt-03"


def test_worktree_format_worktree_table_empty_sentinel():
    result = WorktreeReporter().format_worktree_table([])
    assert isinstance(result, str)
    assert "(无 worktree)" in result
