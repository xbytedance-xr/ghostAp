from __future__ import annotations

from pathlib import Path

import pytest

from src.autonomous.journal.anchor import FileAnchor
from src.autonomous.journal.projections import ProjectionState
from src.autonomous.journal.writer import JournalWriter
from src.autonomous.provisioning.hire_port import EmployeeHireRequest
from src.autonomous.provisioning.hire_service import (
    HireAdmissionError,
    ProductionEmployeeHireService,
)
from src.autonomous.provisioning.hire_state import HireEffectState, HirePhase
from src.autonomous.provisioning.lark_app import RegistrationResult
from src.autonomous.provisioning.verification import (
    ChannelVerificationEvidence,
    SlashVerificationEvidence,
    TenantIngressEvidence,
    VerificationBinding,
    VerificationCoordinates,
    VerificationOutcome,
    VerificationRouter,
)
from src.autonomous.workforce.credential_vault import CredentialReceipt

HMAC_KEY = b"employee-activation-gate-key-32bytes"


class _Registrar:
    async def register(self, request, *, on_link, on_status=None):
        del request, on_link, on_status
        return RegistrationResult("cli_employee", "vault-only-secret")


class _Vault:
    def put(self, agent_id, app_id, app_secret, hire_intent_id, attempt_id):
        assert app_secret == "vault-only-secret"
        return CredentialReceipt(
            credential_ref="cred_" + "a" * 64,
            key_id="k1",
            agent_id=agent_id,
            app_id=app_id,
            hire_intent_id=hire_intent_id,
            attempt_id=attempt_id,
            ciphertext_sha256="b" * 64,
            path=Path("/redacted"),
        )

    def find_orphan_receipts(self, live_credential_refs):
        del live_credential_refs
        return []


def _writer(tmp_path: Path, epoch: int = 1) -> JournalWriter:
    return JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=epoch,
    )


def _service(writer: JournalWriter) -> ProductionEmployeeHireService:
    return ProductionEmployeeHireService(
        writer,
        ProjectionState(),
        visible_employee_limit=1,
        release_evidence_ready=True,
        credential_keyring_ready=True,
        registrar=_Registrar(),
        credential_vault=_Vault(),
    )


def _request() -> EmployeeHireRequest:
    return EmployeeHireRequest(
        employee_name="Atlas",
        tool="codex",
        model="gpt-5.6-sol",
        effort="high",
        chat_id="oc_admin_dm",
        message_id="om_activation",
        requester_principal_id="ou_admin",
        tenant_key="tenant-a",
    )


async def _configured(service: ProductionEmployeeHireService):
    admitted = service.start_hire(_request())
    return await service.run_provisioning(admitted.intent_id)


def _effect(
    service: ProductionEmployeeHireService,
    intent_id: str,
    effect_id: str,
    effect_type: str,
    metadata: dict[str, str],
) -> None:
    for state in (
        HireEffectState.PREPARED,
        HireEffectState.EXECUTING,
        HireEffectState.COMMITTED,
    ):
        service.commit_effect_transition(
            intent_id,
            effect_id=effect_id,
            effect_type=effect_type,
            next_state=state,
            metadata=metadata if state is HireEffectState.COMMITTED else None,
        )


@pytest.mark.asyncio
async def test_active_requires_durable_slash_channel_ingress_and_employee_send(
    tmp_path: Path,
) -> None:
    service = _service(_writer(tmp_path))
    configured = await _configured(service)
    _effect(
        service,
        configured.intent_id,
        "slash-reconcile:1",
        "slash_reconciliation",
        {
            "slash_spec_hash": "spec_hash",
            "slash_observed_hash": "spec_hash",
            "slash_verified_at": "98.0",
        },
    )
    _effect(
        service,
        configured.intent_id,
        "channel-start:1",
        "employee_channel_start",
        {
            "app_id": configured.app_id,
            "generation": "1",
            "identity_app_id": configured.app_id,
            "connection_id": "conn_1",
            "channel_verified_at": "99.0",
        },
    )
    router = VerificationRouter(
        nonce_consumer=service,
        clock=lambda: 100.0,
        nonce_factory=lambda: "nonce_0123456789abcdef0123456789abcdef",
    )
    challenge = router.issue_challenge(
        VerificationBinding(
            hire_intent_id=configured.intent_id,
            tenant_key=configured.tenant_key,
            app_id=configured.app_id,
            agent_id=configured.agent_id,
            generation=1,
            requester_principal_id=configured.requester_principal_id,
            expected_slash_spec_hash="spec_hash",
        ),
        ttl_seconds=60,
    )

    pending = service.begin_activation_verification(challenge)

    assert pending.phase is HirePhase.READY_PENDING_VERIFICATION
    assert pending.verification_nonce == challenge.nonce
    coordinates = VerificationCoordinates(
        hire_intent_id=pending.intent_id,
        tenant_key=pending.tenant_key,
        app_id=pending.app_id,
        agent_id=pending.agent_id,
        generation=1,
        nonce=challenge.nonce,
    )
    decision = router.evaluate(
        challenge,
        slash=SlashVerificationEvidence(
            coordinates,
            "spec_hash",
            "spec_hash",
            True,
            98.0,
        ),
        channel=ChannelVerificationEvidence(
            coordinates,
            pending.app_id,
            "conn_1",
            True,
            99.0,
        ),
        ingress=TenantIngressEvidence(
            coordinates,
            "evt_1",
            "om_status",
            pending.requester_principal_id,
            "/status",
            True,
            True,
            pending.app_id,
            "send_1",
            0,
            107.0,
        ),
        current_generation=1,
        now=110.0,
    )
    assert decision.outcome is VerificationOutcome.READY

    active = service.commit_activation(decision)

    assert active.phase is HirePhase.ACTIVE
    assert active.activation_ingress_event_id == "evt_1"
    assert service.consume_once(challenge, consumed_at=111.0) is False
    assert "nonce_012345" not in repr(active)


