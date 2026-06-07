"""Tests for Workflow orchestrator agent selection (AC2 related)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.feishu.handlers.workflow import WorkflowHandler
from src.workflow_engine.constants import (
    DEFAULT_ORCHESTRATOR_AGENT,
    ORCHESTRATOR_AGENT_OPTIONS,
)
from src.workflow_engine.models import (
    PendingConfirmation,
    WorkflowProject,
    WorkflowStatus,
)


def _create_mock_handler():
    """Create a mock WorkflowHandler with basic dependencies."""
    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = MagicMock()
    handler.reply_card = MagicMock()
    handler.send_card_to_chat = MagicMock()
    handler._reply_workflow_error = MagicMock()
    handler._show_tool_selection_card = MagicMock()
    handler._get_root_path = MagicMock(return_value="/tmp")
    handler._get_project_for_chat = MagicMock(return_value=MagicMock(project_id="test_proj"))
    handler.get_engine_name = MagicMock(return_value="test_engine")
    return handler


def test_agent_selection_card_shown_on_wf_command():
    """主编排 Agent 选择卡在 /wf 命令后显示。"""
    handler = _create_mock_handler()
    
    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.IDLE,
    )
    handler.ctx.workflow_engine_manager.get_or_create = MagicMock(return_value=mock_engine)
    
    handler._show_agent_selection_card(
        chat_id="test_chat",
        requirement="test requirement",
        project=MagicMock(project_id="test_proj"),
        root_path="/tmp",
    )
    
    # Verify engine state is updated
    assert mock_engine.project.status == WorkflowStatus.AWAITING_AGENT_SELECT
    assert mock_engine.project.pending is not None
    assert mock_engine.project.pending.orchestrator_agent == DEFAULT_ORCHESTRATOR_AGENT
    assert mock_engine.project.pending.requirement == "test requirement"
    
    # Verify card was sent
    handler.send_card_to_chat.assert_called_once()
    card = handler.send_card_to_chat.call_args[0][1]
    assert isinstance(card, dict)
    assert "header" in card
    assert "Agent" in str(card["header"]["title"]["content"])


def test_agent_selection_card_contains_all_options():
    """Agent 选择卡包含所有 ORCHESTRATOR_AGENT_OPTIONS。"""
    handler = _create_mock_handler()
    
    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.IDLE,
    )
    handler.ctx.workflow_engine_manager.get_or_create = MagicMock(return_value=mock_engine)
    
    handler._show_agent_selection_card(
        chat_id="test_chat",
        requirement="test",
        project=MagicMock(project_id="test_proj"),
        root_path="/tmp",
    )
    
    card = handler.send_card_to_chat.call_args[0][1]
    card_str = str(card)
    
    # All agent options should be present
    for agent_type, display_name, _ in ORCHESTRATOR_AGENT_OPTIONS:
        assert display_name in card_str, f"Agent {display_name} not found in card"


def test_handle_workflow_select_agent_success():
    """选择 Agent 成功后进入工具选择阶段。"""
    handler = _create_mock_handler()
    
    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_AGENT_SELECT,
        pending=PendingConfirmation(
            requirement="test requirement",
            engine_session_key="session_123",
            initiator_user_id="test_user",
            orchestrator_agent=DEFAULT_ORCHESTRATOR_AGENT,
        ),
    )
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=mock_engine)
    
    with patch('src.thread.get_current_sender_id', return_value="test_user"):
        handler.handle_workflow_select_agent(
            message_id="msg_123",
            chat_id="test_chat",
            project_id="test_proj",
            value={
                "action": "workflow_select_agent",
                "agent_type": "claude",
                "engine_session_key": "session_123",
                "project_id": "test_proj",
            },
        )
    
    # Verify agent selection was stored
    assert mock_engine.project.pending.orchestrator_agent == "claude"
    
    # Verify transition to tool selection
    handler._show_tool_selection_card.assert_called_once()


def test_handle_workflow_select_agent_wrong_state():
    """错误状态下选择 Agent 返回 invalid_state 错误。"""
    handler = _create_mock_handler()
    
    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.RUNNING,  # Wrong state
        pending=PendingConfirmation(
            engine_session_key="session_123",
            initiator_user_id="test_user",
        ),
    )
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=mock_engine)
    
    with patch('src.thread.get_current_sender_id', return_value="test_user"):
        handler.handle_workflow_select_agent(
            message_id="msg_123",
            chat_id="test_chat",
            project_id=None,
            value={
                "action": "workflow_select_agent",
                "agent_type": "claude",
                "engine_session_key": "session_123",
            },
        )
    
    handler._reply_workflow_error.assert_called_once()
    call_args = handler._reply_workflow_error.call_args
    assert call_args[0][1] == "invalid_state"


def test_script_gen_uses_selected_agent():
    """脚本生成使用用户选择的 Agent 类型。"""
    from src.workflow_engine.script_gen import build_script_gen_prompt
    
    # Test with different orchestrator agents
    for agent_type, _, _ in ORCHESTRATOR_AGENT_OPTIONS:
        prompt = build_script_gen_prompt(
            requirement="test",
            available_tools=["coco", "claude"],
            available_roles=["architect"],
            budget_total=2000000,
            budget_tokens=2000000,
            orchestrator_agent=agent_type,
        )
        
        # The agent type should appear in the prompt
        assert agent_type in prompt, f"Agent type {agent_type} not found in prompt"


def test_script_gen_default_agent():
    """未指定 orchestrator_agent 时使用默认值。"""
    from src.workflow_engine.script_gen import build_script_gen_prompt
    
    prompt = build_script_gen_prompt(
        requirement="test",
        available_tools=["coco"],
        available_roles=["architect"],
        budget_total=2000000,
        # 不指定 budget_tokens 和 orchestrator_agent
    )
    
    # Should use default agent
    assert DEFAULT_ORCHESTRATOR_AGENT in prompt


def test_select_agent_uses_project_root_path_when_different_from_chat_dir():
    """project.root_path != chat.working_dir 场景下，主 Agent 选择回调能找到 pending engine。"""
    handler = _create_mock_handler()

    # chat 工作目录是 /tmp/chat_workspace，但 project 的 root_path 是 /path/to/project
    chat_working_dir = "/tmp/chat_workspace"
    project_root_path = "/path/to/project"

    mock_project = MagicMock()
    mock_project.project_id = "bound_project"
    mock_project.root_path = project_root_path

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_AGENT_SELECT,
        pending=PendingConfirmation(
            requirement="test requirement",
            engine_session_key="session_456",
            initiator_user_id="user_1",
            orchestrator_agent=DEFAULT_ORCHESTRATOR_AGENT,
        ),
    )

    # manager.get 必须根据传入的 root_path 返回不同结果：
    # - 使用 chat_working_dir 时 -> None（找不到）
    # - 使用 project_root_path 时 -> mock_engine（正确找到）
    def _manager_get_side_effect(chat_id: str, root_path: str):
        if root_path == project_root_path:
            return mock_engine
        return None

    handler.ctx.workflow_engine_manager.get = MagicMock(side_effect=_manager_get_side_effect)
    handler._get_root_path = MagicMock(return_value=chat_working_dir)

    # _resolve_project_from_id 通过 ctx.project_manager.get_project_for_chat 实现
    handler.ctx.project_manager.get_project_for_chat = MagicMock(return_value=mock_project)

    with patch("src.thread.get_current_sender_id", return_value="user_1"):
        handler.handle_workflow_select_agent(
            message_id="msg_456",
            chat_id="test_chat",
            project_id="bound_project",
            value={
                "action": "workflow_select_agent",
                "agent_type": "coco",
                "engine_session_key": "session_456",
                "project_id": "bound_project",
            },
        )

    # 验证：必须用 project_root_path 去查找 engine（若用 chat 工作目录则会返回 session_expired）
    call_args_list = handler.ctx.workflow_engine_manager.get.call_args_list
    assert any(
        call[0][1] == project_root_path for call in call_args_list
    ), f"engine 查找未使用 project.root_path：{call_args_list}"

    # 验证：成功存储选中的 agent 并进入工具选择阶段
    assert mock_engine.project.pending.orchestrator_agent == "coco"
    handler._show_tool_selection_card.assert_called_once()
    # 验证传递给工具选择阶段的 project 不是 None，且 root_path 正确
    call_args = handler._show_tool_selection_card.call_args
    assert call_args.kwargs.get("project") is not None or (
        len(call_args.args) >= 4 and call_args.args[3] is not None
    ), "调用 _show_tool_selection_card 时 project 应为解析后的 project 对象"


def test_select_agent_falls_back_to_chat_dir_when_no_project_id():
    """当按钮 value 中没有 project_id（旧卡片兼容）时，应回退到 chat 工作目录。"""
    handler = _create_mock_handler()

    chat_working_dir = "/tmp/chat_workspace"

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_AGENT_SELECT,
        pending=PendingConfirmation(
            requirement="test requirement",
            engine_session_key="session_789",
            initiator_user_id="user_2",
            orchestrator_agent=DEFAULT_ORCHESTRATOR_AGENT,
        ),
    )

    # 没有 project_id 时：只在 chat 工作目录能找到 engine
    def _manager_get_side_effect(chat_id: str, root_path: str):
        if root_path == chat_working_dir:
            return mock_engine
        return None

    handler.ctx.workflow_engine_manager.get = MagicMock(side_effect=_manager_get_side_effect)
    handler._get_root_path = MagicMock(return_value=chat_working_dir)

    with patch("src.thread.get_current_sender_id", return_value="user_2"):
        handler.handle_workflow_select_agent(
            message_id="msg_789",
            chat_id="test_chat",
            project_id=None,
            value={
                "action": "workflow_select_agent",
                "agent_type": "claude",
                "engine_session_key": "session_789",
                # 故意不提供 project_id（旧卡片）
            },
        )

    # 旧卡片路径仍可正常流转
    assert mock_engine.project.pending.orchestrator_agent == "claude"
    handler._show_tool_selection_card.assert_called_once()


def test_select_agent_missing_engine_returns_session_expired():
    """若找不到任何 pending engine，应返回 session_expired，不得抛出异常。"""
    handler = _create_mock_handler()

    # manager.get 在任何路径都找不到 engine
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=None)

    with patch("src.thread.get_current_sender_id", return_value="user_3"):
        handler.handle_workflow_select_agent(
            message_id="msg_expired",
            chat_id="test_chat",
            project_id="bound_project",
            value={
                "action": "workflow_select_agent",
                "agent_type": "claude",
                "engine_session_key": "session_xxx",
                "project_id": "bound_project",
            },
        )

    handler._reply_workflow_error.assert_called_once()
    call_args = handler._reply_workflow_error.call_args
    assert call_args[0][1] == "session_expired"


def test_handle_workflow_select_agent_forged_agent_type():
    """伪造回调中使用非法 agent_type 时应返回 invalid_argument，且不创建 session / 不进入工具选择。"""
    handler = _create_mock_handler()

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_AGENT_SELECT,
        pending=PendingConfirmation(
            requirement="test requirement",
            engine_session_key="session_forge",
            initiator_user_id="test_user",
            orchestrator_agent=DEFAULT_ORCHESTRATOR_AGENT,
        ),
    )
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=mock_engine)

    with patch("src.thread.get_current_sender_id", return_value="test_user"):
        handler.handle_workflow_select_agent(
            message_id="msg_forged",
            chat_id="test_chat",
            project_id="test_proj",
            value={
                "action": "workflow_select_agent",
                "agent_type": "FAKE_XXX",
                "engine_session_key": "session_forge",
                "project_id": "test_proj",
            },
        )

    # 必须返回 invalid_argument 类别错误
    handler._reply_workflow_error.assert_called_once()
    call_args = handler._reply_workflow_error.call_args
    assert call_args[0][1] == "invalid_argument", f"expected invalid_argument, got {call_args[0][1]}"
    detail = call_args.kwargs.get("detail", "") or (
        call_args[0][2] if len(call_args[0]) > 2 else ""
    )
    assert "FAKE_XXX" in str(detail), f"detail 应包含非法 agent_type，实际: {detail}"

    # 不得修改 pending state，不得进入工具选择流程
    assert mock_engine.project.pending.orchestrator_agent == DEFAULT_ORCHESTRATOR_AGENT
    assert mock_engine.project.status == WorkflowStatus.AWAITING_AGENT_SELECT
    handler._show_tool_selection_card.assert_not_called()


def _build_handler_for_regen():
    """Helper: build a WorkflowHandler that can call _generate_and_show_confirm_card
    or handle_workflow_regenerate_script without touching the real filesystem."""
    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = MagicMock()
    handler.reply_text = MagicMock()
    handler.send_card_to_chat = MagicMock()
    handler.update_card = MagicMock()
    handler._reply_workflow_error = MagicMock()
    handler._build_confirm_card = MagicMock(return_value={"header": {"title": {"content": "confirm"}}})
    handler._read_pending_script = MagicMock(return_value="")
    handler._get_root_path = MagicMock(return_value="/tmp/test_proj")
    handler.get_engine_name = MagicMock(return_value="test_engine")
    return handler


def test_regenerate_script_preserves_orchestrator_agent():
    """点击"重新生成脚本"后，pending.orchestrator_agent 必须保持为 claude（不会被重置为 coco）。"""
    handler = _build_handler_for_regen()

    mock_project = MagicMock()
    mock_project.project_id = "test_proj"
    mock_project.root_path = "/tmp/test_proj"

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_CONFIRM,
        pending=PendingConfirmation(
            script_path="/tmp/test_proj/.ghostap/workflow_scripts/generated_workflow.js",
            requirement="test requirement",
            meta={"tools": ["coco"]},
            is_fallback=False,
            initiator_user_id="test_user",
            engine_session_key="session_abc",
            selected_tools=["coco"],
            tools_mismatch=False,
            orchestrator_agent="claude",  # 用户之前选择的 agent
            selected_budget=1500000,
        ),
    )
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=mock_engine)
    handler.ctx.workflow_engine_manager.get_or_create = MagicMock(return_value=mock_engine)
    handler.ctx.project_manager.get_project = MagicMock(return_value=mock_project)

    with patch("src.feishu.handlers.workflow.os.path.exists", return_value=True):
        with patch("src.feishu.handlers.workflow.os.remove"):
            # Stub _generate_script_via_ai — only check the orchestrator that is
            # resolved inside the function.
            captured_agent = {"value": None}

            original_gen = handler._generate_script_via_ai

            def fake_gen(requirement, root_path, selected_tools, engine, **kwargs):
                # Inspect what orchestrator agent the function actually resolves
                # from `engine.project.pending`.
                if engine and engine.project and engine.project.pending:
                    captured_agent["value"] = engine.project.pending.orchestrator_agent
                return ("/tmp/test_proj/.ghostap/workflow_scripts/generated_workflow.js", {"tools": ["coco"]}, False)

            handler._generate_script_via_ai = fake_gen

            with patch("src.thread.get_current_sender_id", return_value="test_user"):
                handler.handle_workflow_regenerate_script(
                    message_id="msg_regen",
                    chat_id="test_chat",
                    project_id="test_proj",
                    value={
                        "action": "workflow_regenerate_script",
                        "engine_session_key": "session_abc",
                        "project_id": "test_proj",
                    },
                )

    # 关键断言：_generate_script_via_ai 中读取到的 orchestrator agent 必须是 claude
    assert captured_agent["value"] == "claude", (
        f"重新生成脚本时 orchestrator agent 被重置了，期望 claude，实际 {captured_agent['value']!r}"
    )
    # 同时 pending 里也必须保留 claude
    assert mock_engine.project.pending.orchestrator_agent == "claude"
    assert mock_engine.project.pending.selected_budget == 1500000
    handler._reply_workflow_error.assert_not_called()


def test_generate_and_show_confirm_card_preserves_orchestrator_agent():
    """_generate_and_show_confirm_card 重建 pending 时，应保留原有的 orchestrator_agent。"""
    handler = _build_handler_for_regen()

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_TOOL_SELECT,
        pending=PendingConfirmation(
            requirement="some requirement",
            initiator_user_id="test_user",
            engine_session_key="session_xyz",
            selected_tools=["coco"],
            orchestrator_agent="claude",
            selected_budget=500000,
        ),
    )
    handler.ctx.workflow_engine_manager.get_or_create = MagicMock(return_value=mock_engine)

    # 让 discover_templates 返回空列表，确保走 AI 路径
    with patch("src.workflow_engine.templates.discover_templates", return_value=[]):
        with patch("src.thread.get_current_sender_id", return_value="test_user"):
            # Stub out _generate_script_via_ai to avoid real AI calls
            def fake_gen(requirement, root_path, selected_tools, engine, **kwargs):
                return ("/tmp/test_proj/.ghostap/workflow_scripts/generated_workflow.js", {"tools": ["coco"]}, False)
            handler._generate_script_via_ai = fake_gen

            handler._generate_and_show_confirm_card(
                message_id="msg",
                chat_id="test_chat",
                requirement="some requirement",
                project=MagicMock(project_id="test_proj"),
                root_path="/tmp/test_proj",
                selected_tools=["coco"],
            )

    assert mock_engine.project.pending is not None
    assert mock_engine.project.pending.orchestrator_agent == "claude", (
        "_generate_and_show_confirm_card 未保留 orchestrator_agent"
    )
    assert mock_engine.project.pending.selected_budget == 500000


def test_generate_and_show_confirm_card_defaults_orchestrator_when_missing():
    """在没有现有 pending 或 orchestrator_agent 为空时，应使用 DEFAULT_ORCHESTRATOR_AGENT。"""
    handler = _build_handler_for_regen()

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.IDLE,
        pending=None,
    )
    handler.ctx.workflow_engine_manager.get_or_create = MagicMock(return_value=mock_engine)

    with patch("src.workflow_engine.templates.discover_templates", return_value=[]):
        with patch("src.thread.get_current_sender_id", return_value="test_user"):
            def fake_gen(requirement, root_path, selected_tools, engine, **kwargs):
                return ("/tmp/test_proj/.ghostap/workflow_scripts/generated_workflow.js", {"tools": ["coco"]}, False)
            handler._generate_script_via_ai = fake_gen

            handler._generate_and_show_confirm_card(
                message_id="msg",
                chat_id="test_chat",
                requirement="some requirement",
                project=MagicMock(project_id="test_proj"),
                root_path="/tmp/test_proj",
                selected_tools=["coco"],
            )

    assert mock_engine.project.pending.orchestrator_agent == DEFAULT_ORCHESTRATOR_AGENT

