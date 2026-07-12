from __future__ import annotations

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
