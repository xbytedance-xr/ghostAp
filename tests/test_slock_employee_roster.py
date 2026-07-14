"""Tests for /employees (roster) command and /role add pick→confirm flow."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.autonomous.domain.enums import EmployeeState, WorkerType
from src.feishu.handlers.slock import SlockHandler
from src.slock_engine.slash_commands import (
    SlockCommandAction,
    is_slock_command,
    parse_slock_command,
)

# ---------------------------------------------------------------------------
# Command parsing tests
# ---------------------------------------------------------------------------


class TestEmployeesCommandParsing:
    def test_parse_employees(self):
        cmd = parse_slock_command("/employees")
        assert cmd.action == SlockCommandAction.EMPLOYEE_LIST

    def test_parse_roster_alias(self):
        cmd = parse_slock_command("/roster")
        assert cmd.action == SlockCommandAction.EMPLOYEE_LIST

    def test_is_slock_command_employees_global(self):
        result = is_slock_command("/employees", chat_id=None, manager=None)
        assert result.is_command is True

    def test_is_slock_command_roster_global(self):
        result = is_slock_command("/roster", chat_id=None, manager=None)
        assert result.is_command is True

    def test_is_slock_command_employees_in_dm(self):
        result = is_slock_command("/employees")
        assert result.is_command is True


# ---------------------------------------------------------------------------
# Roster handler tests
# ---------------------------------------------------------------------------


def _projected_employee(
    *,
    agent_id: str = "agt_test1",
    name: str = "柳七月",
    emoji: str = "🤖",
    tool: str = "codex",
    model: str = "gpt-4",
    state: EmployeeState = EmployeeState.ACTIVE,
    tenant_key: str = "tenant_a",
    member_groups: tuple[str, ...] = ("oc_g1",),
) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        name=name,
        emoji=emoji,
        tool=tool,
        model=model,
        state=state,
        worker_type=WorkerType.VISIBLE,
        tenant_key=tenant_key,
        member_groups=member_groups,
    )


def _handler_for_roster(*, hire_service=None) -> SlockHandler:
    handler = object.__new__(SlockHandler)
    handler.ctx = SimpleNamespace(
        employee_hire_service=hire_service,
        employee_membership_service=None,
        project_manager=MagicMock(),
    )
    handler.reply_text = MagicMock(return_value=True)
    handler.reply_card = MagicMock(return_value=True)
    return handler


class TestListEmployeesRoster:
    def test_shows_all_visible_employees_with_state(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        emp_active = _projected_employee(name="柳七月", state=EmployeeState.ACTIVE)
        emp_stuck = _projected_employee(
            agent_id="agt_test2",
            name="南宫婉",
            state=EmployeeState.ACTION_REQUIRED,
            member_groups=(),
        )
        emp_configuring = _projected_employee(
            agent_id="agt_test3",
            name="林黛玉",
            state=EmployeeState.CONFIGURING,
            member_groups=("oc_g1", "oc_g2"),
        )

        projection = SimpleNamespace(employees={
            "agt_test1": emp_active,
            "agt_test2": emp_stuck,
            "agt_test3": emp_configuring,
        })
        hire_service = MagicMock()
        hire_service.synchronize_projection.return_value = projection

        handler = _handler_for_roster(hire_service=hire_service)
        handler.list_employees_roster("om_1", "oc_chat")

        handler.reply_card.assert_called_once()
        card = handler.reply_card.call_args.args[1]
        content = card["elements"][0]["text"]["content"]
        assert "柳七月" in content
        assert "✅ 就绪" in content
        assert "南宫婉" in content
        assert "⚠️ 待处理" in content
        assert "林黛玉" in content
        assert "⏳ 配置中" in content
        assert "群×2" in content

    def test_no_employees_shows_hint(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        projection = SimpleNamespace(employees={})
        hire_service = MagicMock()
        hire_service.synchronize_projection.return_value = projection

        handler = _handler_for_roster(hire_service=hire_service)
        handler.list_employees_roster("om_1", "oc_chat")

        handler.reply_text.assert_called_once()
        assert "/hire" in handler.reply_text.call_args.args[1]

    def test_no_hire_service_shows_fallback(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        handler = _handler_for_roster(hire_service=None)
        handler.list_employees_roster("om_1", "oc_chat")

        handler.reply_text.assert_called_once()
        assert "尚未接入" in handler.reply_text.call_args.args[1]

    def test_no_tenant_key_shows_error(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "")

        handler = _handler_for_roster(hire_service=MagicMock())
        handler.list_employees_roster("om_1", "oc_chat")

        handler.reply_text.assert_called_once()
        assert "租户" in handler.reply_text.call_args.args[1]

    def test_filters_other_tenant(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        emp_other = _projected_employee(tenant_key="tenant_b", name="别人")
        projection = SimpleNamespace(employees={"agt_other": emp_other})
        hire_service = MagicMock()
        hire_service.synchronize_projection.return_value = projection

        handler = _handler_for_roster(hire_service=hire_service)
        handler.list_employees_roster("om_1", "oc_chat")

        handler.reply_text.assert_called_once()
        assert "/hire" in handler.reply_text.call_args.args[1]


# ---------------------------------------------------------------------------
# /role add pick→confirm flow tests
# ---------------------------------------------------------------------------


def _employee(*, agent_id="agt_employee", member_groups=()) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=agent_id,
        name="柳七月",
        emoji="🤖",
        tool="codex",
        member_groups=member_groups,
    )


def _handler_for_card(*, service=None) -> tuple[SlockHandler, MagicMock]:
    handler = object.__new__(SlockHandler)
    handler.ctx = SimpleNamespace(
        employee_membership_service=service,
        project_manager=MagicMock(),
    )
    handler.reply_text = MagicMock(return_value=True)
    handler.reply_card = MagicMock(return_value=True)
    handler.update_card = MagicMock(return_value=True)
    handler.send_text_to_chat = MagicMock(return_value=True)
    handler._check_slock_permission = MagicMock(return_value=True)
    handler._change_employee_membership = MagicMock()

    engine = MagicMock()
    engine.channel = SimpleNamespace(channel_id="oc_team", owner_id="ou_owner")
    manager = MagicMock()
    manager.get_activated_engine.return_value = engine
    handler._get_engine_manager = MagicMock(return_value=manager)
    return handler, service


class TestRoleAddPickConfirm:
    def test_pick_shows_confirm_card_no_mutation(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        service = MagicMock()
        service.get_employee.return_value = _employee()
        handler, _ = _handler_for_card(service=service)

        handler.handle_card_action(
            "om_1", "oc_team", "slock_role_add_pick", {"_option": "agt_employee"}
        )

        handler.update_card.assert_called_once()
        card_json = handler.update_card.call_args.args[1]
        card = json.loads(card_json)
        assert "确认" in card["header"]["title"]["content"]
        assert "柳七月" in card["elements"][0]["text"]["content"]
        handler._change_employee_membership.assert_not_called()

    def test_legacy_select_also_shows_confirm(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        service = MagicMock()
        service.get_employee.return_value = _employee()
        handler, _ = _handler_for_card(service=service)

        handler.handle_card_action(
            "om_1", "oc_team", "slock_role_add_select", {"_option": "agt_employee"}
        )

        handler.update_card.assert_called_once()
        handler._change_employee_membership.assert_not_called()

    def test_confirm_triggers_mutation(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        service = MagicMock()
        service.get_employee.return_value = _employee()
        handler, _ = _handler_for_card(service=service)

        handler.handle_card_action(
            "om_1", "oc_team", "slock_role_add_confirm",
            {"agent_id": "agt_employee", "chat_id": "oc_team"},
        )

        handler._change_employee_membership.assert_called_once()
        call_kwargs = handler._change_employee_membership.call_args.kwargs
        assert call_kwargs["operation"] == "add"

    def test_confirm_with_invalid_employee_shows_error(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        service = MagicMock()
        service.get_employee.return_value = None
        handler, _ = _handler_for_card(service=service)

        handler.handle_card_action(
            "om_1", "oc_team", "slock_role_add_confirm",
            {"agent_id": "agt_nonexistent", "chat_id": "oc_team"},
        )

        handler.send_text_to_chat.assert_called_once()
        assert "失效" in handler.send_text_to_chat.call_args.args[1]
        handler._change_employee_membership.assert_not_called()
