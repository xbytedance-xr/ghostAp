from src.project.context import ProjectContext
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.selection import WorktreeToolOption


def test_worktree_selection_flow_supports_tool_model_loop_and_finalize():
    project = ProjectContext(project_id="p1", project_name="P1", root_path="/tmp/p1")
    manager = WorktreeManager(project_manager=None)

    state = manager.start_selection(project)
    assert state.selection.active is True
    assert state.selection.stage == "tool_select"

    manager.select_tool(
        project,
        WorktreeToolOption(
            provider="acp",
            tool_name="coco",
            display_name="Coco",
            description="ACP Coco",
            supports_model=True,
            model_optional=True,
        ),
    )
    state, added, _ = manager.add_pending_item(project, model_name="doubao-seed-1.6", model_display_name="Doubao 1.6")

    assert added is True
    assert len(state.selection.selected_items) == 1
    assert state.selection.selected_items[0].model_name == "doubao-seed-1.6"
    assert state.selection.stage == "review"

    manager.back_to_tool_selection(project)
    manager.select_tool(
        project,
        WorktreeToolOption(
            provider="ttadk",
            tool_name="tmates",
            display_name="TMates",
            supports_model=False,
        ),
    )
    state, added, _ = manager.add_pending_item(project)

    assert added is True
    assert len(state.selection.selected_items) == 2
    assert state.selection.selected_items[1].supports_model is False
    assert "工具内置模型" in state.selection.selected_items[1].display_label

    state = manager.finalize_selection(project)

    assert state.enabled is True
    assert state.selection.active is False
    assert state.selection.stage == "ready"
    assert len(state.summary_lines) == 2


def test_worktree_selection_dedupes_duplicate_tool_model_pairs():
    project = ProjectContext(project_id="p2", project_name="P2", root_path="/tmp/p2")
    manager = WorktreeManager(project_manager=None)
    manager.start_selection(project)

    option = WorktreeToolOption(
        provider="acp",
        tool_name="claude",
        display_name="Claude",
        supports_model=True,
        model_optional=True,
    )

    manager.select_tool(project, option)
    state, added, message = manager.add_pending_item(project, model_name="sonnet", model_display_name="Sonnet")
    assert added is True
    assert "已添加" in message

    manager.back_to_tool_selection(project)
    manager.select_tool(project, option)
    state, added, message = manager.add_pending_item(project, model_name="sonnet", model_display_name="Sonnet")

    assert added is False
    assert "已忽略重复选择" in message
    assert len(state.selection.selected_items) == 1
