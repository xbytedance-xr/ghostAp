import json
from unittest.mock import MagicMock, patch

from src.card import CardBuilder
from src.feishu.handler_context import HandlerContext
from src.feishu.handlers.worktree import WorktreeHandler
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


def test_three_tool_selection_loop_and_confirm():
    """T10: 3 tools selected in loop (incl. TTADK no-model), finalize produces correct list (AC1-AC3)."""
    project = ProjectContext(project_id="p3", project_name="P3", root_path="/tmp/p3")
    manager = WorktreeManager(project_manager=None)

    state = manager.start_selection(project)
    assert state.selection.stage == "tool_select"

    # --- Tool 1: Claude with model ---
    manager.select_tool(
        project,
        WorktreeToolOption(
            provider="cli", tool_name="claude", display_name="Claude",
            supports_model=True, model_optional=True,
        ),
    )
    assert state.selection.stage == "model_select"
    state, added, _ = manager.add_pending_item(
        project, model_name="claude-3.7-sonnet", model_display_name="Claude 3.7 Sonnet",
    )
    assert added is True
    assert len(state.selection.selected_items) == 1

    # --- Continue → Tool 2: Codex with model ---
    manager.back_to_tool_selection(project)
    assert state.selection.stage == "tool_select"
    manager.select_tool(
        project,
        WorktreeToolOption(
            provider="acp", tool_name="codex", display_name="Codex",
            supports_model=True,
        ),
    )
    state, added, _ = manager.add_pending_item(
        project, model_name="gpt-5.2", model_display_name="GPT-5.2",
    )
    assert added is True
    assert len(state.selection.selected_items) == 2

    # --- Continue → Tool 3: TTADK tmates (no model support) ---
    manager.back_to_tool_selection(project)
    manager.select_tool(
        project,
        WorktreeToolOption(
            provider="ttadk", tool_name="tmates", display_name="TMates",
            supports_model=False,
        ),
    )
    # supports_model=False → goes directly to "review", no model selection
    assert state.selection.stage == "review"
    state, added, _ = manager.add_pending_item(project)
    assert added is True
    assert len(state.selection.selected_items) == 3

    # --- Finalize ---
    state = manager.finalize_selection(project)

    assert state.enabled is True
    assert state.selection.stage == "ready"
    assert len(state.summary_lines) == 3

    # Verify content matches selections
    items = state.selection.selected_items
    assert items[0].tool_name == "claude"
    assert items[0].model_name == "claude-3.7-sonnet"
    assert items[1].tool_name == "codex"
    assert items[1].model_name == "gpt-5.2"
    assert items[2].tool_name == "tmates"
    assert items[2].supports_model is False


def test_handle_worktree_command_enters_selection_and_sends_tool_card():
    """Integration: /wt command triggers start_selection + tool select card with correct state and content."""
    project = ProjectContext(project_id="p-wt", project_name="WT-Test", root_path="/tmp/wt-test")
    manager = WorktreeManager(project_manager=None)

    # --- Step 1: simulate handle_worktree_command logic ---
    # This mirrors src/feishu/handlers/system.py:handle_worktree_command
    state = manager.start_selection(project)

    # Fake available tools (normally from _get_available_worktree_tools)
    tools = [
        WorktreeToolOption(
            provider="acp", tool_name="coco", display_name="Coco",
            description="字节跳动 AI 编程", supports_model=False,
        ).__dict__,
        WorktreeToolOption(
            provider="cli", tool_name="claude", display_name="Claude",
            description="Anthropic Claude CLI", supports_model=False,
        ).__dict__,
    ]

    selected_dicts = [item.to_dict() for item in state.selection.selected_items]
    msg_type, card_json = CardBuilder.build_worktree_tool_select_card(
        tools, selected_dicts, project.project_id,
    )

    # --- Step 2: verify state ---
    assert state.selection.active is True
    assert state.selection.stage == "tool_select"

    # --- Step 3: verify card output ---
    assert msg_type == "interactive"

    card = json.loads(card_json)
    card_str = json.dumps(card, ensure_ascii=False)

    # Card must contain worktree_select_tool action for each tool
    assert "worktree_select_tool" in card_str
    assert "Coco" in card_str
    assert "Claude" in card_str
    assert project.project_id in card_str


# ---------------------------------------------------------------------------
# Helper: create a minimally-mocked WorktreeHandler for integration tests
# ---------------------------------------------------------------------------

def _make_system_handler() -> WorktreeHandler:
    """Construct a WorktreeHandler with a fully-mocked HandlerContext."""
    ctx = MagicMock()
    ctx.settings.ref_note_enabled = False
    handler = WorktreeHandler(ctx)
    return handler


# ---------------------------------------------------------------------------
# Integration tests: /wt command through WorktreeHandler.handle_worktree_command
# ---------------------------------------------------------------------------

