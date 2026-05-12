from unittest.mock import MagicMock, patch

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
    assert "默认模型" in state.selection.selected_items[1].display_label

    state = manager.finalize_selection(project)

    assert state.enabled is True
    assert state.selection.active is False
    assert state.selection.stage == "ready"
    assert len(state.summary_lines) == 2


def test_worktree_selection_remove_item_returns_to_tool_select_stage():
    """remove_selected_item 删除指定 selection_key 后回到 TOOL_SELECT 以便继续添加。"""
    project = ProjectContext(project_id="p_rm", project_name="P_RM", root_path="/tmp/p_rm")
    manager = WorktreeManager(project_manager=None)
    manager.start_selection(project)

    coco_opt = WorktreeToolOption(
        provider="acp", tool_name="coco", display_name="Coco",
        supports_model=True, model_optional=True,
    )
    manager.select_tool(project, coco_opt)
    manager.add_pending_item(project, model_name="m1", model_display_name="M1")
    manager.back_to_tool_selection(project)
    manager.select_tool(project, coco_opt)
    state, _, _ = manager.add_pending_item(project, model_name="m2", model_display_name="M2")
    assert len(state.selection.selected_items) == 2

    target_key = state.selection.selected_items[0].selection_key
    state, removed, msg = manager.remove_selected_item(project, target_key)

    assert removed is True
    assert "已移除" in msg
    assert len(state.selection.selected_items) == 1
    assert state.selection.selected_items[0].model_name == "m2"
    assert state.selection.stage == "tool_select"
    assert state.selection.active is True


def test_worktree_selection_remove_item_unknown_key_is_safe():
    project = ProjectContext(project_id="p_rm2", project_name="P_RM2", root_path="/tmp/p_rm2")
    manager = WorktreeManager(project_manager=None)
    manager.start_selection(project)

    state, removed, msg = manager.remove_selected_item(project, "no:such:key")
    assert removed is False
    assert "未找到" in msg
    assert state.selection.stage == "tool_select"


def test_worktree_selection_clear_items_resets_to_tool_select_stage():
    project = ProjectContext(project_id="p_clr", project_name="P_CLR", root_path="/tmp/p_clr")
    manager = WorktreeManager(project_manager=None)
    manager.start_selection(project)

    opt = WorktreeToolOption(
        provider="acp", tool_name="claude", display_name="Claude",
        supports_model=True, model_optional=True,
    )
    manager.select_tool(project, opt)
    manager.add_pending_item(project, model_name="sonnet", model_display_name="Sonnet")
    manager.back_to_tool_selection(project)
    manager.select_tool(project, opt)
    manager.add_pending_item(project, model_name="opus", model_display_name="Opus")

    state, n, msg = manager.clear_selected_items(project)
    assert n == 2
    assert "已清空已选 2 项" in msg
    assert state.selection.selected_items == []
    assert state.selection.stage == "tool_select"


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
    """AC: /wt enters worktree selection mode and dispatches WORKTREE_TOOL_SELECT event."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-int", project_name="INT", root_path="/tmp/int")
    handler.ctx.project_manager.get_active_project.return_value = project

    fake_tools = [
        {"provider": "acp", "tool_name": "coco", "display_name": "Coco",
         "description": "AI 编程", "supports_model": False},
        {"provider": "cli", "tool_name": "claude", "display_name": "Claude",
         "description": "Claude CLI", "supports_model": True},
    ]
    mock_session = MagicMock()
    mock_session.closed = False
    with patch.object(handler, "_get_available_worktree_tools", return_value=fake_tools), \
         patch.object(handler, "_get_or_create_session", return_value=mock_session):
        handler.handle_worktree_command("msg1", "chat1", project)

    # 1) State: selection active + stage == tool_select
    state = WorktreeManager.get_state(project)
    assert state.selection.active is True
    assert state.selection.stage == "tool_select"

    # 2) session.dispatch was called with WORKTREE_TOOL_SELECT event
    mock_session.dispatch.assert_called_once()
    event = mock_session.dispatch.call_args[0][0]
    from src.card.events import CardEventType
    assert event.type == CardEventType.WORKTREE_TOOL_SELECT

    # 3) Event payload contains tools
    assert len(event.payload["tools"]) == 2


def test_wt_command_shows_single_ttadk_entry_in_top_level_tool_list():
    """Top-level /wt tool card should expose TTADK as one aggregate entry instead of flattening TTADK tools."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-ttadk", project_name="INT", root_path="/tmp/int")
    handler.ctx.project_manager.get_active_project.return_value = project

    fake_tools = [
        {"provider": "acp", "tool_name": "coco", "display_name": "Coco", "description": "AI 编程", "supports_model": True},
        {"provider": "ttadk", "tool_name": "ttadk", "display_name": "TTADK", "description": "TTADK 多工具入口", "supports_model": False},
    ]
    mock_session = MagicMock()
    mock_session.closed = False

    with patch.object(handler, "_get_available_worktree_tools", return_value=fake_tools), \
         patch.object(handler, "_get_or_create_session", return_value=mock_session):
        handler.handle_worktree_command("msg-ttadk", "chat1", project)

    event = mock_session.dispatch.call_args[0][0]
    # TTADK should be in the tools list
    tool_names = [t.get("display_name", "") for t in event.payload["tools"]]
    assert "TTADK" in tool_names
    assert "TTADK · coco" not in tool_names
    assert "TTADK · claude" not in tool_names


