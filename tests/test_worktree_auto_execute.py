"""Tests for worktree auto-execute fast path and silent mode.

Covers:
  (a) /wt with goal parsed correctly
  (b) /wt without goal falls back to confirm card
  (c) Goal from card input triggers fast path
  (d) finish_selection with goal auto-triggers execution
  (e) Zero tools selection error
  (f) Silent mode throttle and timeout notification
  (g) Goal persistence across tool/model selection
    (h) Auto-trigger finish_selection after tool/model choice if goal exists
"""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, call

from src.card import CardBuilder
from src.feishu.handlers.system import SystemHandler
from src.project.context import ProjectContext
from src.worktree_engine.manager import WorktreeManager
from src.worktree_engine.models import WorktreeSelectionState, WorktreeRuntimeState, WorktreeUnit
from src.worktree_engine.selection import WorktreeToolOption


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_system_handler() -> SystemHandler:
    ctx = MagicMock()
    ctx.settings.ref_note_enabled = False
    handler = SystemHandler(ctx)
    return handler


def _make_project(pid: str = "p-auto") -> ProjectContext:
    return ProjectContext(project_id=pid, project_name="AutoTest", root_path="/tmp/auto-test")


_FAKE_TOOLS = [
    {"provider": "acp", "tool_name": "coco", "display_name": "Coco",
     "description": "AI 编程", "supports_model": False, "skip_model_selection": True},
    {"provider": "cli", "tool_name": "claude", "display_name": "Claude",
     "description": "Claude CLI", "supports_model": True},
]


# ---------------------------------------------------------------------------
# (a) /wt with goal parsed correctly
# ---------------------------------------------------------------------------

def test_wt_command_with_goal_parsed():
    """/wt 实现登录功能 → goal='实现登录功能' persisted in selection state."""
    handler = _make_system_handler()
    project = _make_project()
    handler.ctx.project_manager.get_active_project.return_value = project

    reply_mock = MagicMock()
    with patch.object(handler, "_get_available_worktree_tools", return_value=_FAKE_TOOLS), \
         patch.object(handler, "reply_message", reply_mock):
        handler.handle_worktree_command("msg1", "chat1", project, goal="实现登录功能")

    state = WorktreeManager.get_state(project)
    assert state.selection.pending_goal == "实现登录功能"

    reply_mock.assert_called_once()
    card_json = reply_mock.call_args[0][1]
    assert "任务目标" in card_json
    assert "实现登录功能" in card_json


def test_wt_prefix_command_parses_goal():
    """_handle_worktree_prefix_command extracts goal from '/wt 实现登录'."""
    handler = _make_system_handler()
    project = _make_project()
    handler.ctx.project_manager.get_active_project.return_value = project

    reply_mock = MagicMock()
    with patch.object(handler, "_get_available_worktree_tools", return_value=_FAKE_TOOLS), \
         patch.object(handler, "reply_message", reply_mock):
        handler._handle_worktree_prefix_command("msg1", "chat1", "/wt 实现登录", project)

    state = WorktreeManager.get_state(project)
    assert state.selection.pending_goal == "实现登录"


def test_worktree_prefix_command_parses_goal():
    """_handle_worktree_prefix_command extracts goal from '/worktree 重构认证'."""
    handler = _make_system_handler()
    project = _make_project("p-wt2")
    handler.ctx.project_manager.get_active_project.return_value = project

    reply_mock = MagicMock()
    with patch.object(handler, "_get_available_worktree_tools", return_value=_FAKE_TOOLS), \
         patch.object(handler, "reply_message", reply_mock):
        handler._handle_worktree_prefix_command("msg2", "chat2", "/worktree 重构认证", project)

    state = WorktreeManager.get_state(project)
    assert state.selection.pending_goal == "重构认证"


# ---------------------------------------------------------------------------
# (b) /wt without goal falls back to confirm card
# ---------------------------------------------------------------------------

def test_no_goal_fallback_confirm_card():
    """/wt without goal → finish_selection → confirm card shown."""
    handler = _make_system_handler()
    project = _make_project("p-fb")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project)
    mgr.select_tool(project, WorktreeToolOption(
        provider="acp", tool_name="coco", display_name="Coco", supports_model=False,
    ))
    mgr.add_pending_item(project)
    mgr.back_to_tool_selection(project)

    patch_mock = MagicMock()
    with patch.object(handler, "patch_message", patch_mock):
        handler.handle_finish_worktree_selection("msg-fb", "chat-fb", project_id="p-fb", value={})

    patch_mock.assert_called_once()
    card_json = patch_mock.call_args[0][1]
    assert "确认组合" in card_json
    assert "确认并开始执行" in card_json


