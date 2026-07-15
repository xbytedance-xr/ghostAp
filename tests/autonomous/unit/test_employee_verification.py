from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from src.autonomous.provisioning.verification import (
    ChannelVerificationEvidence,
    SlashVerificationEvidence,
    TenantIngressEvidence,
    VerificationBinding,
    VerificationChallenge,
    VerificationCoordinates,
    VerificationDecision,
    VerificationOutcome,
    VerificationRejection,
    VerificationRouter,
)


class _AtomicNonceLedger:
    """Small faithful implementation of the router's durable atomic port."""

    def __init__(self) -> None:
        self.consumed: set[tuple[str, str]] = set()

    def consume_once(
        self,
        challenge: VerificationChallenge,
        *,
        consumed_at: float,
    ) -> bool:
        key = (challenge.hire_intent_id, challenge.nonce)
        if key in self.consumed:
            return False
        self.consumed.add(key)
        return True


def _binding(**overrides: object) -> VerificationBinding:
    values: dict[str, object] = {
        "hire_intent_id": "hire_1",
        "tenant_key": "tenant_1",
        "app_id": "cli_employee_1",
        "agent_id": "agt_employee_1",
        "generation": 7,
        "requester_principal_id": "ou_admin",
        "requester_union_id": "on_admin",
        "expected_slash_spec_hash": "sha256:desired",
    }
    values.update(overrides)
    return VerificationBinding(**values)  # type: ignore[arg-type]


def _challenge(router: VerificationRouter) -> VerificationChallenge:
    return router.issue_challenge(_binding(), ttl_seconds=60.0)


def _coordinates(challenge: VerificationChallenge) -> VerificationCoordinates:
    return VerificationCoordinates(
        hire_intent_id=challenge.hire_intent_id,
        tenant_key=challenge.tenant_key,
        app_id=challenge.app_id,
        agent_id=challenge.agent_id,
        generation=challenge.generation,
        nonce=challenge.nonce,
    )


def _evidence(
    challenge: VerificationChallenge,
) -> tuple[
    SlashVerificationEvidence,
    ChannelVerificationEvidence,
    TenantIngressEvidence,
]:
    coordinates = _coordinates(challenge)
    return (
        SlashVerificationEvidence(
            coordinates=coordinates,
            desired_spec_hash="sha256:desired",
            observed_spec_hash="sha256:desired",
            reconciled=True,
            verified_at=98.0,
        ),
        ChannelVerificationEvidence(
            coordinates=coordinates,
            identity_app_id="cli_employee_1",
            connection_id="conn_1",
            ready=True,
            verified_at=99.0,
        ),
        TenantIngressEvidence(
            coordinates=coordinates,
            event_id="evt_1",
            message_id="om_1",
            sender_principal_id="ou_employee_app_admin",
            sender_union_id="on_admin",
            command="/status",
            is_p2p=True,
            reply_succeeded=True,
            reply_app_id="cli_employee_1",
            employee_send_request_id="send_1",
            main_bot_send_count=0,
            received_at=107.0,
        ),
    )


def _router(ledger: _AtomicNonceLedger | None = None) -> VerificationRouter:
    return VerificationRouter(
        nonce_consumer=ledger or _AtomicNonceLedger(),
        clock=lambda: 100.0,
        nonce_factory=lambda: "nonce_0123456789abcdef0123456789abcdef",
    )


def test_issue_challenge_binds_tenant_app_agent_generation_nonce_and_ttl() -> None:
    router = _router()

    challenge = router.issue_challenge(_binding(), ttl_seconds=60.0)

    assert challenge.hire_intent_id == "hire_1"
    assert challenge.tenant_key == "tenant_1"
    assert challenge.app_id == "cli_employee_1"
    assert challenge.agent_id == "agt_employee_1"
    assert challenge.generation == 7
    assert challenge.nonce == "nonce_0123456789abcdef0123456789abcdef"
    assert challenge.issued_at == 100.0
    assert challenge.expires_at == 160.0
    assert challenge.nonce not in repr(challenge)
    with pytest.raises(FrozenInstanceError):
        challenge.generation = 8  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_key": ""},
        {"app_id": ""},
        {"agent_id": ""},
        {"generation": 0},
        {"requester_principal_id": ""},
        {"requester_union_id": ""},
        {"expected_slash_spec_hash": ""},
    ],
)
def test_issue_challenge_rejects_incomplete_binding(
    overrides: dict[str, object],
) -> None:
    router = _router()

    with pytest.raises(ValueError):
        router.issue_challenge(_binding(**overrides))