def test_wt_command_top_level_tool_card_uses_product_entry_order():
    """Top-level /wt card should prioritize native product entries before TTADK."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-order", project_name="INT", root_path="/tmp/int")
    handler.ctx.project_manager.get_active_project.return_value = project

    fake_tools = [
        {"provider": "acp", "tool_name": "coco", "display_name": "Coco", "description": "AI 编程", "supports_model": True},
        {"provider": "acp", "tool_name": "aiden", "display_name": "Aiden", "description": "AI 编程", "supports_model": True},
        {"provider": "acp", "tool_name": "codex", "display_name": "Codex", "description": "AI 编程", "supports_model": True},
        {"provider": "cli", "tool_name": "claude", "display_name": "Claude", "description": "Claude CLI", "supports_model": True},
        {"provider": "ttadk", "tool_name": "ttadk", "display_name": "TTADK", "description": "TTADK 多工具入口", "supports_model": False},
    ]
    mock_session = MagicMock()
    mock_session.closed = False

    with patch.object(handler, "_get_available_worktree_tools", return_value=fake_tools), \
         patch.object(handler, "_get_or_create_session", return_value=mock_session):
        handler.handle_worktree_command("msg-order", "chat1", project)

    event = mock_session.dispatch.call_args[0][0]
    tool_names = [t.get("display_name", "") for t in event.payload["tools"]]
    # Tools should be in the order provided by _get_available_worktree_tools
    ordered_names = ["Coco", "Aiden", "Codex", "Claude", "TTADK"]
    assert tool_names == ordered_names


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


def test_worktree_select_tool_shows_model_card_even_with_single_model():
    """Worktree 始终弹模型选择卡（即便 models==1）以贯彻'工具×模型'语义。

    旧行为为 len(models)<=1 时自动 skip + 直接添加；改造后需要展示模型卡，让用户
    显式确认所选模型，从而支持后续同工具不同模型多次添加。
    """
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-show", project_name="SHOW", root_path="/tmp/show")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project
    handler.ctx.project_manager.get_project_for_chat.return_value = project

    fake_tool_value = {
        "tool_name": "single_model_tool",
        "provider": "ttadk",
        "supports_model": True,
        "display_name": "SingleTool",
    }
    fake_models = [{"name": "m1", "display_name": "Model 1", "is_default": True}]

    mock_session = MagicMock()
    mock_session.closed = False

    with patch.object(handler, "_get_models_for_tool", return_value=fake_models), \
         patch.object(handler, "_get_or_create_session", return_value=mock_session):
        handler.handle_worktree_select_tool("msg1", "chat1", project_id="p-show", value=fake_tool_value)

    state = WorktreeManager.get_state(project)
    # 卡片仍处于 model_select 阶段（pending_item 非空，selected_items 仍为空）
    assert state.selection.stage == "model_select"
    assert state.selection.pending_item is not None
    assert state.selection.pending_item.tool_name == "single_model_tool"
    assert state.selection.selected_items == []

    mock_session.dispatch.assert_called_once()
    event = mock_session.dispatch.call_args[0][0]
    from src.card.events import CardEventType
    assert event.type == CardEventType.WORKTREE_TOOL_SELECT
    assert event.payload.get("select_action") == "worktree_select_model"


def test_worktree_select_tool_falls_back_when_no_models_returned():
    """无可用模型时应自动 fallback 添加为默认模型，避免出现空模型卡。"""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-empty", project_name="EMPTY", root_path="/tmp/empty")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project
    handler.ctx.project_manager.get_project_for_chat.return_value = project

    fake_tool_value = {
        "tool_name": "no_model_tool",
        "provider": "acp",
        "supports_model": True,
        "display_name": "NoModelTool",
    }

    mock_session = MagicMock()
    mock_session.closed = False

    with patch.object(handler, "_get_models_for_tool", return_value=[]), \
         patch.object(handler, "_get_or_create_session", return_value=mock_session), \
         patch.object(handler, "_get_available_worktree_tools", return_value=[]):
        handler.handle_worktree_select_tool("msg-e", "chat-e", project_id="p-empty", value=fake_tool_value)

    state = WorktreeManager.get_state(project)
    # 0 模型分支：直接添加 + 回到 tool_select
    assert len(state.selection.selected_items) == 1
    assert state.selection.selected_items[0].tool_name == "no_model_tool"
    assert state.selection.stage == "tool_select"


def test_worktree_card_click_flow_accumulates_native_default_and_ttadk_model_tools():
    """Click flow: ACP tool→model, native default tool, TTADK→tool→model, then confirm."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-clicks", project_name="CLICKS", root_path="/tmp/clicks")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project
    handler.ctx.project_manager.get_project_for_chat.return_value = project

    top_tools = [
        {"provider": "acp", "tool_name": "coco", "display_name": "Coco", "supports_model": True},
        {"provider": "cli", "tool_name": "claude", "display_name": "Claude", "supports_model": False},
        {"provider": "ttadk", "tool_name": "ttadk", "display_name": "TTADK", "supports_model": False},
    ]
    ttadk_tools = [
        {"provider": "ttadk", "tool_name": "codex", "display_name": "TTADK · codex", "supports_model": True},
    ]

    def models_for(tool_name, provider="ttadk", **_kwargs):
        if provider == "acp" and tool_name == "coco":
            return [{"name": "doubao-pro", "display_name": "Doubao Pro", "is_default": True}]
        if provider == "ttadk" and tool_name == "codex":
            return [{"name": "gpt-5.2", "display_name": "GPT-5.2", "is_default": True}]
        return []

    mock_session = MagicMock()
    mock_session.closed = False

    with patch.object(handler, "_get_available_worktree_tools", return_value=top_tools), \
         patch.object(handler, "_get_ttadk_worktree_tools", return_value=ttadk_tools), \
         patch.object(handler, "_get_models_for_tool", side_effect=models_for), \
         patch.object(handler, "_get_or_create_session", return_value=mock_session):
        handler.handle_worktree_command("msg-start", "chat1", project)
        handler.handle_worktree_select_tool(
            "msg-coco", "chat1", project_id="p-clicks",
            value={"provider": "acp", "tool_name": "coco", "display_name": "Coco", "supports_model": True},
        )
        handler.handle_worktree_select_model(
            "msg-coco-model", "chat1", project_id="p-clicks",
            value={"model_name": "doubao-pro", "model_display_name": "Doubao Pro"},
        )
        handler.handle_worktree_select_tool(
            "msg-claude", "chat1", project_id="p-clicks",
            value={"provider": "cli", "tool_name": "claude", "display_name": "Claude", "supports_model": False},
        )
        handler.handle_worktree_select_tool(
            "msg-ttadk", "chat1", project_id="p-clicks",
            value={"provider": "ttadk", "tool_name": "ttadk", "display_name": "TTADK", "supports_model": False},
        )
        ttadk_event = mock_session.dispatch.call_args[0][0]
        assert [(t["agent_name"], t["display_name"]) for t in ttadk_event.payload["tools"]] == [
            ("ttadk", "codex")
        ]
        handler.handle_worktree_select_tool(
            "msg-codex", "chat1", project_id="p-clicks",
            value={"provider": "ttadk", "agent_name": "ttadk", "tool_name": "codex", "display_name": "codex", "supports_model": True},
        )
        handler.handle_worktree_select_model(
            "msg-codex-model", "chat1", project_id="p-clicks",
            value={"model_name": "gpt-5.2", "model_display_name": "GPT-5.2"},
        )
        handler.handle_finish_worktree_selection("msg-finish", "chat1", project_id="p-clicks")

    state = WorktreeManager.get_state(project)
    exported = [item.to_dict() for item in state.selection.selected_items]
    assert [(i["agent_name"], i["tool_name"], i["effective_model_display_name"]) for i in exported] == [
        ("", "coco", "Doubao Pro"),
        ("", "claude", "默认模型"),
        ("ttadk", "codex", "GPT-5.2"),
    ]
    confirm_event = mock_session.dispatch.call_args[0][0]
    assert confirm_event.payload["selected_items"] == exported