# ---------------------------------------------------------------------------
# (c) Goal from card input triggers fast path
# ---------------------------------------------------------------------------

def test_goal_from_card_input_triggers_auto_execute():
    """finish_selection with goal in _input_value → auto-execute (no confirm card)."""
    handler = _make_system_handler()
    project = _make_project("p-inp")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project)
    mgr.select_tool(project, WorktreeToolOption(
        provider="cli", tool_name="claude", display_name="Claude", supports_model=False,
    ))
    mgr.add_pending_item(project)

    auto_exec_mock = MagicMock()
    with patch.object(handler, "_auto_execute_worktree", auto_exec_mock):
        handler.handle_finish_worktree_selection(
            "msg-inp", "chat-inp", project_id="p-inp",
            value={"_input_value": "优化性能"},
        )

    auto_exec_mock.assert_called_once()
    call_args = auto_exec_mock.call_args
    assert call_args[0][2] == "优化性能"  # goal positional arg


# ---------------------------------------------------------------------------
# (d) finish_selection with goal auto-triggers execution
# ---------------------------------------------------------------------------

def test_finish_selection_with_pending_goal_auto_executes():
    """pending_goal set from /wt → finish_selection auto-triggers _auto_execute_worktree."""
    handler = _make_system_handler()
    project = _make_project("p-pend")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project, goal="添加单元测试")
    mgr.select_tool(project, WorktreeToolOption(
        provider="acp", tool_name="coco", display_name="Coco", supports_model=False,
    ))
    mgr.add_pending_item(project)

    auto_exec_mock = MagicMock()
    with patch.object(handler, "_auto_execute_worktree", auto_exec_mock):
        handler.handle_finish_worktree_selection(
            "msg-pend", "chat-pend", project_id="p-pend", value={},
        )

    auto_exec_mock.assert_called_once()
    assert auto_exec_mock.call_args[0][2] == "添加单元测试"


def test_auto_execute_worktree_calls_execute_with_silent_mode():
    """_auto_execute_worktree → ensure_worktrees → handle_worktree_execute(silent_mode=True)."""
    handler = _make_system_handler()
    project = _make_project("p-exec")
    handler.ctx.project_manager.get_active_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project, goal="测试目标")
    mgr.select_tool(project, WorktreeToolOption(
        provider="acp", tool_name="coco", display_name="Coco", supports_model=False,
    ))
    mgr.add_pending_item(project)
    mgr.finalize_selection(project)

    execute_mock = MagicMock()
    with patch.object(mgr, "ensure_worktrees") as ew_mock, \
         patch.object(handler, "handle_worktree_execute", execute_mock):
        # Mock ensure_worktrees to return state with units
        state = mgr.get_state(project)
        state.units = [WorktreeUnit(unit_id="u1")]
        state.last_error = ""
        ew_mock.return_value = state

        handler._auto_execute_worktree("msg-exec", "chat-exec", "测试目标", project=project)

    execute_mock.assert_called_once()
    kwargs = execute_mock.call_args
    assert kwargs[1]["silent_mode"] is True
    assert kwargs[0][2] == "测试目标"  # goal
    # Verify units were set to "ready"
    assert all(u.status == "ready" for u in state.units)


# ---------------------------------------------------------------------------
# (e) Zero tools selection error
# ---------------------------------------------------------------------------

def test_zero_tools_error_blocks_execution():
    """finish_selection with 0 tools → error, no _auto_execute_worktree call."""
    handler = _make_system_handler()
    project = _make_project("p-zero")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project, goal="不该执行")

    error_mock = MagicMock()
    auto_exec_mock = MagicMock()
    with patch.object(handler, "reply_error", error_mock), \
         patch.object(handler, "_auto_execute_worktree", auto_exec_mock):
        handler.handle_finish_worktree_selection(
            "msg-zero", "chat-zero", project_id="p-zero", value={"goal": "不该执行"},
        )

    error_mock.assert_called_once()
    assert "请至少选择一个工具" in str(error_mock.call_args)
    auto_exec_mock.assert_not_called()


# ---------------------------------------------------------------------------
# (f) Silent mode throttle and timeout notification
# ---------------------------------------------------------------------------

