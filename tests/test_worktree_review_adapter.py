from unittest.mock import MagicMock

from src.project import ProjectContext
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.models import WorktreeSelectionItem, WorktreeUnit
from src.worktree_engine.review_adapter import WorktreeReviewAdapter


def test_review_adapter_derives_programming_roles_from_goal_and_diff():
    adapter = WorktreeReviewAdapter()

    plan = adapter.plan_roles(
        goal="修复 WT 路由",
        changed_files=["src/feishu/dispatcher.py", "tests/test_feishu_dispatcher.py"],
    )

    role_ids = {role.role_id for role in plan.roles}
    assert {"architect", "tester", "integration", "product"} <= role_ids


def test_review_adapter_downgrades_blocker_without_evidence():
    adapter = WorktreeReviewAdapter()

    outcome = adapter.aggregate([
        {"role_id": "tester", "severity": "blocker", "evidence": "", "message": "bad"}
    ])

    assert outcome.blockers == []
    assert outcome.observations


def test_execute_goal_records_worktree_review_metadata():
    project = ProjectContext("p-review", "Review", "/tmp/review")
    manager = WorktreeManager(project_manager=None)
    state = manager.get_state(project)
    state.units = [WorktreeUnit(unit_id="u1", worktree_path="/tmp/review-wt", has_changes=True)]
    state.selection.selected_items = [
        WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco")
    ]
    manager._dispatcher = MagicMock()
    manager._dispatcher.plan_user_goal.side_effect = lambda goal, units, items: units
    manager._dispatcher.execute_units.side_effect = lambda units, **kwargs: units

    state = manager.execute_goal(project, "修复 auth token 检查")

    role_ids = {role["role_id"] for role in state.review_plan["roles"]}
    assert "security" in role_ids
    assert state.review_outcome == {"blockers": [], "observations": []}
