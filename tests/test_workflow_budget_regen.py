"""Tests for Workflow budget-switch script regeneration (AC12).

Focuses on ``WorkflowHandler.handle_workflow_apply_budget_regenerate`` which
implements the server-side two-step gate:

1. First click (armed_for_regen is False): set the flag, re-render card, NO AI.
2. Second click (armed_for_regen is True): call ``_generate_script_via_ai``
   with ``override_budget_tokens``, update pending.script_path/meta, reset flag.
3. Security checks fail (session key mismatch, user mismatch, wrong status):
   handler responds via ``_reply_workflow_error``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.feishu.handlers.workflow import WorkflowHandler
from src.workflow_engine.models import (
    PendingConfirmation,
    WorkflowProject,
    WorkflowStatus,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_handler() -> WorkflowHandler:
    handler = WorkflowHandler.__new__(WorkflowHandler)
    handler.ctx = MagicMock()
    handler.reply_card = MagicMock()
    handler.update_card = MagicMock()
    handler._reply_workflow_error = MagicMock()
    handler._read_pending_script = MagicMock(return_value="// script content")
    handler._get_root_path = MagicMock(return_value="/tmp")
    return handler


def _make_engine(pending: PendingConfirmation) -> MagicMock:
    engine = MagicMock()
    engine.project = WorkflowProject(
        workflow_id="wf_1", status=WorkflowStatus.AWAITING_CONFIRM, pending=pending
    )
    return engine


# ---------------------------------------------------------------------------
# Task 5 — direct coverage of handle_workflow_apply_budget_regenerate
# ---------------------------------------------------------------------------


def test_apply_budget_regen_first_click_arms_without_ai():
    """第一次点击：设置 armed_for_regen=True，不调用 AI，重渲染确认卡。"""
    handler = _make_handler()
    pending = PendingConfirmation(
        requirement="regen test",
        engine_session_key="session_abc",
        initiator_user_id="user_0",
        selected_tools=["coco"],
        selected_budget=2000000,
        script_path="/tmp/old.js",
        meta={"tools": ["coco"]},
        armed_for_regen=False,
    )
    engine = _make_engine(pending)
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=engine)

    with patch.object(handler, "_generate_script_via_ai") as mock_gen:
        with patch("src.thread.get_current_sender_id", return_value="user_0"):
            handler.handle_workflow_apply_budget_regenerate(
                message_id="msg_1",
                chat_id="chat_1",
                project_id="",
                value={
                    "action": "workflow_apply_budget_regenerate",
                    "engine_session_key": "session_abc",
                },
            )

        # First click must NOT trigger AI
        mock_gen.assert_not_called()

    # Server flag is now armed
    assert pending.armed_for_regen is True

    # Confirm card was re-rendered so the button label reflects armed state
    handler.update_card.assert_called_once()
    handler._reply_workflow_error.assert_not_called()


def test_apply_budget_regen_second_click_runs_ai_and_resets_flag(tmp_path):
    """第二次点击：调用 AI，更新 pending 字段，重置 armed 标志，并
    同步刷新 pending.script_hash。
    """
    import hashlib

    old_script = tmp_path / "old.js"
    old_script.write_bytes(b"// old\n")
    new_script = tmp_path / "new.js"
    new_script.write_bytes(b"// new\n")
    expected_new_hash = hashlib.sha256(new_script.read_bytes()).hexdigest()

    handler = _make_handler()
    pending = PendingConfirmation(
        requirement="regen round two",
        engine_session_key="session_abc",
        initiator_user_id="user_0",
        selected_tools=["coco"],
        selected_budget=5000000,
        script_path=str(old_script),
        meta={"tools": ["coco"]},
        script_hash=hashlib.sha256(old_script.read_bytes()).hexdigest(),
        armed_for_regen=True,
    )
    engine = _make_engine(pending)
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=engine)

    with patch.object(
        handler,
        "_generate_script_via_ai",
        return_value=(str(new_script), {"tools": ["coco"], "phase_count": 1}, False),
    ) as mock_gen:
        with patch("src.thread.get_current_sender_id", return_value="user_0"):
            handler.handle_workflow_apply_budget_regenerate(
                message_id="msg_1",
                chat_id="chat_1",
                project_id="",
                value={
                    "action": "workflow_apply_budget_regenerate",
                    "engine_session_key": "session_abc",
                },
            )

    # AI was called exactly once with override_budget_tokens set
    mock_gen.assert_called_once()
    call_kwargs = mock_gen.call_args.kwargs
    assert call_kwargs.get("override_budget_tokens") == 5000000

    # Old script cleanup reached os.remove (best-effort, guarded)
    assert not old_script.exists(), "旧脚本文件应已被清理"

    # Server flag is reset after execution (single-token burst protection)
    assert pending.armed_for_regen is False
    assert pending.script_path == str(new_script)
    assert pending.script_hash == expected_new_hash
    assert pending.meta == {"tools": ["coco"], "phase_count": 1}

    # Confirm card was re-rendered to show the new script
    handler.update_card.assert_called_once()
    handler._reply_workflow_error.assert_not_called()


def test_apply_budget_regen_refreshes_script_hash_for_confirm_start(tmp_path):
    """After successful regeneration, ``pending.script_hash`` must reflect
    the new script content so that ``handle_workflow_confirm_start`` can
    still pass its TOCTOU check — otherwise the user sees a spurious
    "脚本被篡改" rejection after legitimately re-generating.
    """
    import hashlib

    # Stage an "old" script file and its recorded hash.
    old_script = tmp_path / "old.js"
    old_script.write_bytes(b"// old\n")
    old_hash = hashlib.sha256(old_script.read_bytes()).hexdigest()

    # Stage the "new" script file that _generate_script_via_ai will return.
    new_script = tmp_path / "new.js"
    new_script.write_bytes(b"// new\n")
    new_hash = hashlib.sha256(new_script.read_bytes()).hexdigest()

    handler = _make_handler()
    pending = PendingConfirmation(
        requirement="hash-sync test",
        engine_session_key="session_abc",
        initiator_user_id="user_0",
        selected_tools=["coco"],
        selected_budget=2000000,
        script_path=str(old_script),
        meta={"tools": ["coco"]},
        script_hash=old_hash,
        armed_for_regen=True,
    )
    engine = _make_engine(pending)
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=engine)
    handler._read_pending_script = MagicMock(return_value="// new")

    with patch.object(
        handler,
        "_generate_script_via_ai",
        return_value=(str(new_script), {"tools": ["coco"], "phase_count": 1}, False),
    ):
        with patch("src.thread.get_current_sender_id", return_value="user_0"):
            handler.handle_workflow_apply_budget_regenerate(
                message_id="msg_1",
                chat_id="chat_1",
                project_id="",
                value={
                    "action": "workflow_apply_budget_regenerate",
                    "engine_session_key": "session_abc",
                },
            )

    # The pending hash must now equal the NEW script hash (not the old one).
    assert pending.script_hash == new_hash
    assert pending.script_hash != old_hash
    assert pending.armed_for_regen is False
    assert pending.script_path == str(new_script)
    # The handler must NOT have surfaced an internal-error during
    # regeneration (e.g. failing to read the new file would be an error).
    handler._reply_workflow_error.assert_not_called()

    # --- Second phase: confirm_start must NOT fail with "脚本被篡改".
    # We don't assert a full execution run — only that the TOCTOU hash
    # comparison passes without triggering an internal-error rejection.
    # Wire validation helpers to focus on hash behaviour.
    with patch.object(
        handler,
        "_inject_workflow_refs_into_script",
        side_effect=lambda content, refs: content,
    ), patch(
        "src.workflow_engine.script_gen.validate_generated_script",
        return_value=(True, []),
    ), patch.object(
        handler, "_submit_engine_task",
    ), patch.object(
        handler, "get_engine_name", return_value="workflow",
    ), patch.object(
        handler, "_build_workflow_callbacks", return_value={},
    ):
        handler.handle_workflow_confirm_start(
            message_id="msg_2",
            chat_id="chat_1",
            project_id="",
            value={
                "action": "workflow_confirm_start",
                "engine_session_key": "session_abc",
            },
        )

    # Expect no internal-error rejection (specifically no "脚本被篡改"
    # detail) and that the task-scheduling path was reached.
    # Collect the detail argument of every internal-error call to ensure
    # we didn't land on the TOCTOU rejection branch.
    error_details = []
    for call in handler._reply_workflow_error.call_args_list:
        if call.args and len(call.args) >= 2 and call.args[1] == "internal_error":
            kw = call.kwargs or {}
            error_details.append(kw.get("detail", ""))
    for detail in error_details:
        assert "被篡改" not in detail, (
            f"confirm_start rejected regenerated script as tampered: {detail}"
        )


def test_apply_budget_regen_rejects_wrong_session_key():
    """session key 不匹配时拒绝。"""
    handler = _make_handler()
    pending = PendingConfirmation(
        requirement="attacker",
        engine_session_key="session_real",
        initiator_user_id="user_0",
        selected_tools=["coco"],
        selected_budget=2000000,
        script_path="/tmp/old.js",
        meta={"tools": ["coco"]},
    )
    engine = _make_engine(pending)
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=engine)

    with patch.object(handler, "_generate_script_via_ai") as mock_gen:
        with patch("src.thread.get_current_sender_id", return_value="user_0"):
            handler.handle_workflow_apply_budget_regenerate(
                message_id="msg_1",
                chat_id="chat_1",
                project_id="",
                value={
                    "action": "workflow_apply_budget_regenerate",
                    "engine_session_key": "session_fake",  # forged
                },
            )

        mock_gen.assert_not_called()

    # Flag must NOT be armed when security gate fails
    assert getattr(pending, "armed_for_regen", False) is False

    handler._reply_workflow_error.assert_called_once()
    # error code should indicate session expiry
    assert handler._reply_workflow_error.call_args[0][1] == "session_expired"
    handler.update_card.assert_not_called()


def test_apply_budget_regen_rejects_wrong_initiator_user():
    """非发起者用户点击时拒绝（权限隔离）。"""
    handler = _make_handler()
    pending = PendingConfirmation(
        requirement="owner-only",
        engine_session_key="session_abc",
        initiator_user_id="user_0",
        selected_tools=["coco"],
        selected_budget=2000000,
        script_path="/tmp/old.js",
        meta={"tools": ["coco"]},
    )
    engine = _make_engine(pending)
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=engine)

    with patch.object(handler, "_generate_script_via_ai") as mock_gen:
        with patch("src.thread.get_current_sender_id", return_value="user_1"):  # wrong user
            handler.handle_workflow_apply_budget_regenerate(
                message_id="msg_1",
                chat_id="chat_1",
                project_id="",
                value={
                    "action": "workflow_apply_budget_regenerate",
                    "engine_session_key": "session_abc",
                },
            )

        mock_gen.assert_not_called()

    assert getattr(pending, "armed_for_regen", False) is False
    handler._reply_workflow_error.assert_called_once()
    assert handler._reply_workflow_error.call_args[0][1] == "forbidden"
    handler.update_card.assert_not_called()


def test_apply_budget_regen_rejects_when_status_not_awaiting_confirm():
    """status 非 AWAITING_CONFIRM 时拒绝（并发状态防护）。"""
    handler = _make_handler()
    pending = PendingConfirmation(
        requirement="running",
        engine_session_key="session_abc",
        initiator_user_id="user_0",
        selected_tools=["coco"],
        selected_budget=2000000,
        script_path="/tmp/old.js",
        meta={"tools": ["coco"]},
    )
    engine = MagicMock()
    engine.project = WorkflowProject(
        workflow_id="wf_1", status=WorkflowStatus.RUNNING, pending=pending
    )
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=engine)

    with patch.object(handler, "_generate_script_via_ai") as mock_gen:
        with patch("src.thread.get_current_sender_id", return_value="user_0"):
            handler.handle_workflow_apply_budget_regenerate(
                message_id="msg_1",
                chat_id="chat_1",
                project_id="",
                value={
                    "action": "workflow_apply_budget_regenerate",
                    "engine_session_key": "session_abc",
                },
            )

        mock_gen.assert_not_called()

    handler._reply_workflow_error.assert_called_once()
    assert handler._reply_workflow_error.call_args[0][1] == "invalid_state"
    handler.update_card.assert_not_called()


def test_apply_budget_regen_strips_forged_payload_fields():
    """filter_workflow_button_value 会剥离伪造字段，防止 client-side 绕过。"""
    from src.card.events.payloads import filter_workflow_button_value

    raw = {
        "action": "workflow_apply_budget_regenerate",
        "engine_session_key": "session_abc",
        "confirmed": True,  # forged client-side field
        "override_budget_tokens": 99999999,  # forged
        "admin": True,  # forged
    }
    cleaned = filter_workflow_button_value(raw)
    assert "confirmed" not in cleaned
    assert "override_budget_tokens" not in cleaned
    assert "admin" not in cleaned
    assert cleaned.get("engine_session_key") == "session_abc"


# ---------------------------------------------------------------------------
# Retained: budget selection still updates pending.budget without AI
# ---------------------------------------------------------------------------


def test_budget_selection_updates_pending_state():
    """预算选择仅更新 pending.selected_budget，不触发 AI 生成。"""
    handler = _make_handler()
    pending = PendingConfirmation(
        requirement="test",
        engine_session_key="session_123",
        initiator_user_id="test_user",
        selected_tools=["coco"],
        script_path="/tmp/test.js",
        meta={"tools": ["coco"]},
        selected_budget=None,
    )
    engine = _make_engine(pending)
    handler.ctx.workflow_engine_manager.get = MagicMock(return_value=engine)

    with patch.object(handler, "_generate_script_via_ai") as mock_gen:
        with patch("src.thread.get_current_sender_id", return_value="test_user"):
            handler.handle_workflow_select_budget(
                message_id="msg_1",
                chat_id="chat_1",
                project_id="",
                value={
                    "action": "workflow_select_budget",
                    "budget_tokens": 5000000,
                    "engine_session_key": "session_123",
                },
            )

    assert pending.selected_budget == 5000000
    assert handler.update_card.call_count == 1
    mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# Retained: PendingConfirmation serialisation round-trip for armed_for_regen
# ---------------------------------------------------------------------------


def test_pending_confirmation_armed_for_regen_roundtrip():
    """JSON dump → reload 后 armed_for_regen 保持一致。"""
    pending = PendingConfirmation(
        requirement="test roundtrip",
        engine_session_key="session_456",
        initiator_user_id="u_0",
        selected_tools=["coco"],
        selected_budget=2000000,
        armed_for_regen=True,
    )

    raw = pending.model_dump_json()
    restored = PendingConfirmation.model_validate_json(raw)

    assert restored.armed_for_regen is True
    assert restored.requirement == "test roundtrip"
    assert restored.selected_budget == 2000000


def test_pending_confirmation_legacy_json_without_armed_for_regen():
    """旧 JSON 缺少 armed_for_regen 字段时默认值为 False。"""
    legacy_payload = {
        "requirement": "legacy workflow",
        "engine_session_key": "session_789",
        "initiator_user_id": "u_1",
        "selected_tools": ["claude"],
        "selected_budget": 1000000,
    }
    restored = PendingConfirmation.model_validate(legacy_payload)
    assert restored.armed_for_regen is False