def test_wt_command_enters_selection_mode_and_shows_tool_prompt():
    """AC: /wt enters worktree selection mode and displays the initial tool selection prompt."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-int", project_name="INT", root_path="/tmp/int")
    handler.ctx.project_manager.get_active_project.return_value = project

    fake_tools = [
        {"provider": "acp", "tool_name": "coco", "display_name": "Coco",
         "description": "AI 编程", "supports_model": False},
        {"provider": "cli", "tool_name": "claude", "display_name": "Claude",
         "description": "Claude CLI", "supports_model": True},
    ]
    reply_mock = MagicMock()
    with patch.object(handler, "_get_available_worktree_tools", return_value=fake_tools), \
         patch.object(handler, "reply_message", reply_mock):
        handler.handle_worktree_command("msg1", "chat1", project)

    # 1) State: selection active + stage == tool_select
    state = WorktreeManager.get_state(project)
    assert state.selection.active is True
    assert state.selection.stage == "tool_select"

    # 2) reply_message was called (not reply_error)
    reply_mock.assert_called_once()
    call_args = reply_mock.call_args
    assert call_args[0][0] == "msg1"  # message_id
    sent_card_json = call_args[0][1]  # card content
    assert call_args[1]["msg_type"] == "interactive"

    # 3) Card contains tool selection action and tool names
    card_str = sent_card_json if isinstance(sent_card_json, str) else json.dumps(sent_card_json, ensure_ascii=False)
    assert "worktree_select_tool" in card_str
    assert "Coco" in card_str
    assert "Claude" in card_str


def test_wt_command_without_project_returns_error():
    """Edge: /wt without an active project should reply with an error."""
    handler = _make_system_handler()
    handler.ctx.project_manager.get_active_project.return_value = None

    error_mock = MagicMock()
    with patch.object(handler, "reply_error", error_mock):
        handler.handle_worktree_command("msg2", "chat2")

    error_mock.assert_called_once()
    error_text = str(error_mock.call_args)
    assert "请先创建或切换到一个项目" in error_text


def test_wt_command_without_available_tools_returns_error():
    """Edge: /wt with no available tools should reply with an error."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-empty", project_name="E", root_path="/tmp/e")
    handler.ctx.project_manager.get_active_project.return_value = project

    error_mock = MagicMock()
    with patch.object(handler, "_get_available_worktree_tools", return_value=[]), \
         patch.object(handler, "reply_error", error_mock):
        handler.handle_worktree_command("msg3", "chat3", project)

    error_mock.assert_called_once()
    error_text = str(error_mock.call_args)
    assert "当前环境没有可用的编程工具" in error_text


def test_worktree_select_tool_skips_model_selection_if_only_one_model():
    """AC: if a tool has only 1 model, skip model selection card and auto-add it."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-skip", project_name="SKIP", root_path="/tmp/skip")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project
    handler.ctx.project_manager.get_project_for_chat.return_value = project

    # Mock tool with supports_model=True but only 1 model
    fake_tool_value = {
        "tool_name": "single_model_tool",
        "provider": "ttadk",
        "supports_model": True,
        "display_name": "SingleTool"
    }
    
    # Mock models list with 1 item
    fake_models = [{"name": "m1", "display_name": "Model 1", "is_default": True}]
    
    patch_message_mock = MagicMock(return_value=True)
    
    with patch.object(handler, "_get_models_for_tool", return_value=fake_models), \
         patch.object(handler, "patch_message", patch_message_mock):
        
        handler.handle_worktree_select_tool("msg1", "chat1", project_id="p-skip", value=fake_tool_value)
        
    # Verify state: item should have been added and stage back to tool_select
    state = WorktreeManager.get_state(project)
    assert len(state.selection.selected_items) == 1
    assert state.selection.selected_items[0].tool_name == "single_model_tool"
    assert state.selection.selected_items[0].model_name == "m1"
    assert state.selection.stage == "tool_select"
    
    # Verify CardBuilder.build_worktree_tool_select_card was called (not model select)
    patch_message_mock.assert_called_once()
    sent_card_json = patch_message_mock.call_args[0][1]
    assert "选择工具" in sent_card_json


def test_worktree_select_tool_skips_model_selection_for_coco_even_with_multiple_models():
    """AC: if tool is 'coco', skip model selection even if it might have multiple models."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-coco", project_name="COCO", root_path="/tmp/coco")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project
    handler.ctx.project_manager.get_project_for_chat.return_value = project

    fake_tool_value = {
        "tool_name": "coco",
        "provider": "acp",
        "supports_model": True,
        "display_name": "Coco",
        "skip_model_selection": True
    }
    
    # Mock multiple models
    fake_models = [
        {"name": "m1", "display_name": "Model 1", "is_default": True},
        {"name": "m2", "display_name": "Model 2", "is_default": False}
    ]
    
    patch_message_mock = MagicMock(return_value=True)
    
    with patch.object(handler, "_get_models_for_tool", return_value=fake_models), \
         patch.object(handler, "patch_message", patch_message_mock):
        
        handler.handle_worktree_select_tool("msg2", "chat1", project_id="p-coco", value=fake_tool_value)
        
    state = WorktreeManager.get_state(project)
    assert len(state.selection.selected_items) == 1
    assert state.selection.selected_items[0].tool_name == "coco"
    assert state.selection.selected_items[0].model_name == "m1" # Default one picked
    assert state.selection.stage == "tool_select"
    
    patch_message_mock.assert_called_once()
    sent_card_json = patch_message_mock.call_args[0][1]
    assert "选择工具" in sent_card_json
