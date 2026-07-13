"""Integration contract for the durable employee provisioning activity."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.autonomous.domain import EmployeeState
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
from src.autonomous.workforce.credential_vault import CredentialReceipt

HMAC_KEY = b"hire-provisioning-integration-key!"
APP_SECRET = "employee-secret-never-journaled"


def _request() -> EmployeeHireRequest:
    return EmployeeHireRequest(
        employee_name="Atlas",
        tool="codex",
        model="gpt-5.6-sol",
        effort="high",
        chat_id="oc_admin_dm",
        message_id="om_hire_saga",
        requester_principal_id="ou_admin",
        tenant_key="tenant-a",
        profile="max",
        role="software engineer",
        persona="careful reviewer",
    )


def _writer(tmp_path: Path, epoch: int = 1) -> JournalWriter:
    return JournalWriter.open(
        tmp_path / "journal",
        anchor=FileAnchor(tmp_path / "anchor.json"),
        hmac_key=HMAC_KEY,
        writer_epoch=epoch,
    )


class RecordingRegistrar:
    def __init__(self, writer: JournalWriter) -> None:
        self.writer = writer
        self.calls = 0
        self.callback_returned_before_notification = False

    async def register(self, request, *, on_link, on_status=None):
        del request
        self.calls += 1
        event_types = [
            frame.events[0].event_type for frame in self.writer.replay()
        ]
        assert event_types[-2:] == [
            "hire.effect.prepared",
            "hire.effect.executing",
        ]
        assert self.writer.anchor.read().sequence == 3
        on_link("https://open.feishu.cn/register/one-shot", 60)
        if on_status is not None:
            on_status("waiting_for_admin")
        self.callback_returned_before_notification = True
        return RegistrationResult(app_id="cli_employee", app_secret=APP_SECRET)


class RecordingVault:
    def __init__(self, writer: JournalWriter) -> None:
        self.writer = writer
        self.calls: list[tuple[str, str, str, str, str]] = []

    def put(self, agent_id, app_id, app_secret, hire_intent_id, attempt_id):
        frames = tuple(self.writer.replay())
        assert [frame.events[0].event_type for frame in frames[-3:]] == [
            "hire.effect.committed",
            "hire.effect.prepared",
            "hire.effect.executing",
        ]
        assert frames[-3].events[0].payload["metadata"] == {
            "app_id": "cli_employee"
        }
        assert self.writer.anchor.read().sequence == frames[-1].sequence
        self.calls.append(
            (agent_id, app_id, app_secret, hire_intent_id, attempt_id)
        )
        return CredentialReceipt(
            credential_ref="cred_" + "a" * 64,
            key_id="k1",
            agent_id=agent_id,
            app_id=app_id,
            hire_intent_id=hire_intent_id,
            attempt_id=attempt_id,
            ciphertext_sha256="b" * 64,
            path=Path("/redacted/credential.json"),
        )

    def find_orphan_receipts(self, live_credential_refs):
        del live_credential_refs
        return []


class FlakyVault(RecordingVault):
    def __init__(self, writer: JournalWriter, *, failures: int) -> None:
        super().__init__(writer)
        self.failures = failures
        self.attempts = 0

    def put(self, *args):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise OSError("injected Vault failure")
        return super().put(*args)


@pytest.mark.asyncio
async def test_registration_vault_and_atomic_binding_are_ordered_and_secret_safe(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    registrar = RecordingRegistrar(writer)
    vault = RecordingVault(writer)
    notifications: list[tuple[str, object]] = []
    release_notification = asyncio.Event()

    async def on_link(_state, url: str, expire_in: int) -> None:
        await release_notification.wait()
        notifications.append((url, expire_in))

    async def on_status(_state, status: str) -> None:
        notifications.append(("status", status))

    service = ProductionEmployeeHireService(
        writer,
        ProjectionState(),
        visible_employee_limit=1,
        release_evidence_ready=True,
        credential_keyring_ready=True,
        registrar=registrar,
        credential_vault=vault,
        on_registration_link=on_link,
        on_registration_status=on_status,
    )
    admitted = service.start_hire(_request())

    activity = asyncio.create_task(service.run_provisioning(admitted.intent_id))
    for _ in range(20):
        if registrar.callback_returned_before_notification:
            break
        await asyncio.sleep(0)
    assert registrar.callback_returned_before_notification is True
    assert activity.done() is False
    release_notification.set()
    configured = await activity

    assert configured.phase is HirePhase.CONFIGURING
    assert configured.app_id == "cli_employee"
    assert configured.credential_ref == "cred_" + "a" * 64
    assert set(notifications) == {
        ("status", "waiting_for_admin"),
        ("https://open.feishu.cn/register/one-shot", 60),
    }
    assert vault.calls[0][2] == APP_SECRET
    assert registrar.calls == 1

    frames = tuple(writer.replay())
    assert [event.event_type for event in frames[-1].events] == [
        "employee.bot_principal_bound",
        "bot_principal.bound",
        "employee.state_changed",
    ]
    assert frames[-1].events[-1].payload == {
        "state": EmployeeState.CONFIGURING.value
    }
    assert service.projection_state.cursor_sequence == frames[-1].sequence
    assert service.projection_state.employees[configured.agent_id].state is EmployeeState.CONFIGURING
    principal = service.projection_state.bot_principals[configured.bot_principal_id]
    assert principal.app_id == configured.app_id
    assert principal.credential_ref == configured.credential_ref

    raw_journal = writer.journal_path.read_text(encoding="utf-8")
    assert APP_SECRET not in raw_journal
    assert "open.feishu.cn/register/one-shot" not in raw_journal
    assert APP_SECRET not in repr(configured)
    assert APP_SECRET not in repr(service)


@pytest.mark.asyncio
async def test_transient_vault_failure_retries_without_registering_a_second_app(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    registrar = RecordingRegistrar(writer)
    vault = FlakyVault(writer, failures=1)
    service = ProductionEmployeeHireService(
        writer,
        ProjectionState(),
        visible_employee_limit=1,
        release_evidence_ready=True,
        credential_keyring_ready=True,
        registrar=registrar,
        credential_vault=vault,
    )
    admitted = service.start_hire(_request())

    configured = await service.run_provisioning(admitted.intent_id)

    assert configured.phase is HirePhase.CONFIGURING
    assert registrar.calls == 1
    assert vault.attempts == 2
    assert configured.effect_state("store-credential") is HireEffectState.COMMITTED


@pytest.mark.asyncio
async def test_persistent_vault_failure_disposes_effect_before_action_required(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path)
    registrar = RecordingRegistrar(writer)
    vault = FlakyVault(writer, failures=2)
    service = ProductionEmployeeHireService(
        writer,
        ProjectionState(),
        visible_employee_limit=1,
        release_evidence_ready=True,
        credential_keyring_ready=True,
        registrar=registrar,
        credential_vault=vault,
    )
    admitted = service.start_hire(_request())

    with pytest.raises(HireAdmissionError, match="credential storage"):
        await service.run_provisioning(admitted.intent_id)

    failed = service.get_state(admitted.intent_id)
    assert failed is not None
    assert failed.phase is HirePhase.ACTION_REQUIRED
    assert failed.effect_state("store-credential") is HireEffectState.ACTION_REQUIRED
    assert all(
        effect_state not in {HireEffectState.PREPARED, HireEffectState.EXECUTING}
        for _effect_id, effect_state in failed.effects
    )


def test_start_hire_can_submit_committed_intent_without_constructing_coroutine(
    tmp_path: Path,
) -> None:
    submitted: list[str] = []
    service = ProductionEmployeeHireService(
        _writer(tmp_path),
        ProjectionState(),
        visible_employee_limit=1,
        release_evidence_ready=True,
        credential_keyring_ready=True,
        provisioning_submitter=submitted.append,
    )

    admitted = service.start_hire(_request())

    assert submitted == [admitted.intent_id]


def test_idempotent_retry_resubmits_a_durable_nonterminal_intent(
    tmp_path: Path,
) -> None:
    submitted: list[str] = []

    def submit(intent_id: str) -> None:
        submitted.append(intent_id)
        if len(submitted) == 1:
            raise RuntimeError("injected scheduler failure")

    service = ProductionEmployeeHireService(
        _writer(tmp_path),
        ProjectionState(),
        visible_employee_limit=1,
        release_evidence_ready=True,
        credential_keyring_ready=True,
        provisioning_submitter=submit,
    )
    with pytest.raises(HireAdmissionError, match="submission failed"):
        service.start_hire(_request())

    admitted = service.start_hire(_request())

    assert submitted == [admitted.intent_id, admitted.intent_id]
