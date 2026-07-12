from __future__ import annotations

import pytest

from src.autonomous.journal.frame import JournalEvent
from src.autonomous.workforce.registry import ProjectedAgentRegistry
from tests.autonomous.workforce_helpers import commit_events, seed_workforce_state


def test_projected_registry_is_global_and_returns_fresh_slock_view(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    registry = ProjectedAgentRegistry(state)

    employee = registry.get("tenant_1", "agt_1")
    first = registry.as_slock_identity("tenant_1", "agt_1")
    second = registry.as_slock_identity("tenant_1", "agt_1")

    assert employee is not None
    assert first is not None
    assert second is not None
    assert first is not second
    assert first.agent_id == employee.agent_id
    assert first.name == employee.name
    assert first.agent_type == employee.tool
    assert first.model_name == employee.model
    assert first.system_prompt == employee.persona
    assert not hasattr(first, "app_id")
    assert not hasattr(first, "credential_ref")


def test_membership_filter_does_not_change_global_identity(tmp_path) -> None:
    writer, state = seed_workforce_state(tmp_path)
    registry = ProjectedAgentRegistry(state)

    assert [
        employee.agent_id
        for employee in registry.list_agents("tenant_1", "oc_team")
    ] == ["agt_1"]
    assert registry.list_agents("tenant_1", "oc_other") == []
    assert registry.get("tenant_1", "agt_1") is not None

    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.membership_changed",
            aggregate_id="agt_1",
            payload={"member_groups": ["oc_other"]},
        ),
    )
    assert registry.get("tenant_1", "agt_1") is not None
    assert [
        employee.agent_id
        for employee in registry.list_agents("tenant_1", "oc_other")
    ] == ["agt_1"]


def test_projected_registry_is_tenant_scoped_and_read_only(tmp_path) -> None:
    _, state = seed_workforce_state(tmp_path)
    registry = ProjectedAgentRegistry(state)

    assert registry.get("tenant_2", "agt_1") is None
    assert registry.find_by_name("tenant_2", "Atlas") is None
    assert not hasattr(registry, "register")
    assert not hasattr(registry, "remove")


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get", ("agt_1",)),
        ("find_by_name", ("Atlas",)),
        ("list_agents", ()),
        ("as_slock_identity", ("agt_1",)),
    ],
)
def test_projected_registry_rejects_empty_tenant(
    tmp_path,
    method: str,
    args: tuple[str, ...],
) -> None:
    _, state = seed_workforce_state(tmp_path)
    registry = ProjectedAgentRegistry(state)

    with pytest.raises(ValueError, match="tenant_key"):
        getattr(registry, method)("", *args)


def test_archived_employee_is_hidden_but_tombstone_is_retained(tmp_path) -> None:
    writer, state = seed_workforce_state(tmp_path)
    registry = ProjectedAgentRegistry(state)
    commit_events(
        writer,
        state,
        JournalEvent(
            event_type="employee.state_changed",
            aggregate_id="agt_1",
            payload={"state": "archived"},
        ),
    )

    assert "agt_1" in state.employees
    assert registry.get("tenant_1", "agt_1") is None
    assert registry.find_by_name("tenant_1", "Atlas") is None
    assert registry.list_agents("tenant_1") == []
    assert registry.as_slock_identity("tenant_1", "agt_1") is None
