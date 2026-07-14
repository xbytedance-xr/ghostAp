from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.autonomous.gateway.models import GatewayExecutionStatus
from tests.autonomous.integration.test_employee_slock_gateway import (
    _binding,
    _real_coordinator_harness,
    _runtime_model,
)


def test_cancel_before_permit_execution_never_calls_slock(tmp_path, monkeypatch) -> None:
    harness = _real_coordinator_harness(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        harness.engine,
        "_run_acp_session",
        lambda *_args, **_kwargs: calls.append("called") or "done",
    )
    prepared = harness.coordinator.prepare_next()
    assert prepared is not None

    outcome = harness.coordinator.request_cancel(
        agent_id=prepared.binding.agent_id,
        chat_id=prepared.binding.chat_id,
        requester_principal_id=prepared.binding.requester_principal_id,
        command_acceptance_id="acc_stop_1",
    )
    finalized = harness.coordinator.execute_prepared(prepared)

    assert outcome.status == "cancel_requested"
    assert finalized.status is GatewayExecutionStatus.CANCELED
    assert calls == []
    harness.close()


def test_terminal_first_stop_does_not_create_second_terminal(tmp_path, monkeypatch) -> None:
    harness = _real_coordinator_harness(tmp_path)
    monkeypatch.setattr(harness.engine, "_run_acp_session", lambda *_args, **_kwargs: "done")
    prepared = harness.coordinator.prepare_next()
    assert prepared is not None
    completed = harness.coordinator.execute_prepared(prepared)
    before = harness.writer.get_last_frame().sequence

    outcome = harness.coordinator.request_cancel(
        agent_id=prepared.binding.agent_id,
        chat_id=prepared.binding.chat_id,
        requester_principal_id=prepared.binding.requester_principal_id,
        command_acceptance_id="acc_stop_2",
    )

    assert completed.status is GatewayExecutionStatus.COMPLETED
    assert outcome.status == "already_terminal"
    assert harness.writer.get_last_frame().sequence == before
    harness.close()


def test_stop_revalidates_original_requester_authority(tmp_path) -> None:
    harness = _real_coordinator_harness(tmp_path)
    prepared = harness.coordinator.prepare_next()
    assert prepared is not None

    outcome = harness.coordinator.request_cancel(
        agent_id=prepared.binding.agent_id,
        chat_id=prepared.binding.chat_id,
        requester_principal_id="ou_intruder",
        command_acceptance_id="acc_stop_3",
    )

    assert outcome.status == "forbidden"
    assert harness.coordinator.state.attempts[prepared.binding.attempt_id].cancel_requested is False
    harness.close()


def test_stop_allows_configured_admin_and_team_owner(tmp_path) -> None:
    for index, requester in enumerate(("ou_admin", "ou_owner")):
        harness = _real_coordinator_harness(tmp_path / str(index))
        prepared = harness.coordinator.prepare_next()
        assert prepared is not None
        harness.coordinator._admin_principal_ids = frozenset({"ou_admin"})
        harness.coordinator._team_owner_resolver = lambda _chat: "ou_owner"

        outcome = harness.coordinator.request_cancel(
            agent_id=prepared.binding.agent_id,
            chat_id=prepared.binding.chat_id,
            requester_principal_id=requester,
            command_acceptance_id=f"acc_stop_authority_{index}",
        )

        assert outcome.status == "cancel_requested"
        harness.close()


