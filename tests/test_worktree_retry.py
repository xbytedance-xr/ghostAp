"""Tests for worktree retry-failed-units functionality."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from src.card.builders.worktree import WorktreeBuilder
from src.project.context import ProjectContext
from src.worktree_engine.dispatcher import WorktreeDispatcher
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.models import WorktreeSelectionItem, WorktreeUnit


# ---------------------------------------------------------------------------
# Fake session helpers
# ---------------------------------------------------------------------------


@dataclass
class FakePromptResult:
    stop_reason: str
    text: str


class FakeSession:
    def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
        self.provider = provider
        self.tool_name = tool_name
        self.working_dir = working_dir
        self.model_name = model_name

    def start(self, startup_timeout=60):
        return "session"

    def send_prompt(self, text, on_event=None, timeout=None):
        Path(self.working_dir, f"{self.tool_name}.txt").write_text(text, encoding="utf-8")
        return FakePromptResult(stop_reason="end_turn", text=f"done:{self.tool_name}")

    def close(self):
        return None


class FailingSession:
    """A session that always raises on send_prompt."""

    def __init__(self, *, provider, tool_name, working_dir, model_name=None, ttadk_use_pty=False):
        self.provider = provider
        self.tool_name = tool_name
        self.working_dir = working_dir

    def start(self, startup_timeout=60):
        return "session"

    def send_prompt(self, text, on_event=None, timeout=None):
        raise RuntimeError("simulated failure")

    def close(self):
        return None


# ---------------------------------------------------------------------------
# Manager retry tests
# ---------------------------------------------------------------------------


def _make_project_with_units(tmp_path, statuses: list[str]) -> tuple[ProjectContext, WorktreeManager]:
    """Helper: create project + manager with units in given statuses."""
    dirs = [tmp_path / f"wt{i}" for i in range(len(statuses))]
    for d in dirs:
        d.mkdir(exist_ok=True)

    project = ProjectContext(project_id="p1", project_name="P1", root_path=str(tmp_path))

    units = []
    for i, status in enumerate(statuses):
        unit = WorktreeUnit(
            unit_id=f"u{i}",
            selection_key=f"acp:coco:default",
            provider="acp",
            tool_name="coco",
            display_name="Coco",
            worktree_path=str(dirs[i]),
            status=status,
            task_title=f"Task {i}",
            task_prompt=f"Prompt {i}",
        )
        if status == "failed":
            unit.error = "simulated error"
            unit.summary = "simulated error"
        elif status == "completed":
            unit.summary = "completed successfully"
            unit.has_changes = True
        units.append(unit)

    state = project.worktree_state
    state.units = units
    state.enabled = True
    state.last_user_goal = "实现功能"
    state.selection.selected_items = [
        WorktreeSelectionItem(provider="acp", tool_name="coco", display_name="Coco"),
    ]

    manager = WorktreeManager(project_manager=None)
    manager._dispatcher = WorktreeDispatcher(session_factory=lambda **kw: FakeSession(**kw))

    return project, manager


def test_retry_resets_only_failed_units(tmp_path):
    """Completed units stay unchanged; only failed units get re-executed."""
    project, manager = _make_project_with_units(tmp_path, ["completed", "failed", "failed"])

    state = manager.retry_failed_units(project)

    # First unit (completed) should remain completed with original summary
    assert state.units[0].status == "completed"
    assert state.units[0].summary == "completed successfully"
    assert state.units[0].has_changes is True

    # Previously failed units should now be completed (FakeSession succeeds)
    assert state.units[1].status == "completed"
    assert state.units[2].status == "completed"
    assert state.units[1].error == ""
    assert state.units[2].error == ""


def test_retry_preserves_last_user_goal(tmp_path):
    """Retry uses the last_user_goal from state without requiring new input."""
    project, manager = _make_project_with_units(tmp_path, ["failed"])

    state = manager.get_state(project)
    assert state.last_user_goal == "实现功能"

    state = manager.retry_failed_units(project)

    # Goal should still be preserved
    assert state.last_user_goal == "实现功能"
    # The retried unit should be completed now
    assert state.units[0].status == "completed"


def test_retry_returns_error_when_no_goal(tmp_path):
    """If last_user_goal is empty, retry returns an error."""
    project, manager = _make_project_with_units(tmp_path, ["failed"])
    project.worktree_state.last_user_goal = ""

    state = manager.retry_failed_units(project)

    assert "目标为空" in state.last_error


def test_retry_returns_error_when_no_failed_units(tmp_path):
    """If no units are failed, retry returns an error."""
    project, manager = _make_project_with_units(tmp_path, ["completed", "completed"])

    state = manager.retry_failed_units(project)

    assert "没有失败" in state.last_error


def test_retry_returns_error_when_running_units_exist(tmp_path):
    """If there are running units, retry is blocked."""
    project, manager = _make_project_with_units(tmp_path, ["running", "failed"])

    state = manager.retry_failed_units(project)

    assert "正在执行" in state.last_error


# ---------------------------------------------------------------------------
# Card rendering tests
# ---------------------------------------------------------------------------


def test_progress_card_shows_retry_button_when_failed():
    """Card should contain retry button when there are failed units and no running units."""
    units = [
        {"status": "completed", "display_name": "Coco", "tool_name": "coco", "task_title": "Task 1"},
        {"status": "failed", "display_name": "Claude", "tool_name": "claude", "task_title": "Task 2", "error": "timeout"},
    ]

    _, card_json = WorktreeBuilder.build_worktree_progress_card(units, project_id="p1")
    card = json.loads(card_json)

    # Find the retry button in the card elements
    found_retry = False
    for element in card.get("body", {}).get("elements", []):
        if element.get("tag") == "action":
            for action in element.get("actions", []):
                val = action.get("value", {})
                if val.get("action") == "worktree_retry_failed":
                    found_retry = True
                    assert val.get("project_id") == "p1"
                    assert "重试" in action.get("text", {}).get("content", "")
                    break
    assert found_retry, "Retry button should be present when failed units exist"


def test_progress_card_no_retry_button_when_all_success():
    """Card should NOT contain retry button when all units succeeded."""
    units = [
        {"status": "completed", "display_name": "Coco", "tool_name": "coco", "task_title": "Task 1"},
        {"status": "completed", "display_name": "Claude", "tool_name": "claude", "task_title": "Task 2"},
    ]

    _, card_json = WorktreeBuilder.build_worktree_progress_card(units, project_id="p1")
    card = json.loads(card_json)

    for element in card.get("body", {}).get("elements", []):
        if element.get("tag") == "action":
            for action in element.get("actions", []):
                val = action.get("value", {})
                assert val.get("action") != "worktree_retry_failed", \
                    "Retry button should NOT be present when all units succeeded"


def test_progress_card_no_retry_button_when_running():
    """Card should NOT show retry button while units are still running."""
    units = [
        {"status": "running", "display_name": "Coco", "tool_name": "coco", "task_title": "Task 1"},
        {"status": "failed", "display_name": "Claude", "tool_name": "claude", "task_title": "Task 2", "error": "err"},
    ]

    _, card_json = WorktreeBuilder.build_worktree_progress_card(units, project_id="p1")
    card = json.loads(card_json)

    for element in card.get("body", {}).get("elements", []):
        if element.get("tag") == "action":
            for action in element.get("actions", []):
                val = action.get("value", {})
                assert val.get("action") != "worktree_retry_failed", \
                    "Retry button should NOT be present while units are running"


# ---------------------------------------------------------------------------
# Cleanup card — parallel retry + merge button tests
# ---------------------------------------------------------------------------


def _extract_actions(card_json: str) -> list[dict]:
    """Helper: extract all button value dicts from a card JSON string."""
    card = json.loads(card_json)
    results = []
    for element in card.get("body", {}).get("elements", []):
        if element.get("tag") == "action":
            for action in element.get("actions", []):
                results.append(action)
    return results


def test_cleanup_card_shows_retry_when_partial_failed():
    """Cleanup card should show both merge and retry buttons when partial failed."""
    units = [
        {"status": "completed", "display_name": "Coco", "tool_name": "coco", "task_title": "T1"},
        {"status": "failed", "display_name": "Claude", "tool_name": "claude", "task_title": "T2", "error": "err"},
    ]
    _, card_json = WorktreeBuilder.build_worktree_cleanup_card(
        merge_notes=["- note"], project_id="p1", units=units,
    )
    buttons = _extract_actions(card_json)
    action_ids = [b.get("value", {}).get("action") for b in buttons]
    assert "worktree_merge" in action_ids, "Merge button should be present"
    assert "worktree_retry_failed" in action_ids, "Retry button should be present alongside merge"
    assert "worktree_cleanup" in action_ids, "Cleanup button should still be present"


def test_cleanup_card_merge_text_changes_when_partial_failed():
    """Merge button text should change to '✅ 先合并已完成' when failed units exist."""
    units = [
        {"status": "completed", "display_name": "Coco", "tool_name": "coco", "task_title": "T1"},
        {"status": "failed", "display_name": "Claude", "tool_name": "claude", "task_title": "T2", "error": "err"},
    ]
    _, card_json = WorktreeBuilder.build_worktree_cleanup_card(
        merge_notes=["- note"], project_id="p1", units=units,
    )
    buttons = _extract_actions(card_json)
    merge_btn = next(b for b in buttons if b.get("value", {}).get("action") == "worktree_merge")
    assert merge_btn["text"]["content"] == "✅ 先合并已完成"


def test_cleanup_card_no_retry_when_all_completed():
    """Cleanup card should NOT show retry button and should keep '合并所有分支' text when all completed."""
    units = [
        {"status": "completed", "display_name": "Coco", "tool_name": "coco", "task_title": "T1"},
        {"status": "completed", "display_name": "Claude", "tool_name": "claude", "task_title": "T2"},
    ]
    _, card_json = WorktreeBuilder.build_worktree_cleanup_card(
        merge_notes=["- note"], project_id="p1", units=units,
    )
    buttons = _extract_actions(card_json)
    action_ids = [b.get("value", {}).get("action") for b in buttons]
    assert "worktree_retry_failed" not in action_ids, "Retry button should NOT be present when all completed"
    merge_btn = next(b for b in buttons if b.get("value", {}).get("action") == "worktree_merge")
    assert merge_btn["text"]["content"] == "合并所有分支"


def test_cleanup_card_shows_failed_unit_summary():
    """Cleanup card should show failed unit summary (name + task_title + error) in partial_failed scenario."""
    units = [
        {"status": "completed", "display_name": "Coco", "tool_name": "coco", "task_title": "T1"},
        {"status": "failed", "display_name": "Claude", "tool_name": "claude", "task_title": "T2", "error": "连接超时"},
    ]
    _, card_json = WorktreeBuilder.build_worktree_cleanup_card(
        merge_notes=["- note"], project_id="p1", units=units,
    )
    assert "失败单元" in card_json, "Should contain failed unit summary header"
    assert "Claude" in card_json, "Should contain failed unit display_name"
    assert "T2" in card_json, "Should contain failed unit task_title"
    assert "连接超时" in card_json, "Should contain failed unit error reason"
    # Verify the combined format: ❌ **Claude** · T2 — 连接超时
    assert "**Claude** · T2 — 连接超时" in card_json, "Should use format: name · task_title — error"


def test_cleanup_card_no_failed_summary_when_all_completed():
    """Cleanup card should NOT contain failed unit summary when all units completed."""
    units = [
        {"status": "completed", "display_name": "Coco", "tool_name": "coco", "task_title": "T1"},
        {"status": "completed", "display_name": "Claude", "tool_name": "claude", "task_title": "T2"},
    ]
    _, card_json = WorktreeBuilder.build_worktree_cleanup_card(
        merge_notes=["- note"], project_id="p1", units=units,
    )
    assert "失败单元" not in card_json, "Should NOT contain failed unit summary when all completed"


def test_cleanup_card_shows_failed_unit_summary_default_error():
    """Cleanup card should show '未知执行异常' as fallback when error field is empty."""
    units = [
        {"status": "completed", "display_name": "Coco", "tool_name": "coco", "task_title": "T1"},
        {"status": "failed", "display_name": "Claude", "tool_name": "claude", "task_title": "T2", "error": ""},
    ]
    _, card_json = WorktreeBuilder.build_worktree_cleanup_card(
        merge_notes=["- note"], project_id="p1", units=units,
    )
    assert "未知执行异常" in card_json, "Should show fallback error text when error is empty"
    assert "T2" in card_json, "Should still contain task_title even with empty error"
    assert "**Claude** · T2 — 未知执行异常" in card_json, "Should use format with task_title and fallback error"


def test_cleanup_card_failed_summary_without_task_title():
    """Cleanup card should degrade to name — error format when task_title is missing or empty."""
    units = [
        {"status": "failed", "display_name": "Claude", "tool_name": "claude", "task_title": "", "error": "超时"},
        {"status": "failed", "display_name": "Coco", "tool_name": "coco", "error": "崩溃"},
    ]
    _, card_json = WorktreeBuilder.build_worktree_cleanup_card(
        merge_notes=["- note"], project_id="p1", units=units,
    )
    # Should NOT contain ' · ' separator when task_title is empty/missing
    assert "**Claude** — 超时" in card_json, "Empty task_title should degrade to name — error"
    assert "**Coco** — 崩溃" in card_json, "Missing task_title should degrade to name — error"
    assert " · " not in card_json, "No ' · ' separator should appear when all task_titles are empty"
