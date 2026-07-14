import pytest

from src.autonomous.journal.frame import JournalEvent
from src.autonomous.membership.models import (
    MembershipEffect,
    MembershipEffectState,
    MembershipOperation,
    MembershipState,
    membership_effect_id,
)
from src.autonomous.membership.projection import (
    MembershipProjectionError,
    MembershipProjectionState,
    reduce_membership_frame,
)
from tests.autonomous.workforce_helpers import make_writer


def _effect(operation: MembershipOperation = MembershipOperation.ADD) -> MembershipEffect:
    return MembershipEffect(
        schema_version=1,
        effect_id=membership_effect_id(
            "tenant_1", "oc_team", "agt_1", operation, 1
        ),
        tenant_key="tenant_1",
        chat_id="oc_team",
        agent_id="agt_1",
        app_id="cli_1",
        requester_principal_id="ou_admin",
        operation=operation,
        state=MembershipEffectState.PREPARED,
        membership_epoch=1,
        error_code="",
    )


def _commit(writer, *events: JournalEvent):
    return writer.commit(
        events,
        writer.get_aggregate_versions({event.aggregate_id for event in events}),
    ).frame


def test_add_effect_replays_to_active_only_with_atomic_workforce_change(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = MembershipProjectionState()
    effect = _effect()

    reduce_membership_frame(
        state,
        _commit(
            writer,
            JournalEvent(
                event_type="employee.membership.effect_prepared",
                aggregate_id=effect.effect_id,
                payload={"effect": effect.to_dict()},
            ),
        ),
    )
    assert state.records[("tenant_1", "oc_team", "agt_1")].state is MembershipState.ADDING

    reduce_membership_frame(
        state,
        _commit(
            writer,
            JournalEvent(
                event_type="employee.membership.effect_executing",
                aggregate_id=effect.effect_id,
                payload={"effect_id": effect.effect_id},
            ),
        ),
    )
    committed = _commit(
        writer,
        JournalEvent(
            event_type="employee.membership.effect_committed",
            aggregate_id=effect.effect_id,
            payload={"effect_id": effect.effect_id, "observed_is_member": True},
        ),
        JournalEvent(
            event_type="employee.membership_changed",
            aggregate_id="agt_1",
            payload={"member_groups": ["oc_team"]},
        ),
    )
    reduce_membership_frame(state, committed)

    record = state.records[("tenant_1", "oc_team", "agt_1")]
    assert record.state is MembershipState.ACTIVE
    assert record.membership_epoch == 1
    assert state.effects[effect.effect_id].state is MembershipEffectState.COMMITTED
    reduce_membership_frame(state, committed)
    assert state.records[("tenant_1", "oc_team", "agt_1")] == record


def test_commit_without_matching_membership_change_is_rejected(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = MembershipProjectionState()
    effect = _effect()
    for event_type, payload in (
        ("employee.membership.effect_prepared", {"effect": effect.to_dict()}),
        ("employee.membership.effect_executing", {"effect_id": effect.effect_id}),
    ):
        reduce_membership_frame(
            state,
            _commit(
                writer,
                JournalEvent(event_type=event_type, aggregate_id=effect.effect_id, payload=payload),
            ),
        )

    with pytest.raises(MembershipProjectionError, match="membership_changed"):
        reduce_membership_frame(
            state,
            _commit(
                writer,
                JournalEvent(
                    event_type="employee.membership.effect_committed",
                    aggregate_id=effect.effect_id,
                    payload={"effect_id": effect.effect_id, "observed_is_member": True},
                ),
            ),
        )


def test_unknown_remote_result_becomes_degraded_and_terminal(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = MembershipProjectionState()
    effect = _effect(MembershipOperation.REMOVE)
    for event_type, payload in (
        ("employee.membership.effect_prepared", {"effect": effect.to_dict()}),
        ("employee.membership.effect_executing", {"effect_id": effect.effect_id}),
        (
            "employee.membership.effect_action_required",
            {"effect_id": effect.effect_id, "error_code": "remote_unknown"},
        ),
    ):
        reduce_membership_frame(
            state,
            _commit(
                writer,
                JournalEvent(event_type=event_type, aggregate_id=effect.effect_id, payload=payload),
            ),
        )

    record = state.records[("tenant_1", "oc_team", "agt_1")]
    assert record.state is MembershipState.DEGRADED
    assert state.effects[effect.effect_id].state is MembershipEffectState.ACTION_REQUIRED
    assert state.effects[effect.effect_id].error_code == "remote_unknown"


def test_out_of_order_transition_is_rejected(tmp_path) -> None:
    writer = make_writer(tmp_path)
    state = MembershipProjectionState()
    effect = _effect()

    with pytest.raises(MembershipProjectionError, match="prepared"):
        reduce_membership_frame(
            state,
            _commit(
                writer,
                JournalEvent(
                    event_type="employee.membership.effect_executing",
                    aggregate_id=effect.effect_id,
                    payload={"effect_id": effect.effect_id},
                ),
            ),
        )
