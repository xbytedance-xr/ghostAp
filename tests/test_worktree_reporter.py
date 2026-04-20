from src.card import CardBuilder
from src.project.context import ProjectContext
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.models import WorktreeUnit
from src.worktree_engine.reporter import WorktreeReporter
from src.worktree_engine.selection import WorktreeToolOption


def test_reporter_builds_unit_summary_and_merge_notes():
    project = ProjectContext(project_id="p1", project_name="P1", root_path="/tmp/p1")
    state = project.worktree_state
    state.base_branch = "main"
    state.units = [
        WorktreeUnit(
            unit_id="u1",
            selection_key="acp:coco:model-a",
            provider="acp",
            tool_name="coco",
            display_name="Coco",
            model_name="model-a",
            branch_name="ghostap/wt/01-acp-coco-model-a",
            worktree_path="/tmp/w1",
            status="completed",
            task_title="分析与方案",
            summary="分析完成",
            has_changes=True,
        ),
        WorktreeUnit(
            unit_id="u2",
            selection_key="ttadk:codex:model-b",
            provider="ttadk",
            tool_name="codex",
            display_name="Codex",
            model_name="model-b",
            branch_name="ghostap/wt/02-ttadk-codex-model-b",
            worktree_path="/tmp/w2",
            status="completed",
            task_title="审查与汇总",
            summary="审查完成",
            has_changes=False,
        ),
    ]

    reporter = WorktreeReporter()
    refreshed = reporter.refresh_state(state)

    assert refreshed.merge_entry_ready is True
    assert len(refreshed.summary_lines) == 2
    assert "Coco" in refreshed.summary_lines[0]
    assert "有代码变更" in refreshed.summary_lines[0]
    assert len(refreshed.merge_notes) == 2
    assert "ghostap/wt/01-acp-coco-model-a" in refreshed.merge_notes[0]
    assert "main" in refreshed.merge_notes[0]


def test_worktree_result_and_merge_entry_cards_expose_integration_entry():
    result_type, result_card = CardBuilder.build_worktree_result_card(
        selected_items=[],
        unit_summary_lines=["- `Coco` · `completed` · 分析与方案 · 有代码变更 · 分析完成"],
        project_id="p1",
        merge_entry_ready=True,
        message="已完成任务执行",
    )
    merge_type, merge_card = CardBuilder.build_worktree_merge_entry_card(
        merge_notes=["- `Coco` → 分支 `ghostap/wt/01` → worktree `/tmp/w1` → 建议合并回 `main`"],
        project_id="p1",
        base_branch="main",
    )

    assert result_type == "interactive"
    assert "show_worktree_merge_entry" in result_card
    assert "工作单元结果" in result_card
    assert merge_type == "interactive"
    assert "待集成项" in merge_card
    assert "ghostap/wt/01" in merge_card


def test_execute_goal_refreshes_summary_lines_and_merge_notes(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    project = ProjectContext(project_id="p2", project_name="P2", root_path=str(project_root))
    manager = WorktreeManager(project_manager=None)

    manager.start_selection(project)
    manager.select_tool(project, WorktreeToolOption(provider="acp", tool_name="coco", display_name="Coco"))
    manager.add_pending_item(project, model_name="model-a")
    manager.finalize_selection(project)
    project.worktree_state.units = [
        WorktreeUnit(
            unit_id="u1",
            selection_key="acp:coco:model-a",
            provider="acp",
            tool_name="coco",
            display_name="Coco",
            model_name="model-a",
            branch_name="ghostap/wt/01-acp-coco-model-a",
            worktree_path=str(project_root / "wt1"),
            status="completed",
            task_title="分析与方案",
            summary="分析完成",
        )
    ]

    original_execute_units = manager._dispatcher.execute_units

    def _fake_execute_units(units, timeout=None, max_workers=None, on_unit_update=None):
        for unit in units:
            unit.status = "completed"
            unit.summary = unit.summary or "执行完成"
        return list(units)

    manager._dispatcher.execute_units = _fake_execute_units
    state = manager.execute_goal(project, "实现 worktree 汇总")
    manager._dispatcher.execute_units = original_execute_units

    assert state.summary_lines
    assert state.merge_notes
    assert state.merge_entry_ready is True


def test_reporter_timeout_unit_shows_timeout_summary():
    """Failed unit with timeout error → summary line includes timeout info."""
    reporter = WorktreeReporter()
    units = [
        WorktreeUnit(
            unit_id="u1",
            selection_key="acp:coco:d",
            provider="acp",
            tool_name="coco",
            display_name="Coco",
            status="failed",
            task_title="分析与方案",
            error="执行超时 (30s)",
        ),
        WorktreeUnit(
            unit_id="u2",
            selection_key="acp:codex:d",
            provider="acp",
            tool_name="codex",
            display_name="Codex",
            status="completed",
            task_title="实现与修改",
            summary="实现完成",
            has_changes=True,
        ),
    ]
    lines = reporter.build_unit_summary_lines(units)

    # Failed timeout unit shows timeout error text
    assert "超时" in lines[0]
    assert "❌" in lines[0]
    # Completed unit shows normally
    assert "✅" in lines[1]
    assert "实现完成" in lines[1]


def test_get_unit_display_name_mapping():
    reporter = WorktreeReporter()

    # 1. Explicit display_name
    u1 = WorktreeUnit(unit_id="wt-01", display_name="My Tool")
    assert reporter._get_unit_display_name(u1) == "My Tool"

    # 2. Standard wt-01 maps to 工作空间 A
    u2 = WorktreeUnit(unit_id="wt-01")
    assert reporter._get_unit_display_name(u2) == "工作空间 A"

    # 3. Standard wt-26 maps to 工作空间 Z
    u3 = WorktreeUnit(unit_id="wt-26")
    assert reporter._get_unit_display_name(u3) == "工作空间 Z"

    # 4. Out of range wt-27 falls back to 单元 27
    u4 = WorktreeUnit(unit_id="wt-27")
    assert reporter._get_unit_display_name(u4) == "单元 27"

    # 5. Unknown ID maps to 自动分配中
    u5 = WorktreeUnit(unit_id="unknown")
    assert reporter._get_unit_display_name(u5) == "自动分配中"

    # 6. Suffix but not wt- prefix fallback
    u6 = WorktreeUnit(unit_id="custom-02")
    assert reporter._get_unit_display_name(u6) == "单元 02"
