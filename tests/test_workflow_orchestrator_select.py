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