def test_default_deny_requires_all_three_evidence_classes() -> None:
    ledger = _AtomicNonceLedger()
    router = _router(ledger)
    challenge = _challenge(router)

    decision = router.evaluate(challenge, current_generation=7, now=110.0)

    assert decision.outcome is VerificationOutcome.REJECTED
    assert decision.rejection is VerificationRejection.MISSING_EVIDENCE
    assert decision.activation_evidence is None
    assert ledger.consumed == set()


def test_ready_requires_exact_slash_channel_and_real_tenant_ingress_evidence() -> None:
    ledger = _AtomicNonceLedger()
    router = _router(ledger)
    challenge = _challenge(router)
    slash, channel, ingress = _evidence(challenge)

    decision = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=7,
        now=110.0,
    )

    assert decision.outcome is VerificationOutcome.READY
    assert decision.rejection is None
    assert decision.activation_evidence is not None
    assert decision.activation_evidence.coordinates == _coordinates(challenge)
    assert decision.activation_evidence.slash_spec_hash == "sha256:desired"
    assert decision.activation_evidence.channel_connection_id == "conn_1"
    assert decision.activation_evidence.ingress_event_id == "evt_1"
    assert decision.activation_evidence.ingress_message_id == "om_1"
    assert decision.activation_evidence.verified_at == 110.0
    assert ledger.consumed == {("hire_1", challenge.nonce)}
    with pytest.raises(FrozenInstanceError):
        decision.outcome = VerificationOutcome.REJECTED  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.activation_evidence.verified_at = 111.0  # type: ignore[misc,union-attr]


@pytest.mark.parametrize(
    ("target", "coordinates", "expected"),
    [
        (
            "slash",
            {"tenant_key": "tenant_other"},
            VerificationRejection.BINDING_MISMATCH,
        ),
        (
            "channel",
            {"app_id": "cli_other"},
            VerificationRejection.BINDING_MISMATCH,
        ),
        (
            "ingress",
            {"agent_id": "agt_other"},
            VerificationRejection.BINDING_MISMATCH,
        ),
        (
            "ingress",
            {"nonce": "nonce_other_0123456789abcdef"},
            VerificationRejection.BINDING_MISMATCH,
        ),
    ],
)
def test_cross_tenant_wrong_app_agent_or_nonce_is_rejected_without_consumption(
    target: str,
    coordinates: dict[str, object],
    expected: VerificationRejection,
) -> None:
    ledger = _AtomicNonceLedger()
    router = _router(ledger)
    challenge = _challenge(router)
    slash, channel, ingress = _evidence(challenge)
    replacement = replace(_coordinates(challenge), **coordinates)
    if target == "slash":
        slash = replace(slash, coordinates=replacement)
    elif target == "channel":
        channel = replace(channel, coordinates=replacement)
    else:
        ingress = replace(ingress, coordinates=replacement)

    decision = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=7,
        now=110.0,
    )

    assert decision.outcome is VerificationOutcome.REJECTED
    assert decision.rejection is expected
    assert ledger.consumed == set()


def test_stale_generation_and_expired_challenge_are_rejected() -> None:
    ledger = _AtomicNonceLedger()
    router = _router(ledger)
    challenge = _challenge(router)
    slash, channel, ingress = _evidence(challenge)

    stale = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=8,
        now=110.0,
    )
    expired = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=7,
        now=161.0,
    )

    assert stale.rejection is VerificationRejection.STALE_GENERATION
    assert expired.rejection is VerificationRejection.EXPIRED_CHALLENGE
    assert ledger.consumed == set()


@pytest.mark.parametrize(
    ("evidence_name", "changes"),
    [
        ("slash", {"verified_at": 101.0}),
        ("channel", {"verified_at": 97.0}),
        ("ingress", {"received_at": 99.0}),
        ("ingress", {"received_at": 111.0}),
    ],
)
def test_evidence_must_be_ordered_inside_the_challenge_window(
    evidence_name: str,
    changes: dict[str, object],
) -> None:
    ledger = _AtomicNonceLedger()
    router = _router(ledger)
    challenge = _challenge(router)
    slash, channel, ingress = _evidence(challenge)
    if evidence_name == "slash":
        slash = replace(slash, **changes)
    elif evidence_name == "channel":
        channel = replace(channel, **changes)
    else:
        ingress = replace(ingress, **changes)

    decision = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=7,
        now=110.0,
    )

    assert decision.outcome is VerificationOutcome.REJECTED
    assert decision.rejection is VerificationRejection.EVIDENCE_TIME_MISMATCH
    assert ledger.consumed == set()


