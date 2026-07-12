from dataclasses import FrozenInstanceError, replace

import pytest

from src.autonomous.domain import (
    BotPrincipal,
    EmployeeDefinition,
    EmployeeIdOrigin,
    EmployeeState,
    WorkerType,
)


def test_employee_round_trip_uses_canonical_agent_id_and_full_identity() -> None:
    employee = EmployeeDefinition(
        agent_id="agt_01",
        tenant_key="tenant_1",
        owner_principal_id="ou_admin",
        name="Atlas",
        emoji="🧭",
        tool="codex",
        model="gpt-5.6-sol",
        profile="standard",
        effort="high",
        role="coder",
        persona="Production backend engineer",
        personality_traits=("precise", "skeptical"),
        permissions=("file_read", "shell"),
        member_groups=("oc_team",),
        worker_type=WorkerType.VISIBLE,
        state=EmployeeState.READY_PENDING_VERIFICATION,
        id_origin=EmployeeIdOrigin.NATIVE,
        aggregate_version=3,
    )

    payload = employee.to_dict()

    assert payload["agent_id"] == "agt_01"
    assert "employee_id" not in payload
    assert EmployeeDefinition.from_dict(payload) == employee
    assert employee.employee_id == employee.agent_id


def test_legacy_employee_id_is_only_an_input_alias() -> None:
    restored = EmployeeDefinition.from_dict(
        {
            "employee_id": "agt_migrated",
            "tenant_key": "tenant_1",
            "owner_principal_id": "ou_admin",
            "name": "Legacy",
            "id_origin": "legacy_alias",
            "legacy_id_alias": "codex:default:Legacy",
        }
    )
    assert restored.agent_id == "agt_migrated"
    assert restored.to_dict()["legacy_id_alias"] == "codex:default:Legacy"


def test_employee_domain_is_frozen_and_transitions_use_replace() -> None:
    employee = EmployeeDefinition(agent_id="agt_1")
    with pytest.raises(FrozenInstanceError):
        employee.state = EmployeeState.ACTIVE  # type: ignore[misc]
    assert replace(employee, state=EmployeeState.ACTIVE).state is EmployeeState.ACTIVE


def test_visible_employee_requires_tenant_and_owner() -> None:
    with pytest.raises(ValueError, match="tenant_key"):
        EmployeeDefinition(name="Atlas", worker_type=WorkerType.VISIBLE)


def test_bot_principal_never_serializes_secret() -> None:
    principal = BotPrincipal(
        bot_principal_id="bot_1",
        tenant_key="tenant_1",
        agent_id="agt_1",
        app_id="cli_1",
        credential_ref="cred_1",
        desired_manifest_hash="sha256:desired",
        observed_manifest_hash="sha256:observed",
    )
    payload = principal.to_dict()
    assert payload["credential_ref"] == "cred_1"
    assert "app_secret" not in payload
    assert BotPrincipal.from_dict(payload) == principal


def test_bot_principal_direct_construction_requires_agent_binding() -> None:
    with pytest.raises(ValueError, match="agent_id"):
        BotPrincipal(bot_principal_id="bot_1", agent_id="")


@pytest.mark.parametrize(
    "binding",
    [
        {},
        {"agent_id": ""},
        {"employee_id": ""},
    ],
)
def test_bot_principal_deserialization_rejects_missing_or_empty_binding(
    binding: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="agent_id"):
        BotPrincipal.from_dict({"bot_principal_id": "bot_1", **binding})


def test_bot_principal_accepts_legacy_employee_id_binding() -> None:
    principal = BotPrincipal.from_dict(
        {
            "bot_principal_id": "bot_1",
            "employee_id": "agt_legacy",
        }
    )

    assert principal.agent_id == "agt_legacy"
    assert principal.to_dict()["agent_id"] == "agt_legacy"