@pytest.mark.asyncio
async def test_verification_cannot_start_without_committed_exact_configuration(
    tmp_path: Path,
) -> None:
    service = _service(_writer(tmp_path))
    configured = await _configured(service)
    router = VerificationRouter(nonce_consumer=service, clock=lambda: 100.0)
    challenge = router.issue_challenge(
        VerificationBinding(
            configured.intent_id,
            configured.tenant_key,
            configured.app_id,
            configured.agent_id,
            1,
            configured.requester_principal_id,
            "spec_hash",
        )
    )

    with pytest.raises(HireAdmissionError, match="configuration evidence"):
        service.begin_activation_verification(challenge)
    assert service.recover().get(configured.intent_id).phase is HirePhase.CONFIGURING


@pytest.mark.asyncio
async def test_activation_nonce_and_terminal_state_survive_replay(tmp_path: Path) -> None:
    service = _service(_writer(tmp_path))
    configured = await _configured(service)
    _effect(
        service,
        configured.intent_id,
        "slash-reconcile:1",
        "slash_reconciliation",
        {
            "slash_spec_hash": "spec_hash",
            "slash_observed_hash": "spec_hash",
            "slash_verified_at": "98.0",
        },
    )
    _effect(
        service,
        configured.intent_id,
        "channel-start:1",
        "employee_channel_start",
        {
            "app_id": configured.app_id,
            "generation": "1",
            "identity_app_id": configured.app_id,
            "connection_id": "conn_1",
            "channel_verified_at": "99.0",
        },
    )
    router = VerificationRouter(
        nonce_consumer=service,
        clock=lambda: 100.0,
        nonce_factory=lambda: "nonce_replay_0123456789abcdef0123456789",
    )
    challenge = router.issue_challenge(
        VerificationBinding(
            configured.intent_id,
            configured.tenant_key,
            configured.app_id,
            configured.agent_id,
            1,
            configured.requester_principal_id,
            "spec_hash",
        )
    )
    service.begin_activation_verification(challenge)
    assert service.consume_once(challenge, consumed_at=101.0) is True
    service.projection_state  # keep replay cursor exercised before close
    service.close()

    reopened = _service(_writer(tmp_path, epoch=2))
    replayed = reopened.recover().get(configured.intent_id)

    assert replayed is not None
    assert replayed.phase is HirePhase.READY_PENDING_VERIFICATION
    assert replayed.verification_consumed is True
    assert reopened.consume_once(challenge, consumed_at=102.0) is False


@pytest.mark.asyncio
async def test_challenge_issue_resumes_after_crash_in_validating_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service(_writer(tmp_path))
    configured = await _configured(service)
    _effect(
        service,
        configured.intent_id,
        "slash-reconcile:1",
        "slash_reconciliation",
        {
            "slash_spec_hash": "spec_hash",
            "slash_observed_hash": "spec_hash",
            "slash_verified_at": "98.0",
        },
    )
    _effect(
        service,
        configured.intent_id,
        "channel-start:1",
        "employee_channel_start",
        {
            "app_id": configured.app_id,
            "generation": "1",
            "identity_app_id": configured.app_id,
            "connection_id": "conn_1",
            "channel_verified_at": "99.0",
        },
    )
    challenge = VerificationRouter(
        nonce_consumer=service,
        clock=lambda: 100.0,
        nonce_factory=lambda: "nonce_crash_0123456789abcdef0123456789",
    ).issue_challenge(
        VerificationBinding(
            configured.intent_id,
            configured.tenant_key,
            configured.app_id,
            configured.agent_id,
            1,
            configured.requester_principal_id,
            "spec_hash",
        )
    )
    original_commit = service._commit_hire_event

    def crash(_event):
        raise OSError("injected crash")

    monkeypatch.setattr(service, "_commit_hire_event", crash)
    with pytest.raises(OSError, match="injected crash"):
        service.begin_activation_verification(challenge)
    assert service.get_state(configured.intent_id).phase is HirePhase.VALIDATING

    monkeypatch.setattr(service, "_commit_hire_event", original_commit)
    resumed = service.begin_activation_verification(challenge)

    assert resumed.phase is HirePhase.READY_PENDING_VERIFICATION