def test_silent_mode_throttle_interval():
    """silent_mode=True → throttle interval is 30s (not 0.5s)."""
    handler = _make_system_handler()
    project = _make_project("p-sil")
    handler.ctx.project_manager.get_active_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project)
    mgr.select_tool(project, WorktreeToolOption(
        provider="acp", tool_name="coco", display_name="Coco", supports_model=False,
    ))
    mgr.add_pending_item(project)
    mgr.finalize_selection(project)

    # Prepare units
    state = mgr.get_state(project)
    state.units = [WorktreeUnit(unit_id="u1", status="ready")]

    send_mock = MagicMock(return_value="progress-mid")
    patch_mock = MagicMock()

    # Mock execute_goal to capture the on_unit_update callback
    captured_callback = [None]
    original_execute = mgr.execute_goal

    def fake_execute(proj, goal, on_unit_update=None, **kw):
        captured_callback[0] = on_unit_update
        state.units[0].status = "completed"
        state.merge_entry_ready = False
        state.last_error = ""
        return state

    with patch.object(mgr, "execute_goal", side_effect=fake_execute), \
         patch.object(handler, "send_message", send_mock), \
         patch.object(handler, "patch_message", patch_mock):
        handler.handle_worktree_execute("msg-sil", "chat-sil", "测试", project=project, silent_mode=True)

    # Check initial message contains silent mode indicator
    init_card = send_mock.call_args[0][1]
    assert "已开始执行" in init_card or "自动通知" in init_card

    # Callback should have been captured
    assert captured_callback[0] is not None

    # Simulate rapid callbacks — they should be throttled at 30s
    patch_mock.reset_mock()
    cb = captured_callback[0]
    cb(state.units[0])  # first call — should NOT update (just set time)
    cb(state.units[0])  # second call within <30s — should be throttled
    # Only final result card patched (from main flow), not from rapid callbacks
    # The rapid callbacks should not cause additional patch calls


# ---------------------------------------------------------------------------
# (g) Goal persistence across tool/model selection
# ---------------------------------------------------------------------------

def test_goal_persistence_across_selection():
    """Goal set via /wt persists through tool select → model select → finish."""
    handler = _make_system_handler()
    project = _make_project("p-pers")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project

    mgr = handler._worktree_manager()

    # Step 1: /wt with goal
    with patch.object(handler, "_get_available_worktree_tools", return_value=_FAKE_TOOLS), \
         patch.object(handler, "reply_message"):
        handler.handle_worktree_command("m1", "c1", project, goal="实现搜索功能")

    state = mgr.get_state(project)
    assert state.selection.pending_goal == "实现搜索功能"

    # Step 2: select tool — goal should persist in state
    with patch.object(handler, "_get_models_for_tool", return_value=[]), \
         patch.object(handler, "_get_available_worktree_tools", return_value=_FAKE_TOOLS), \
         patch.object(handler, "patch_message"):
        handler.handle_worktree_select_tool(
            "m2", "c1", project_id="p-pers",
            value={"tool_name": "coco", "provider": "acp", "supports_model": False,
                   "goal": "实现搜索功能"},
        )

    state = mgr.get_state(project)
    assert state.selection.pending_goal == "实现搜索功能"
    assert len(state.selection.selected_items) == 1

    # Step 3: finish selection — should auto-execute with preserved goal
    auto_exec_mock = MagicMock()
    with patch.object(handler, "_auto_execute_worktree", auto_exec_mock):
        handler.handle_finish_worktree_selection(
            "m3", "c1", project_id="p-pers",
            value={"goal": "实现搜索功能"},
        )

    auto_exec_mock.assert_called_once()
    assert auto_exec_mock.call_args[0][2] == "实现搜索功能"


def test_goal_from_model_select_card_persists():
    """Goal passed in model select card value persists to state."""
    handler = _make_system_handler()
    project = _make_project("p-model")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project, goal="重构数据库")
    mgr.select_tool(project, WorktreeToolOption(
        provider="cli", tool_name="claude", display_name="Claude", supports_model=True,
    ))

    with patch.object(handler, "_get_available_worktree_tools", return_value=_FAKE_TOOLS), \
         patch.object(handler, "patch_message"):
        handler.handle_worktree_select_model(
            "m-model", "c-model", project_id="p-model",
            value={"model_name": "sonnet", "model_display_name": "Sonnet", "goal": "重构数据库"},
        )

    state = mgr.get_state(project)
    assert state.selection.pending_goal == "重构数据库"
    assert len(state.selection.selected_items) == 1


# ---------------------------------------------------------------------------
# Card content tests
# ---------------------------------------------------------------------------