def test_worktree_command_binds_topic_even_without_existing_thread_context():
    """WT force-enables topic semantics by binding the command message as root."""
    from src.thread import get_current_thread_id, get_thread_manager, set_current_thread_id

    handler = _make_system_handler()
    project = ProjectContext(project_id="p-topic-force", project_name="P", root_path="/tmp/p")
    handler.ctx.project_manager.get_active_project.return_value = project
    mock_session = MagicMock()
    mock_session.closed = False

    try:
        set_current_thread_id(None)
        with patch.object(handler, "_get_available_worktree_tools", return_value=[
            {"provider": "acp", "tool_name": "coco", "display_name": "Coco", "supports_model": False},
        ]), patch.object(handler, "_get_or_create_session", return_value=mock_session):
            handler.handle_worktree_command("msg-root", "chat1", project)

        assert get_current_thread_id() == "msg-root"
        ctx = get_thread_manager().get_engine_context("msg-root")
        assert ctx is not None
        assert ctx.mode == "worktree"
        event = mock_session.dispatch.call_args.args[0]
        assert event.payload["thread_root_id"] == "msg-root"
    finally:
        get_thread_manager().remove("msg-root")
        set_current_thread_id(None)


def test_worktree_select_model_accepts_generic_model_payload_and_returns_to_confirmable_menu():
    """Model callbacks may carry id/name fields; selecting one should add the item and show confirm."""
    handler = _make_system_handler()
    project = ProjectContext(project_id="p-codex", project_name="CODEX", root_path="/tmp/codex")
    handler.ctx.project_manager.get_active_project.return_value = project
    handler.ctx.project_manager.get_project.return_value = project
    handler.ctx.project_manager.get_project_for_chat.return_value = project

    mgr = handler._worktree_manager()
    mgr.start_selection(project)
    mgr.select_tool(
        project,
        WorktreeToolOption(
            provider="acp",
            tool_name="codex",
            display_name="Codex",
            supports_model=True,
            model_optional=True,
        ),
    )

    top_tools = [
        {"provider": "acp", "tool_name": "aiden", "display_name": "Aiden", "supports_model": True},
        {"provider": "acp", "tool_name": "codex", "display_name": "Codex", "supports_model": True},
    ]
    mock_session = MagicMock()
    mock_session.closed = False

    with patch.object(handler, "_get_available_worktree_tools", return_value=top_tools), \
         patch.object(handler, "_get_or_create_session", return_value=mock_session):
        handler.handle_worktree_select_model(
            "msg-model",
            "chat1",
            project_id="p-codex",
            value={"id": "gpt-5.5", "name": "GPT-5.5"},
        )

    state = WorktreeManager.get_state(project)
    assert len(state.selection.selected_items) == 1
    assert state.selection.selected_items[0].tool_name == "codex"
    assert state.selection.selected_items[0].model_name == "gpt-5.5"
    assert state.selection.selected_items[0].model_display_name == "GPT-5.5"
    assert state.selection.stage == "tool_select"

    event = mock_session.dispatch.call_args[0][0]
    assert event.payload["tools"] == top_tools
    assert len(event.payload["selected"]) == 1
    assert event.payload["selected"][0]["selection_key"] == "acp:codex:gpt-5.5"
