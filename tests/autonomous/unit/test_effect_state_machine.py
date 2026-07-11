"""Unit tests for Effect state machine transitions."""

from __future__ import annotations

import pytest

from src.autonomous.domain.effects import Effect, EffectState
from src.autonomous.domain.enums import EffectEvent, RiskLevel
from src.autonomous.domain.state_machine import (
    TransitionRejected,
    transition_effect,
)


def _make_effect(state: EffectState = EffectState.PROPOSED, **kw: object) -> Effect:
    defaults = dict(
        effect_instance_id="efx_test_1",
        capability="cap_send",
        risk_level=RiskLevel.R2,
        run_id="run_1",
        attempt_id="att_1",
    )
    defaults.update(kw)
    return Effect(state=state, **defaults)


def test_proposed_to_policy_allowed() -> None:
    effect = _make_effect(EffectState.PROPOSED)
    updated, record, _ = transition_effect(effect, EffectEvent.POLICY_ALLOWED)
    assert updated.state is EffectState.POLICY_ALLOWED


def test_proposed_to_policy_denied() -> None:
    effect = _make_effect(EffectState.PROPOSED)
    updated, record, _ = transition_effect(effect, EffectEvent.POLICY_DENIED)
    assert updated.state is EffectState.POLICY_DENIED


def test_policy_allowed_to_prepared() -> None:
    effect = _make_effect(EffectState.POLICY_ALLOWED)
    updated, record, _ = transition_effect(effect, EffectEvent.PREPARED)
    assert updated.state is EffectState.PREPARED


def test_prepared_to_executing() -> None:
    effect = _make_effect(EffectState.PREPARED)
    updated, record, _ = transition_effect(effect, EffectEvent.DISPATCH_STARTED)
    assert updated.state is EffectState.EXECUTING
    assert updated.active_dispatch is True


def test_executing_to_committed() -> None:
    effect = _make_effect(EffectState.EXECUTING, active_dispatch=True)
    updated, record, _ = transition_effect(effect, EffectEvent.DISPATCH_COMMITTED)
    assert updated.state is EffectState.COMMITTED
    assert updated.active_dispatch is False
    assert updated.committed_at is not None


def test_executing_to_unknown() -> None:
    effect = _make_effect(EffectState.EXECUTING, active_dispatch=True)
    updated, record, _ = transition_effect(effect, EffectEvent.DISPATCH_UNKNOWN)
    assert updated.state is EffectState.UNKNOWN_EFFECT
    assert updated.active_dispatch is False


def test_executing_to_failed_safe() -> None:
    effect = _make_effect(EffectState.EXECUTING, active_dispatch=True)
    updated, record, _ = transition_effect(effect, EffectEvent.DISPATCH_FAILED_SAFE)
    assert updated.state is EffectState.FAILED_SAFE
    assert updated.active_dispatch is False


def test_unknown_to_reconciling() -> None:
    effect = _make_effect(EffectState.UNKNOWN_EFFECT)
    updated, record, _ = transition_effect(effect, EffectEvent.RECONCILE_STARTED)
    assert updated.state is EffectState.RECONCILING


def test_reconciling_to_committed_via_remote() -> None:
    effect = _make_effect(EffectState.RECONCILING)
    updated, record, _ = transition_effect(effect, EffectEvent.REMOTE_COMMITTED)
    assert updated.state is EffectState.COMMITTED


def test_reconciling_to_failed_safe() -> None:
    effect = _make_effect(EffectState.RECONCILING)
    updated, record, _ = transition_effect(effect, EffectEvent.REMOTE_NOT_EXECUTED)
    assert updated.state is EffectState.FAILED_SAFE


def test_reconciling_to_retry_authorized() -> None:
    effect = _make_effect(EffectState.RECONCILING)
    updated, record, _ = transition_effect(effect, EffectEvent.RETRY_AUTHORIZED)
    assert updated.state is EffectState.RETRY_AUTHORIZED


def test_retry_authorized_to_prepared() -> None:
    effect = _make_effect(EffectState.RETRY_AUTHORIZED)
    updated, record, _ = transition_effect(effect, EffectEvent.PREPARED)
    assert updated.state is EffectState.PREPARED


def test_committed_to_compensating() -> None:
    effect = _make_effect(EffectState.COMMITTED)
    updated, record, _ = transition_effect(effect, EffectEvent.COMPENSATE_STARTED)
    assert updated.state is EffectState.COMPENSATING


def test_compensating_to_compensated() -> None:
    effect = _make_effect(EffectState.COMPENSATING)
    updated, record, _ = transition_effect(effect, EffectEvent.COMPENSATED)
    assert updated.state is EffectState.COMPENSATED


def test_compensating_to_compensation_failed() -> None:
    effect = _make_effect(EffectState.COMPENSATING)
    updated, record, _ = transition_effect(effect, EffectEvent.COMPENSATION_FAILED)
    assert updated.state is EffectState.COMPENSATION_FAILED


def test_manual_reconciliation_abandoned() -> None:
    effect = _make_effect(EffectState.MANUAL_RECONCILIATION)
    updated, record, _ = transition_effect(effect, EffectEvent.ABANDONED_ACCEPTED)
    assert updated.state is EffectState.ABANDONED_ACCEPTED


def test_illegal_transition_rejected() -> None:
    effect = _make_effect(EffectState.COMMITTED)
    with pytest.raises(TransitionRejected):
        transition_effect(effect, EffectEvent.POLICY_ALLOWED)


def test_terminal_states_cannot_transition() -> None:
    for terminal in (
        EffectState.FAILED_SAFE,
        EffectState.ABORTED_NO_DISPATCH,
        EffectState.POLICY_DENIED,
        EffectState.COMPENSATED,
        EffectState.ABANDONED_ACCEPTED,
    ):
        effect = _make_effect(terminal)
        with pytest.raises(TransitionRejected):
            transition_effect(effect, EffectEvent.DISPATCH_STARTED)


def test_version_increments_on_transition() -> None:
    effect = _make_effect(EffectState.PROPOSED, aggregate_version=5)
    updated, _, _ = transition_effect(effect, EffectEvent.POLICY_ALLOWED)
    assert updated.aggregate_version == 6


def test_version_check_rejects_mismatch() -> None:
    effect = _make_effect(EffectState.PROPOSED, aggregate_version=3)
    with pytest.raises(TransitionRejected, match="version"):
        transition_effect(
            effect, EffectEvent.POLICY_ALLOWED, expected_aggregate_version=2
        )


def test_abort_before_dispatch_from_prepared() -> None:
    effect = _make_effect(EffectState.PREPARED)
    updated, _, _ = transition_effect(effect, EffectEvent.ABORT_BEFORE_DISPATCH)
    assert updated.state is EffectState.ABORTED_NO_DISPATCH
