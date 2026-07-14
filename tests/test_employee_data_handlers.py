from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.feishu.handlers.slock import SlockHandler
from src.thread import (
    set_current_is_p2p,
    set_current_sender_id,
    set_current_tenant_key,
    set_current_thread_id,
)


def _handler(*, employee, data):
    handler = object.__new__(SlockHandler)
    hire = MagicMock()
    hire.synchronize_projection.return_value = SimpleNamespace(
        employees={employee.agent_id: employee}
    )
    handler.ctx = SimpleNamespace(
        employee_hire_service=hire,
        employee_data_composition=data,
        settings=SimpleNamespace(
            app_id="main_bot",
            admin_user_ids=frozenset({"ou_admin"}),
        ),
    )
    handler.reply_text = MagicMock()
    handler.reply_card = MagicMock()
    handler._get_engine_manager = MagicMock(
        side_effect=AssertionError("canonical read fell back to legacy Slock")
    )
    return handler


def test_main_bot_admin_dm_memory_uses_authoritative_port() -> None:
    employee = SimpleNamespace(
        agent_id="agt_alpha",
        name="Alpha",
        tenant_key="tenant_1",
    )
    memory_query = MagicMock()
    memory_query.query.return_value = SimpleNamespace(content="# canonical memory")
    handler = _handler(
        employee=employee,
        data=SimpleNamespace(memory_query=memory_query),
    )
    set_current_sender_id("ou_admin")
    set_current_tenant_key("tenant_1")
    set_current_is_p2p(True)
    set_current_thread_id(None)
    try:
        handler.show_agent_memory("om_1", "oc_dm", "Alpha")
    finally:
        set_current_sender_id(None)
        set_current_tenant_key(None)
        set_current_is_p2p(False)
        set_current_thread_id(None)

    request, spec = memory_query.query.call_args.args
    assert request.principal_id == "ou_admin"
    assert request.tenant_key == "tenant_1"
    assert request.receiving_bot_app_id == "main_bot"
    assert request.chat_type == "p2p"
    assert spec.agent_id == "agt_alpha"
    assert spec.full_l1 is True
    handler.reply_card.assert_called_once()


def test_main_bot_non_admin_history_is_denied_before_read() -> None:
    employee = SimpleNamespace(
        agent_id="agt_alpha",
        name="Alpha",
        tenant_key="tenant_1",
    )
    history_query = MagicMock()
    handler = _handler(
        employee=employee,
        data=SimpleNamespace(query=history_query),
    )
    set_current_sender_id("ou_intruder")
    set_current_tenant_key("tenant_1")
    set_current_is_p2p(True)
    try:
        handler.show_employee_history("om_2", "oc_dm", "Alpha")
    finally:
        set_current_sender_id(None)
        set_current_tenant_key(None)
        set_current_is_p2p(False)

    history_query.query.assert_not_called()
    handler.reply_text.assert_called_once()