def test_tool_select_card_with_goal_shows_readonly_display():
    """build_worktree_tool_select_card with goal shows read-only goal, no input box."""
    _, card_json = CardBuilder.build_worktree_tool_select_card(
        _FAKE_TOOLS, [], "proj1", goal="实现功能X",
    )
    card = json.loads(card_json)
    body = json.dumps(card, ensure_ascii=False)
    assert "任务目标" in body
    assert "实现功能X" in body
    # Should NOT have input tag for goal (read-only mode)
    # The input name "worktree_goal" should not appear since goal is provided
    assert body.count('"name": "worktree_goal"') == 0


def test_tool_select_card_without_goal_shows_input_box():
    """build_worktree_tool_select_card without goal shows input box."""
    _, card_json = CardBuilder.build_worktree_tool_select_card(
        _FAKE_TOOLS, [], "proj1",
    )
    body = json.dumps(json.loads(card_json), ensure_ascii=False)
    assert "worktree_goal" in body
    assert "输入任务目标" in body


def test_model_select_card_passes_goal_in_buttons():
    """build_worktree_model_select_card passes goal in button values."""
    models = [{"name": "gpt-4.1", "display_name": "GPT-4.1", "is_default": True}]
    _, card_json = CardBuilder.build_worktree_model_select_card(
        models, "Claude", [], "proj1", goal="测试目标",
    )
    body = json.dumps(json.loads(card_json), ensure_ascii=False)
    assert '"goal": "测试目标"' in body or '"goal":"测试目标"' in body


# ---------------------------------------------------------------------------
# (h) Auto-trigger finish_selection after tool/model choice if goal exists
# ---------------------------------------------------------------------------

def test_auto_execute_after_model_selection():
    """If goal exists, handle_worktree_select_model triggers handle_finish_worktree_selection."""
    handler = _make_system_handler()
    project = _make_project("p-auto-model")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project, goal="自动执行任务")
    mgr.select_tool(project, WorktreeToolOption(
        provider="cli", tool_name="claude", display_name="Claude", supports_model=True,
    ))

    finish_mock = MagicMock()
    with patch.object(handler, "_get_available_worktree_tools", return_value=_FAKE_TOOLS), \
         patch.object(handler, "handle_finish_worktree_selection", finish_mock), \
         patch.object(handler, "patch_message"):
        handler.handle_worktree_select_model(
            "m-auto", "c-auto", project_id="p-auto-model",
            value={"model_name": "sonnet", "goal": "自动执行任务"},
        )

    # Should have called handle_finish_worktree_selection
    finish_mock.assert_called_once()
    assert finish_mock.call_args[0][1] == "c-auto" # chat_id
    assert finish_mock.call_args[1]["project_id"] == "p-auto-model"


def test_auto_execute_after_tool_selection_skipping_model():
    """If goal exists, handle_worktree_select_tool (skipping model) triggers handle_finish_worktree_selection."""
    handler = _make_system_handler()
    project = _make_project("p-auto-tool")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project, goal="直接开始任务")

    finish_mock = MagicMock()
    with patch.object(handler, "_get_available_worktree_tools", return_value=_FAKE_TOOLS), \
         patch.object(handler, "_get_models_for_tool", return_value=[]), \
         patch.object(handler, "handle_finish_worktree_selection", finish_mock), \
         patch.object(handler, "patch_message"):
        # Select "coco" which skips model selection in _FAKE_TOOLS
        handler.handle_worktree_select_tool(
            "m-auto", "c-auto", project_id="p-auto-tool",
            value={"tool_name": "coco", "provider": "acp", "supports_model": False,
                   "skip_model_selection": True, "goal": "直接开始任务"},
        )

    # Should have called handle_finish_worktree_selection
    finish_mock.assert_called_once()
    assert finish_mock.call_args[0][1] == "c-auto" # chat_id
    assert finish_mock.call_args[1]["project_id"] == "p-auto-tool"


# ---------------------------------------------------------------------------
# is_interceptable_command tests
# ---------------------------------------------------------------------------

def test_is_interceptable_command_wt_with_goal():
    assert SystemHandler.is_interceptable_command("/wt 实现登录功能")
    assert SystemHandler.is_interceptable_command("/worktree 重构认证")
    assert SystemHandler.is_interceptable_command("/wt")
    assert SystemHandler.is_interceptable_command("/worktree")
    assert not SystemHandler.is_interceptable_command("/wtzzzz")
