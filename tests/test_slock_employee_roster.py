"""Tests for /employees (roster) command and /role add pick→confirm flow."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

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
    bot_principal_id: str = "bot_test1",
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
        bot_principal_id=bot_principal_id,
    )


def _handler_for_roster(*, hire_service=None, fire_service=None) -> SlockHandler:
    handler = object.__new__(SlockHandler)
    handler.ctx = SimpleNamespace(
        employee_hire_service=hire_service,
        employee_fire_service=fire_service,
        employee_membership_service=None,
        project_manager=MagicMock(),
        settings=SimpleNamespace(admin_user_ids=frozenset({"ou_admin"})),
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

    def test_archived_employees_are_history_not_current_roster(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        archived = _projected_employee(state=EmployeeState.ARCHIVED, member_groups=())
        projection = SimpleNamespace(employees={"agt_test1": archived})
        hire_service = MagicMock()
        hire_service.synchronize_projection.return_value = projection
        handler = _handler_for_roster(hire_service=hire_service)

        handler.list_employees_roster("om_1", "oc_chat")

        handler.reply_card.assert_not_called()
        message = handler.reply_text.call_args.args[1]
        assert "没有在职员工" in message
        assert "历史归档 1 人" in message
        assert "/hire" in message

    def test_admin_dm_shows_app_identity_and_pending_confirmation(self, monkeypatch):
        from src.autonomous.provisioning.fire_state import (
            FireCleanupMode,
            FirePhase,
        )

        monkeypatch.setattr(
            "src.thread.manager.get_current_tenant_key", lambda: "tenant_a"
        )
        monkeypatch.setattr(
            "src.thread.manager.get_current_sender_id", lambda: "ou_admin"
        )
        monkeypatch.setattr(
            "src.thread.manager.get_current_is_p2p", lambda: True
        )
        employee = _projected_employee(
            agent_id="agt_atlas",
            name="Atlas",
            state=EmployeeState.RETIRING,
        )
        projection = SimpleNamespace(
            employees={employee.agent_id: employee},
            bot_principals={
                "bot_test1": SimpleNamespace(
                    tenant_key="tenant_a",
                    agent_id="agt_atlas",
                    app_id="cli_atlas",
                    credential_ref="secret-must-not-render",
                )
            },
        )
        hire_service = MagicMock()
        hire_service.synchronize_projection.return_value = projection
        hire_service.list_states.return_value = ()
        fire_service = MagicMock()
        fire_service.list_states.return_value = (
            SimpleNamespace(
                tenant_key="tenant_a",
                agent_id="agt_atlas",
                phase=FirePhase.ACTION_REQUIRED,
                cleanup_mode=FireCleanupMode.EXTERNAL_UNKNOWN,
                error_code="external_cleanup_authority_unavailable",
                external_disposition_confirmed=False,
                app_id="cli_atlas",
            ),
        )
        handler = _handler_for_roster(
            hire_service=hire_service,
            fire_service=fire_service,
        )

        handler.list_employees_roster("om_1", "oc_dm")

        card = handler.reply_card.call_args.args[1]
        content = card["elements"][0]["text"]["content"]
        assert "agt_atlas" in content
        assert "cli_atlas" in content
        assert (
            "/fire agt_atlas --confirm-app-disposed cli_atlas" in content
        )
        assert "secret-must-not-render" not in content
        assert "bot_test1" not in content

    def test_admin_dm_no_app_confirmation_requires_prior_platform_check(
        self,
        monkeypatch,
    ):
        from src.autonomous.provisioning.fire_state import (
            FireCleanupMode,
            FirePhase,
        )

        monkeypatch.setattr(
            "src.thread.manager.get_current_tenant_key", lambda: "tenant_a"
        )
        monkeypatch.setattr(
            "src.thread.manager.get_current_sender_id", lambda: "ou_admin"
        )
        monkeypatch.setattr(
            "src.thread.manager.get_current_is_p2p", lambda: True
        )
        employee = _projected_employee(
            agent_id="agt_no_app",
            name="Atlas",
            state=EmployeeState.RETIRING,
            bot_principal_id="",
        )
        hire_service = MagicMock()
        hire_service.synchronize_projection.return_value = SimpleNamespace(
            employees={employee.agent_id: employee},
            bot_principals={},
        )
        hire_service.list_states.return_value = ()
        fire_service = MagicMock()
        fire_service.list_states.return_value = (
            SimpleNamespace(
                tenant_key="tenant_a",
                agent_id="agt_no_app",
                phase=FirePhase.ACTION_REQUIRED,
                cleanup_mode=FireCleanupMode.EXTERNAL_UNKNOWN,
                error_code="external_cleanup_authority_unavailable",
                external_disposition_confirmed=False,
                app_id="",
            ),
        )
        handler = _handler_for_roster(
            hire_service=hire_service,
            fire_service=fire_service,
        )

        handler.list_employees_roster("om_1", "oc_dm")

        content = handler.reply_card.call_args.args[1]["elements"][0]["text"][
            "content"
        ]
        assert "请先确认开放平台未创建应用" in content
        assert (
            "/fire agt_no_app --confirm-app-disposed NO_APP_FOUND" in content
        )
        assert "已确认" not in content

    @pytest.mark.parametrize(
        ("sender_id", "is_p2p"),
        (("ou_other", True), ("ou_admin", False)),
    )
    def test_sensitive_roster_details_require_admin_dm(
        self,
        monkeypatch,
        sender_id,
        is_p2p,
    ):
        monkeypatch.setattr(
            "src.thread.manager.get_current_tenant_key", lambda: "tenant_a"
        )
        monkeypatch.setattr(
            "src.thread.manager.get_current_sender_id", lambda: sender_id
        )
        monkeypatch.setattr(
            "src.thread.manager.get_current_is_p2p", lambda: is_p2p
        )
        employee = _projected_employee(
            agent_id="agt_private",
            name="Atlas",
            state=EmployeeState.RETIRING,
        )
        projection = SimpleNamespace(
            employees={employee.agent_id: employee},
            bot_principals={
                "bot_test1": SimpleNamespace(
                    tenant_key="tenant_a",
                    agent_id="agt_private",
                    app_id="cli_private",
                    credential_ref="cred_private",
                )
            },
        )
        hire_service = MagicMock()
        hire_service.synchronize_projection.return_value = projection
        fire_service = MagicMock()
        fire_service.list_states.return_value = ()
        handler = _handler_for_roster(
            hire_service=hire_service,
            fire_service=fire_service,
        )

        handler.list_employees_roster("om_1", "oc_chat")

        content = handler.reply_card.call_args.args[1]["elements"][0]["text"][
            "content"
        ]
        assert "Atlas" in content
        assert "agt_private" not in content
        assert "cli_private" not in content
        assert "--confirm-app-disposed" not in content
        assert "NO_APP_FOUND" not in content
        fire_service.list_states.assert_not_called()


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
    def test_picker_uses_schema2_static_select_without_legacy_action(self, monkeypatch):
        monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")

        service = MagicMock()
        service.list_employees.return_value = [_employee()]
        handler, _ = _handler_for_card(service=service)

        handler.add_role_to_group("om_1", "oc_team")

        handler.reply_card.assert_called_once()
        card = handler.reply_card.call_args.args[1]
        blob = json.dumps(card, ensure_ascii=False)
        assert '"tag": "action"' not in blob
        assert '"tag": "select_static"' in blob

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
        assert '"tag": "action"' not in card_json
        assert '"tag": "column_set"' in card_json
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
