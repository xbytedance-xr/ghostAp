from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.autonomous.membership import (
    MembershipAuthorizationError,
    MembershipMutationOutcome,
    MembershipOperation,
    MembershipState,
)
from src.feishu.handlers.slock import SlockHandler


def _employee(*, member_groups: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id="agt_employee",
        name="柳七月",
        emoji="🤖",
        tool="codex",
        member_groups=member_groups,
    )


def _handler(*, service: MagicMock | None = None) -> tuple[SlockHandler, MagicMock, MagicMock]:
    handler = object.__new__(SlockHandler)
    handler.ctx = SimpleNamespace(
        employee_membership_service=service,
        project_manager=MagicMock(),
    )
    handler.reply_text = MagicMock(return_value=True)
    handler.reply_card = MagicMock(return_value=True)
    handler.send_text_to_chat = MagicMock(return_value=True)
    handler._check_slock_permission = MagicMock(return_value=True)

    engine = MagicMock()
    engine.channel = SimpleNamespace(channel_id="oc_team", owner_id="ou_owner")
    manager = MagicMock()
    manager.get_activated_engine.return_value = engine
    handler._get_engine_manager = MagicMock(return_value=manager)
    legacy = MagicMock()
    handler._get_global_registry = MagicMock(return_value=legacy)
    return handler, engine, legacy


def test_role_add_uses_canonical_membership_service(monkeypatch) -> None:
    service = MagicMock()
    service.find_employee_by_name.return_value = _employee()
    service.mutate.return_value = MembershipMutationOutcome(
        state=MembershipState.ACTIVE,
        confirmed=True,
        changed=True,
    )
    handler, engine, legacy = _handler(service=service)
    monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")
    monkeypatch.setattr("src.thread.manager.get_current_sender_id", lambda: "ou_admin")

    handler.add_role_to_group("om_1", "oc_team", "柳七月")

    request = service.mutate.call_args.args[0]
    assert request.operation is MembershipOperation.ADD
    assert request.tenant_key == "tenant_a"
    assert request.requester_principal_id == "ou_admin"
    engine.registry.register.assert_not_called()
    legacy.find_by_name.assert_not_called()
    assert "已加入当前群" in handler.reply_text.call_args.args[1]


def test_role_remove_only_removes_canonical_chat_membership(monkeypatch) -> None:
    service = MagicMock()
    service.find_employee_by_name.return_value = _employee(member_groups=("oc_team", "oc_other"))
    service.mutate.return_value = MembershipMutationOutcome(
        state=MembershipState.ABSENT,
        confirmed=True,
        changed=True,
    )
    handler, engine, legacy = _handler(service=service)
    monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")
    monkeypatch.setattr("src.thread.manager.get_current_sender_id", lambda: "ou_admin")

    handler.remove_role("om_1", "oc_team", "柳七月")

    request = service.mutate.call_args.args[0]
    assert request.operation is MembershipOperation.REMOVE
    engine.registry.remove.assert_not_called()
    legacy.remove.assert_not_called()
    assert "已移出当前群" in handler.reply_text.call_args.args[1]


def test_role_add_checks_permission_before_listing_or_mutation() -> None:
    service = MagicMock()
    handler, _engine, legacy = _handler(service=service)
    handler._check_slock_permission.return_value = False

    handler.add_role_to_group("om_1", "oc_team", "")

    service.list_employees.assert_not_called()
    service.mutate.assert_not_called()
    legacy.list_agents.assert_not_called()


def test_degraded_membership_never_reports_success(monkeypatch) -> None:
    service = MagicMock()
    service.find_employee_by_name.return_value = _employee()
    service.mutate.return_value = MembershipMutationOutcome(
        state=MembershipState.DEGRADED,
        confirmed=False,
        changed=False,
        effect_id="membfx_1",
        error_code="remote_unknown",
    )
    handler, _engine, _legacy = _handler(service=service)
    monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")
    monkeypatch.setattr("src.thread.manager.get_current_sender_id", lambda: "ou_admin")

    handler.add_role_to_group("om_1", "oc_team", "柳七月")

    message = handler.reply_text.call_args.args[1]
    assert "✅" not in message
    assert "无法确认" in message


def test_service_authorization_failure_is_not_rendered_as_success(monkeypatch) -> None:
    service = MagicMock()
    service.find_employee_by_name.return_value = _employee()
    service.mutate.side_effect = MembershipAuthorizationError("denied")
    handler, _engine, _legacy = _handler(service=service)
    monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")
    monkeypatch.setattr("src.thread.manager.get_current_sender_id", lambda: "ou_intruder")

    handler.add_role_to_group("om_1", "oc_team", "柳七月")

    message = handler.reply_text.call_args.args[1]
    assert "✅" not in message
    assert "权限不足" in message


def test_employee_picker_callback_uses_projected_agent_id(monkeypatch) -> None:
    service = MagicMock()
    service.get_employee.return_value = _employee()
    service.mutate.return_value = MembershipMutationOutcome(
        state=MembershipState.ACTIVE,
        confirmed=True,
        changed=False,
    )
    handler, _engine, _legacy = _handler(service=service)
    monkeypatch.setattr("src.thread.manager.get_current_tenant_key", lambda: "tenant_a")
    monkeypatch.setattr("src.thread.manager.get_current_sender_id", lambda: "ou_admin")

    handler.handle_card_action(
        "om_1",
        "oc_team",
        "slock_role_add_select",
        {"_option": "agt_employee"},
    )

    service.get_employee.assert_called_once_with("tenant_a", "agt_employee")
    assert service.mutate.call_args.args[0].agent_id == "agt_employee"
