from dataclasses import FrozenInstanceError, replace

import pytest

from src.autonomous.membership.models import (
    MembershipEffect,
    MembershipEffectState,
    MembershipOperation,
    MembershipState,
    membership_effect_id,
)


def _effect(**overrides: object) -> MembershipEffect:
    values: dict[str, object] = {
        "schema_version": 1,
        "effect_id": membership_effect_id(
            "tenant_1", "oc_team", "agt_1", MembershipOperation.ADD, 1
        ),
        "tenant_key": "tenant_1",
        "chat_id": "oc_team",
        "agent_id": "agt_1",
        "app_id": "cli_1",
        "requester_principal_id": "ou_admin",
        "operation": MembershipOperation.ADD,
        "state": MembershipEffectState.PREPARED,
        "membership_epoch": 1,
        "error_code": "",
    }
    values.update(overrides)
    return MembershipEffect(**values)


def test_membership_effect_is_stable_frozen_and_exact_schema() -> None:
    effect = _effect()

    assert MembershipEffect.from_dict(effect.to_dict()) == effect
    assert effect.desired_state is MembershipState.ACTIVE
    with pytest.raises(FrozenInstanceError):
        effect.error_code = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="exact schema"):
        MembershipEffect.from_dict({**effect.to_dict(), "extra": True})
    with pytest.raises(ValueError, match="effect_id"):
        replace(effect, effect_id="membfx_" + "0" * 64)


def test_remove_effect_targets_absent_and_ids_are_coordinate_bound() -> None:
    add = _effect()
    remove_id = membership_effect_id(
        "tenant_1", "oc_team", "agt_1", MembershipOperation.REMOVE, 1
    )
    remove = _effect(
        effect_id=remove_id,
        operation=MembershipOperation.REMOVE,
    )

    assert remove.desired_state is MembershipState.ABSENT
    assert add.effect_id != remove.effect_id
    assert remove.effect_id == membership_effect_id(
        "tenant_1", "oc_team", "agt_1", "remove", 1
    )


@pytest.mark.parametrize(
    ("state", "error_code", "matches"),
    [
        (MembershipEffectState.ACTION_REQUIRED, "", "error_code"),
        (MembershipEffectState.PREPARED, "remote_unknown", "error_code"),
    ],
)
def test_effect_error_code_is_only_present_for_action_required(
    state: MembershipEffectState,
    error_code: str,
    matches: str,
) -> None:
    with pytest.raises(ValueError, match=matches):
        _effect(state=state, error_code=error_code)