def test_gateway_running_cancel_invokes_engine_and_overrides_late_success() -> None:
    from src.autonomous.ingress import dispatch as module
    from src.slock_engine.models import AgentIdentity

    binding = _binding(module)
    entered = threading.Event()
    release = threading.Event()

    class _Engine:
        def __init__(self) -> None:
            self.canceled: list[str] = []

        def run_agent_session(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(2)
            return "late success"

        def cancel_employee_session(self, agent_id: str) -> bool:
            self.canceled.append(agent_id)
            release.set()
            return True

    engine = _Engine()
    gateway = module.EmployeeSlockGateway()
    permit = gateway.issue_permit(
        binding=binding,
        prompt="budgeted",
        engine=engine,
        agent=AgentIdentity(
            agent_id=binding.agent_id,
            agent_type=binding.tool,
            model_name=_runtime_model(binding),
            model_profile=binding.profile,
            reasoning_effort=binding.effort,
            permissions=list(binding.permissions),
            security_profile="employee_v1",
        ),
        timeout_seconds=30,
        env={"HOME": "/tmp/employee"},
    )
    result = []
    thread = threading.Thread(target=lambda: result.append(gateway.execute_permit(permit)))
    thread.start()
    assert entered.wait(1)

    assert gateway.cancel_attempt(binding) is True
    thread.join(2)

    assert engine.canceled == [binding.agent_id]
    assert result[0].status is GatewayExecutionStatus.CANCELED


def test_runtime_consumes_exact_stop_before_router_admission() -> None:
    from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime

    acceptance_id = "acc_stop_control"
    record = SimpleNamespace(
        disposition=None,
        metadata=SimpleNamespace(
            agent_id="agt_alpha",
            chat_id="oc_team",
            sender_principal_id="ou_requester",
            tenant_key="tenant_1",
            thread_root_message_id="om_root",
        ),
    )
    ingress = MagicMock()
    ingress.state = SimpleNamespace(by_acceptance_id={acceptance_id: record})
    ingress.get_payload.return_value = SimpleNamespace(
        normalized_parts=({"content": {"text": " /stop "}},),
    )
    dispatch = MagicMock()
    dispatch.request_cancel.return_value = SimpleNamespace(status="cancel_requested")
    lifecycle = MagicMock()
    runtime = EmployeeDepartmentRuntime()
    runtime._ingress = ingress
    runtime._dispatch = dispatch
    runtime._outbox_lifecycle = lifecycle
    runtime._drain_employee_outbox_once = MagicMock(return_value=True)

    assert runtime._handle_control_ingress(acceptance_id) is True

    dispatch.request_cancel.assert_called_once()
    lifecycle.command_response.assert_called_once()
    ingress.record_disposition.assert_called_once_with(
        acceptance_id,
        state="terminal",
        reason_code="stop_cancel_requested",
    )


def test_runtime_does_not_consume_non_control_text() -> None:
    from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime

    acceptance_id = "acc_normal"
    ingress = MagicMock()
    ingress.state = SimpleNamespace(
        by_acceptance_id={acceptance_id: SimpleNamespace(disposition=None)}
    )
    ingress.get_payload.return_value = SimpleNamespace(
        normalized_parts=({"content": {"text": "please stop later"}},),
    )
    runtime = EmployeeDepartmentRuntime()
    runtime._ingress = ingress
    runtime._dispatch = MagicMock()
    runtime._outbox_lifecycle = MagicMock()

    assert runtime._handle_control_ingress(acceptance_id) is False
    runtime._dispatch.request_cancel.assert_not_called()


def test_runtime_consumes_durable_membership_event_for_reconciliation() -> None:
    from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime

    acceptance_id = "acc_membership"
    record = SimpleNamespace(
        disposition=None,
        metadata=SimpleNamespace(
            agent_id="agt_alpha",
            chat_id="oc_team",
            tenant_key="tenant_1",
        ),
    )
    ingress = MagicMock()
    ingress.state = SimpleNamespace(by_acceptance_id={acceptance_id: record})
    ingress.get_payload.return_value = SimpleNamespace(
        normalized_parts=({"type": "membership_event", "operation": "added"},),
    )
    membership = MagicMock()
    membership.reconcile_event.return_value = SimpleNamespace(state=SimpleNamespace(value="active"))
    runtime = EmployeeDepartmentRuntime()
    runtime._ingress = ingress
    runtime._membership = membership

    assert runtime._handle_control_ingress(acceptance_id) is True

    membership.reconcile_event.assert_called_once_with(
        tenant_key="tenant_1",
        chat_id="oc_team",
        agent_id="agt_alpha",
    )
    ingress.record_disposition.assert_called_once_with(
        acceptance_id,
        state="terminal",
        reason_code="membership_active",
    )