@pytest.mark.parametrize(
    ("evidence_name", "changes", "expected"),
    [
        (
            "slash",
            {"reconciled": False},
            VerificationRejection.SLASH_NOT_RECONCILED,
        ),
        (
            "slash",
            {"observed_spec_hash": "sha256:drift"},
            VerificationRejection.SLASH_NOT_RECONCILED,
        ),
        (
            "channel",
            {"ready": False},
            VerificationRejection.CHANNEL_IDENTITY_MISMATCH,
        ),
        (
            "channel",
            {"identity_app_id": "cli_other"},
            VerificationRejection.CHANNEL_IDENTITY_MISMATCH,
        ),
        (
            "ingress",
            {"sender_union_id": "on_other"},
            VerificationRejection.INVALID_TENANT_INGRESS,
        ),
        (
            "ingress",
            {"is_p2p": False},
            VerificationRejection.INVALID_TENANT_INGRESS,
        ),
        (
            "ingress",
            {"command": "/task do something"},
            VerificationRejection.INVALID_TENANT_INGRESS,
        ),
        (
            "ingress",
            {"reply_succeeded": False},
            VerificationRejection.INVALID_TENANT_INGRESS,
        ),
        (
            "ingress",
            {"reply_app_id": "cli_other"},
            VerificationRejection.INVALID_TENANT_INGRESS,
        ),
        (
            "ingress",
            {"main_bot_send_count": 1},
            VerificationRejection.INVALID_TENANT_INGRESS,
        ),
    ],
)
def test_incomplete_or_untrusted_evidence_cannot_produce_ready(
    evidence_name: str,
    changes: dict[str, object],
    expected: VerificationRejection,
) -> None:
    ledger = _AtomicNonceLedger()
    router = _router(ledger)
    challenge = _challenge(router)
    slash, channel, ingress = _evidence(challenge)
    if evidence_name == "slash":
        slash = replace(slash, **changes)
    elif evidence_name == "channel":
        channel = replace(channel, **changes)
    else:
        ingress = replace(ingress, **changes)

    decision = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=7,
        now=110.0,
    )

    assert decision.outcome is VerificationOutcome.REJECTED
    assert decision.rejection is expected
    assert ledger.consumed == set()


def test_consumed_nonce_is_rejected_on_replay() -> None:
    ledger = _AtomicNonceLedger()
    router = _router(ledger)
    challenge = _challenge(router)
    slash, channel, ingress = _evidence(challenge)

    first = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=7,
        now=110.0,
    )
    replay = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=7,
        now=111.0,
    )

    assert first.outcome is VerificationOutcome.READY
    assert replay.outcome is VerificationOutcome.REJECTED
    assert replay.rejection is VerificationRejection.NONCE_REPLAY
    assert replay.activation_evidence is None


def test_invalid_nonce_consumer_result_fails_closed() -> None:
    class _BrokenConsumer:
        def consume_once(
            self,
            challenge: VerificationChallenge,
            *,
            consumed_at: float,
        ) -> object:
            return 1

    router = VerificationRouter(
        nonce_consumer=_BrokenConsumer(),  # type: ignore[arg-type]
        clock=lambda: 100.0,
        nonce_factory=lambda: "nonce_0123456789abcdef0123456789abcdef",
    )
    challenge = _challenge(router)
    slash, channel, ingress = _evidence(challenge)

    decision = router.evaluate(
        challenge,
        slash=slash,
        channel=channel,
        ingress=ingress,
        current_generation=7,
        now=110.0,
    )

    assert decision.outcome is VerificationOutcome.REJECTED
    assert decision.rejection is VerificationRejection.NONCE_STORE_FAILURE


@pytest.mark.parametrize(
    ("outcome", "rejection"),
    [
        (VerificationOutcome.READY, VerificationRejection.MISSING_EVIDENCE),
        (VerificationOutcome.REJECTED, None),
    ],
)
def test_decision_contract_rejects_inconsistent_ready_or_rejected_state(
    outcome: VerificationOutcome,
    rejection: VerificationRejection | None,
) -> None:
    with pytest.raises(ValueError, match="decision"):
        VerificationDecision(
            outcome=outcome,
            rejection=rejection,
            activation_evidence=None,
            evaluated_at=1.0,
        )
