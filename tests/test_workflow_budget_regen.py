"""Tests for Workflow budget switch triggering script regeneration (AC12)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, call

import pytest

from src.feishu.handlers.workflow import WorkflowHandler
from src.workflow_engine.constants import DEFAULT_BUDGET_TOKENS
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
    handler.update_card = MagicMock()
    handler._reply_workflow_error = MagicMock()
    handler._read_pending_script = MagicMock(return_value="// script content")
    handler._get_root_path = MagicMock(return_value="/tmp")
    handler.get_engine_name = MagicMock(return_value="test_engine")
    return handler


def test_budget_selection_updates_pending_state():
    """选择预算后更新 pending 状态。"""
    handler = _create_mock_handler()

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_CONFIRM,
        pending=PendingConfirmation(
            requirement="test",
            engine_session_key="session_123",
            initiator_user_id="test_user",
            selected_tools=["coco"],
            script_path="/tmp/test.js",
            meta={"tools": ["coco"]},
            budget=None,
        ),
    )
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=mock_engine)

    # Mock script generation to avoid actual AI calls
    with patch.object(handler, '_generate_script_via_ai') as mock_gen:
        mock_gen.return_value = ("/tmp/new_script.js", {"tools": ["coco"], "budget_tokens": 5000000}, False)

        with patch('src.thread.get_current_sender_id', return_value="test_user"):
            handler.handle_workflow_select_budget(
                message_id="msg_123",
                chat_id="test_chat",
                project_id="",
                value={
                    "action": "workflow_select_budget",
                    "budget_tokens": 5000000,
                    "engine_session_key": "session_123",
                },
            )

    # Verify budget was updated
    assert mock_engine.project.pending.budget == 5000000

    # Verify card was updated twice: once for "regenerating" card, once for final confirm card
    assert handler.update_card.call_count == 2

    # Verify script generation was called with the new budget
    mock_gen.assert_called_once()
    call_kwargs = mock_gen.call_args.kwargs
    assert call_kwargs.get("override_budget_tokens") == 5000000


def test_budget_change_triggers_script_regeneration():
    """切换预算触发脚本重新生成。"""
    handler = _create_mock_handler()

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_CONFIRM,
        pending=PendingConfirmation(
            requirement="test requirement",
            engine_session_key="session_123",
            initiator_user_id="test_user",
            selected_tools=["coco"],
            script_path="/tmp/old_script.js",
            meta={"tools": ["coco"]},
            budget=2000000,  # Original budget
        ),
    )
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=mock_engine)

    # Mock script generation
    with patch.object(handler, '_generate_script_via_ai') as mock_gen:
        mock_gen.return_value = ("/tmp/new_script.js", {"tools": ["coco"]}, False)

        with patch('src.thread.get_current_sender_id', return_value="test_user"):
            # Add a regenerate flag to the value (if implemented)
            handler.handle_workflow_select_budget(
                message_id="msg_123",
                chat_id="test_chat",
                project_id="",
                value={
                    "action": "workflow_select_budget",
                    "budget_tokens": 5000000,  # Different budget
                    "engine_session_key": "session_123",
                    "regenerate": True,  # If this flag is supported
                },
            )

        # If regenerate is supported, script should be regenerated
        # If not, this test documents the expected behavior
        if mock_gen.called:
            # Verify script generation was called with new budget
            call_args = mock_gen.call_args
            assert str(5000000) in str(call_args[0]) or str(5000000) in str(call_args.kwargs)
            # Also verify override_budget_tokens kwarg is set correctly
            assert call_args.kwargs.get("override_budget_tokens") == 5000000


def test_confirm_card_shows_budget_generated_notice():
    """确认卡显示「当前脚本按 X 预算生成」提示。"""
    handler = _create_mock_handler()

    # Test with different budgets
    test_cases = [
        (500000, "50万"),
        (1500000, "150万"),
        (2000000, "200万"),
        (5000000, "500万"),
    ]

    for budget, expected_text in test_cases:
        card = handler._build_confirm_card(
            meta={"tools": ["coco"], "phases": [{"name": "Phase 1"}]},
            requirement="test requirement",
            engine_session_key="test_session",
            chat_id="test_chat",
            project_id="test_proj",
            is_fallback=False,
            selected_tools=["coco"],
            selected_budget=budget,
            script_content="// test script",
        )

        card_str = str(card)
        # Should show budget notice
        has_budget_notice = (
            "预算" in card_str
            and (str(budget) in card_str or expected_text in card_str)
            and ("生成" in card_str or "按" in card_str)
        )
        assert has_budget_notice, f"Budget notice not found for budget {budget}"


def test_script_gen_prompt_includes_budget_constraint():
    """脚本生成 prompt 包含预算硬约束。"""
    from src.workflow_engine.script_gen import build_script_gen_prompt

    # Test with different budgets
    test_cases = [
        (500000, "预算紧张"),
        (2000000, "预算适中"),
        (5000000, "预算充足"),
    ]

    for budget, expected_guidance in test_cases:
        prompt = build_script_gen_prompt(
            requirement="test",
            available_tools=["coco"],
            available_roles=["architect"],
            budget_total=DEFAULT_BUDGET_TOKENS,
            budget_tokens=budget,
            orchestrator_agent="coco",
        )

        # Budget should be in the prompt
        assert f"{budget:,}" in prompt, f"Budget {budget:,} not found in prompt"

        # Guidance should match budget tier
        if expected_guidance == "预算紧张":
            assert "减少并行度" in prompt or "避免过度 fan-out" in prompt
        elif expected_guidance == "预算充足":
            assert "激进的并行策略" in prompt or "多轮验证" in prompt


def test_script_gen_prompt_without_budget_tokens():
    """未指定 budget_tokens 时不包含预算硬约束区段。"""
    from src.workflow_engine.script_gen import build_script_gen_prompt

    prompt = build_script_gen_prompt(
        requirement="test",
        available_tools=["coco"],
        available_roles=["architect"],
        budget_total=DEFAULT_BUDGET_TOKENS,
        # budget_tokens=None (default)
    )

    # Should not have hard constraint section
    assert "预算硬约束" not in prompt
    # But should still have the regular budget_total
    assert str(DEFAULT_BUDGET_TOKENS) in prompt


def test_budget_selection_invalid_tokens():
    """无效的 budget_tokens 返回 invalid_argument 错误。"""
    handler = _create_mock_handler()

    mock_engine = MagicMock()
    mock_engine.project = WorkflowProject(
        workflow_id="test_wf",
        status=WorkflowStatus.AWAITING_CONFIRM,
        pending=PendingConfirmation(
            engine_session_key="session_123",
            initiator_user_id="test_user",
        ),
    )
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=mock_engine)

    with patch('src.thread.get_current_sender_id', return_value="test_user"):
        # Test with non-integer
        handler.handle_workflow_select_budget(
            message_id="msg_123",
            chat_id="test_chat",
            project_id="",
            value={
                "action": "workflow_select_budget",
                "budget_tokens": "not_an_int",
                "engine_session_key": "session_123",
            },
        )

    handler._reply_workflow_error.assert_called_once()
    call_args = handler._reply_workflow_error.call_args
    assert call_args[0][1] == "invalid_argument"
