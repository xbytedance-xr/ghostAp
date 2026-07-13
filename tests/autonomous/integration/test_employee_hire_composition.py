from __future__ import annotations

import asyncio
import base64
import inspect
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import SecretStr

from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime
from src.autonomous.provisioning.hire_port import EmployeeHireRequest
from src.autonomous.provisioning.hire_state import HireEffectState, HirePhase
from src.autonomous.provisioning.lark_app import RegistrationResult
from src.autonomous.provisioning.slash_commands import VerifiedSlashState
from src.autonomous.supervisor.employee_channels import ChannelProcessState, ChannelSendReceipt


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _settings(tmp_path: Path, *, limit: int) -> SimpleNamespace:
    keyring = {
        "version": 1,
        "keys": {"k1": _b64(b"v" * 32)},
    }
    return SimpleNamespace(
        autonomous_visible_employee_limit=limit,
        autonomous_journal_dir=str(tmp_path / "journal"),
        autonomous_journal_hmac_key=SecretStr(_b64(b"j" * 32)),
        autonomous_anchor_provider="file",
        autonomous_anchor_path=str(tmp_path / "anchor.json"),
        autonomous_credential_dir=str(tmp_path / "vault"),
        autonomous_credential_keys=SecretStr(json.dumps(keyring)),
        autonomous_credential_active_key_id="k1",
        autonomous_worker_sandbox_verified=True,
    )


class _Registrar:
    async def register(self, request, *, on_link, on_status=None):
        del request, on_status
        on_link("https://open.feishu.cn/register/one-shot", 60)
        return RegistrationResult("cli_employee", "runtime-vault-only-secret")


class _Slash:
    async def reconcile(self):
        return VerifiedSlashState(
            spec_hash="slash_hash",
            observed_hash="slash_hash",
            observed=(),
        )


class _FailingSlash:
    async def reconcile(self):
        raise RuntimeError("tenant Slash state did not converge")


class _BlockingSlash:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    async def reconcile(self):
        self.calls += 1
        self.entered.set()
        await asyncio.to_thread(self.release.wait, 5)
        return await _Slash().reconcile()


class _FailOnceSlash:
    def __init__(self, *, failures: int = 1) -> None:
        self.calls = 0
        self.failures = failures

    async def reconcile(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("injected transient recovery failure")
        return await _Slash().reconcile()


class _Channels:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, str, int]] = []
        self.callbacks: dict[str, object] = {}
        self.sent: list[tuple[str, int, str, object, object]] = []
        self.statuses: dict[str, object] = {}
        self.closed = False

    def start(self, agent_id, app_id, credential_ref, generation, on_event):
        self.started.append((agent_id, app_id, credential_ref, generation))
        self.callbacks[agent_id] = on_event
        status = SimpleNamespace(
            state=ChannelProcessState.READY,
            generation=generation,
            identity={"app_id": app_id, "open_id": "ou_employee"},
            ready_metadata={"connection_id": "conn_runtime"},
        )
        self.statuses[agent_id] = status
        return status

    def status(self, agent_id):
        return self.statuses.get(agent_id)

    def crash(self, agent_id: str) -> None:
        status = self.statuses[agent_id]
        self.statuses[agent_id] = SimpleNamespace(
            **{**vars(status), "state": ChannelProcessState.CRASHED}
        )

    def send(self, agent_id, *, generation, target, message, options=None):
        self.sent.append((agent_id, generation, target, message, options))
        status = self.statuses[agent_id]
        return ChannelSendReceipt(
            request_id="send_runtime",
            success=True,
            app_id=status.identity["app_id"],
            generation=generation,
            connection_id=status.ready_metadata["connection_id"],
            message_id="om_employee_reply",
        )

    def recover(self, desired):
        return {item.agent_id: self.start(
            item.agent_id,
            item.app_id,
            item.credential_ref,
            item.generation,
            item.on_event,
        ) for item in desired}

    def close(self):
        self.closed = True


class _FailingStartChannels(_Channels):
    def start(self, agent_id, app_id, credential_ref, generation, on_event):
        del agent_id, app_id, credential_ref, generation, on_event
        raise RuntimeError("injected crash before Channel READY")


class _FailOnceStartChannels(_Channels):
    def __init__(self) -> None:
        super().__init__()
        self.attempted_generations: list[int] = []

    def start(self, agent_id, app_id, credential_ref, generation, on_event):
        self.attempted_generations.append(generation)
        if len(self.attempted_generations) == 1:
            raise RuntimeError("injected first Channel launch failure")
        return super().start(
            agent_id,
            app_id,
            credential_ref,
            generation,
            on_event,
        )


class _FailingVerificationRouter:
    def issue_challenge(self, _binding):
        raise RuntimeError("injected crash after Channel READY")


class _ForgedReceiptChannels(_Channels):
    def send(self, agent_id, *, generation, target, message, options=None):
        del agent_id, target, message, options
        return ChannelSendReceipt(
            request_id="send_forged",
            success=True,
            app_id="cli_main_bot",
            generation=generation,
            connection_id="conn_runtime",
            message_id="om_forged",
        )


def _request() -> EmployeeHireRequest:
    return EmployeeHireRequest(
        employee_name="Atlas",
        tool="codex",
        model="gpt-5.6-sol",
        effort="high",
        chat_id="oc_admin_dm",
        message_id="om_composition",
        requester_principal_id="ou_admin",
        tenant_key="tenant-a",
    )


def _runtime(
    settings: SimpleNamespace,
    *,
    release_evidence_ready: bool,
    **kwargs: object,
) -> EmployeeDepartmentRuntime:
    """Inject a test-only release verdict without exposing a production bypass."""
    with patch.object(
        EmployeeDepartmentRuntime,
        "_release_evidence_ready",
        return_value=release_evidence_ready,
    ):
        kwargs.setdefault(
            "main_bot_send_audit",
            lambda _tenant_key, _started_at, _ended_at: 0,
        )
        return EmployeeDepartmentRuntime.from_settings(settings, **kwargs)


def test_production_factory_has_no_boolean_release_bypass() -> None:
    assert "release_evidence_ready" not in inspect.signature(
        EmployeeDepartmentRuntime.from_settings
    ).parameters
    assert "resume_external" not in inspect.signature(
        EmployeeDepartmentRuntime.recover
    ).parameters


def test_limit_zero_is_dormant_without_touching_durable_paths(tmp_path: Path) -> None:
    runtime = _runtime(
        _settings(tmp_path, limit=0),
        release_evidence_ready=True,
    )

    assert runtime.hire_service is None
    assert runtime.readiness().ready is False
    assert "visible_employee_limit" in runtime.readiness().blockers
    assert not (tmp_path / "journal").exists()
    assert not (tmp_path / "vault").exists()
    runtime.close()


def test_missing_release_evidence_fails_before_keys_or_files_are_opened(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        _settings(tmp_path, limit=1),
        release_evidence_ready=False,
    )

    assert runtime.hire_service is None
    assert runtime.readiness().blockers == ("release_evidence",)
    assert not (tmp_path / "journal").exists()
    runtime.close()


def test_settings_driven_release_evaluator_defaults_to_pending(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1)
    settings.autonomous_employee_release_id = ""
    settings.autonomous_employee_commit_sha = ""
    settings.autonomous_employee_service_instance_id = ""
    settings.autonomous_employee_staging_tenant_hash = ""
    settings.autonomous_employee_production_tenant_hash = ""
    settings.autonomous_employee_release_evidence_bundle = str(
        tmp_path / "missing-evidence.jsonl"
    )
    settings.autonomous_employee_release_checkpoint = str(
        tmp_path / "missing-checkpoint.json"
    )

    runtime = EmployeeDepartmentRuntime.from_settings(settings)

    assert runtime.hire_service is None
    assert runtime.readiness().blockers == ("release_evidence",)
    assert not (tmp_path / "journal").exists()
    runtime.close()


def test_ready_release_without_main_bot_audit_fails_before_durable_open(
    tmp_path: Path,
) -> None:
    with patch.object(
        EmployeeDepartmentRuntime,
        "_release_evidence_ready",
        return_value=True,
    ):
        runtime = EmployeeDepartmentRuntime.from_settings(
            _settings(tmp_path, limit=1),
            notification_link=lambda *_: None,
        )

    assert runtime.hire_service is None
    assert runtime.readiness().blockers == ("main_bot_send_audit",)
    assert not (tmp_path / "journal").exists()
    runtime.close()


def test_ready_runtime_wires_saga_slash_channel_and_durable_challenge(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    runtime = _runtime(
        _settings(tmp_path, limit=1),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert runtime.hire_service is not None

    admitted = runtime.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = runtime.hire_service.get_state(admitted.intent_id)
        if state is not None and state.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)

    assert state is not None
    assert state.phase is HirePhase.READY_PENDING_VERIFICATION
    assert state.slash_spec_hash == state.slash_observed_hash == "slash_hash"
    assert state.channel_generation == 1
    assert state.channel_connection_id == "conn_runtime"
    assert state.verification_nonce
    assert channels.started == [
        (state.agent_id, state.app_id, state.credential_ref, 1)
    ]
    event_types = [
        event.event_type
        for frame in runtime.journal_frames()
        for event in frame.events
    ]
    for effect in ("slash-reconcile:1:1", "channel-start:1"):
        positions = [
            index
            for index, frame in enumerate(runtime.journal_frames())
            if frame.events[0].payload.get("effect_id") == effect
        ]
        assert len(positions) == 3
        assert positions == sorted(positions)
    assert event_types[-1] == "employee.state_changed"

    runtime.close()
    assert channels.closed is True


def test_failed_channel_start_is_disposed_and_retried_with_next_generation(
    tmp_path: Path,
) -> None:
    channels = _FailOnceStartChannels()
    runtime = _runtime(
        _settings(tmp_path, limit=1),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert runtime.hire_service is not None
    admitted = runtime.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = runtime.hire_service.get_state(admitted.intent_id)
        if state is not None and state.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)

    assert state is not None
    assert state.phase is HirePhase.READY_PENDING_VERIFICATION
    assert state.channel_generation == 2
    assert channels.attempted_generations == [1, 2]
    assert state.effect_state("channel-start:1") is HireEffectState.ACTION_REQUIRED
    assert state.effect_state("channel-start:2") is HireEffectState.COMMITTED
    runtime.close()


def test_duplicate_hire_resubmission_reuses_one_configuration_activity(
    tmp_path: Path,
) -> None:
    slash = _BlockingSlash()
    channels = _Channels()
    runtime = _runtime(
        _settings(tmp_path, limit=1),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: slash,
        notification_link=lambda *_: None,
    )
    assert runtime.hire_service is not None
    request = _request()
    admitted = runtime.hire_service.start_hire(request)
    assert slash.entered.wait(5)

    for _ in range(5):
        duplicate = runtime.hire_service.start_hire(request)
        assert duplicate.intent_id == admitted.intent_id
    slash.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = runtime.hire_service.get_state(admitted.intent_id)
        if state is not None and state.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)

    assert state is not None
    assert state.phase is HirePhase.READY_PENDING_VERIFICATION
    assert slash.calls == 1
    assert len(channels.started) == 1
    runtime.close()


def test_duplicate_hire_during_recovery_does_not_start_second_configuration(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1)
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert first.hire_service is not None
    request = _request()
    admitted = first.hire_service.start_hire(request)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = first.hire_service.get_state(admitted.intent_id)
        if pending is not None and pending.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert pending is not None
    first.close()

    slash = _BlockingSlash()
    restarted = _runtime(
        settings,
        release_evidence_ready=True,
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: slash,
        notification_link=lambda *_: None,
    )
    assert restarted.hire_service is not None
    assert slash.entered.wait(5)

    duplicate = restarted.hire_service.start_hire(request)
    assert duplicate.intent_id == admitted.intent_id
    slash.release.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        recovered = restarted.hire_service.get_state(admitted.intent_id)
        if (
            recovered is not None
            and recovered.phase is HirePhase.READY_PENDING_VERIFICATION
            and recovered.channel_generation == 2
        ):
            break
        time.sleep(0.02)

    assert recovered is not None
    assert recovered.phase is HirePhase.READY_PENDING_VERIFICATION
    assert slash.calls == 1
    restarted.close()


def test_transient_recovery_failure_is_supervised_and_converges_in_process(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1)
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert first.hire_service is not None
    admitted = first.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = first.hire_service.get_state(admitted.intent_id)
        if pending is not None and pending.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert pending is not None
    first.close()

    slash = _FailOnceSlash(failures=4)
    restarted = _runtime(
        settings,
        release_evidence_ready=True,
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: slash,
        notification_link=lambda *_: None,
    )
    assert restarted.hire_service is not None
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        recovered = restarted.hire_service.get_state(admitted.intent_id)
        if (
            recovered is not None
            and recovered.phase is HirePhase.READY_PENDING_VERIFICATION
            and recovered.channel_generation == 2
        ):
            break
        time.sleep(0.05)

    assert recovered is not None
    assert recovered.phase is HirePhase.READY_PENDING_VERIFICATION
    assert slash.calls == 5
    assert restarted.readiness().ready is True
    restarted.close()


def test_real_employee_status_ingress_and_employee_send_are_required_for_active(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    runtime = _runtime(
        _settings(tmp_path, limit=1),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert runtime.hire_service is not None
    admitted = runtime.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = runtime.hire_service.get_state(admitted.intent_id)
        if pending is not None and pending.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert pending is not None
    callback = channels.callbacks[pending.agent_id]

    callback(  # type: ignore[operator]
        {
            "event": "rawMessageMeta",
            "data": {
                "event_id": "evt_status",
                "tenant_key": "tenant-a",
                "message_id": "om_status",
            },
        }
    )
    callback(  # type: ignore[operator]
        {
            "event": "message",
            "data": {
                "id": "om_status",
                "content_text": "/status",
                "conversation": {"chat_type": "p2p"},
                "sender": {"open_id": "ou_admin"},
                "raw": {"message_id": "om_status"},
            },
        }
    )
    while time.monotonic() < deadline:
        active = runtime.hire_service.get_state(admitted.intent_id)
        if active is not None and active.phase is HirePhase.ACTIVE:
            break
        time.sleep(0.02)

    assert active is not None and active.phase is HirePhase.ACTIVE
    assert active.activation_ingress_event_id == "evt_status"
    assert active.activation_send_request_id == "send_runtime"
    assert channels.sent == [
        (
            active.agent_id,
            1,
            "ou_admin",
            {"text": "Atlas is ready."},
            {"reply_to": "om_status"},
        )
    ]
    runtime.close()


def test_forged_employee_send_identity_cannot_activate(tmp_path: Path) -> None:
    channels = _ForgedReceiptChannels()
    runtime = _runtime(
        _settings(tmp_path, limit=1),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert runtime.hire_service is not None
    admitted = runtime.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = runtime.hire_service.get_state(admitted.intent_id)
        if pending is not None and pending.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert pending is not None
    callback = channels.callbacks[pending.agent_id]
    callback({  # type: ignore[operator]
        "event": "rawMessageMeta",
        "data": {
            "event_id": "evt_forged",
            "tenant_key": "tenant-a",
            "message_id": "om_forged_status",
        },
    })
    callback({  # type: ignore[operator]
        "event": "message",
        "data": {
            "id": "om_forged_status",
            "content_text": "/status",
            "conversation": {"chat_type": "p2p"},
            "sender": {"open_id": "ou_admin"},
            "raw": {},
        },
    })
    time.sleep(0.2)

    rejected = runtime.hire_service.get_state(admitted.intent_id)
    assert rejected is not None
    assert rejected.phase is HirePhase.READY_PENDING_VERIFICATION
    assert rejected.activation_send_request_id == ""
    runtime.close()


def test_limit_zero_blocks_new_admission_but_replays_existing_employee_state(
    tmp_path: Path,
) -> None:
    first_channels = _Channels()
    settings = _settings(tmp_path, limit=1)
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=first_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert first.hire_service is not None
    admitted = first.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = first.hire_service.get_state(admitted.intent_id)
        if state is not None and state.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert state is not None
    first.close()

    settings.autonomous_visible_employee_limit = 0
    recovered_channels = _Channels()
    recovered = _runtime(
        settings,
        release_evidence_ready=False,
        channel_supervisor=recovered_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
    )

    assert recovered.hire_service is not None
    replayed = recovered.hire_service.get_state(admitted.intent_id)
    assert replayed is not None
    assert replayed.phase is HirePhase.READY_PENDING_VERIFICATION
    assert recovered_channels.started == []
    assert set(recovered.readiness().blockers) >= {
        "visible_employee_limit",
        "release_evidence",
    }
    recovered.recover()
    time.sleep(0.1)
    assert recovered_channels.started == []
    duplicate = recovered.hire_service.start_hire(_request())
    assert duplicate.intent_id == admitted.intent_id
    time.sleep(0.1)
    assert recovered_channels.started == []
    recovered.close()


def test_hard_closed_nonterminal_replay_cannot_resubmit_external_activity(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1)
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _FailingSlash(),
        notification_link=lambda *_: None,
    )
    assert first.hire_service is not None
    admitted = first.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        failed = first.hire_service.get_state(admitted.intent_id)
        if (
            failed is not None
            and failed.phase is HirePhase.CONFIGURING
            and failed.effect_state("slash-reconcile:1:1")
            is HireEffectState.EXECUTING
        ):
            break
        time.sleep(0.02)
    assert failed is not None
    first.close()

    settings.autonomous_visible_employee_limit = 0
    channels = _Channels()
    recovered = _runtime(
        settings,
        release_evidence_ready=False,
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
    )
    assert recovered.hire_service is not None
    replayed = recovered.hire_service.start_hire(_request())
    assert replayed.intent_id == admitted.intent_id
    time.sleep(0.2)

    still_closed = recovered.hire_service.get_state(admitted.intent_id)
    assert still_closed is not None
    assert still_closed.phase is HirePhase.CONFIGURING
    assert channels.started == []
    recovered.close()


def test_crashed_active_channel_advances_generation_and_requires_reverification(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    runtime = _runtime(
        _settings(tmp_path, limit=1),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert runtime.hire_service is not None
    admitted = runtime.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = runtime.hire_service.get_state(admitted.intent_id)
        if state is not None and state.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert state is not None
    callback = channels.callbacks[state.agent_id]
    callback({  # type: ignore[operator]
        "event": "rawMessageMeta",
        "data": {"event_id": "evt_1", "tenant_key": "tenant-a", "message_id": "om_1"},
    })
    callback({  # type: ignore[operator]
        "event": "message",
        "data": {
            "id": "om_1",
            "content_text": "/status",
            "conversation": {"chat_type": "p2p"},
            "sender": {"open_id": "ou_admin"},
            "raw": {},
        },
    })
    while time.monotonic() < deadline:
        active = runtime.hire_service.get_state(admitted.intent_id)
        if active is not None and active.phase is HirePhase.ACTIVE:
            break
        time.sleep(0.02)
    assert active is not None and active.phase is HirePhase.ACTIVE

    channels.crash(active.agent_id)
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        revalidating = runtime.hire_service.get_state(admitted.intent_id)
        if (
            revalidating is not None
            and revalidating.phase is HirePhase.READY_PENDING_VERIFICATION
            and revalidating.channel_generation == 2
        ):
            break
        time.sleep(0.05)

    assert revalidating is not None
    assert revalidating.phase is HirePhase.READY_PENDING_VERIFICATION
    assert revalidating.channel_generation == 2
    assert [item[3] for item in channels.started] == [1, 2]
    runtime.close()


def test_restart_replaces_active_channel_generation_and_challenge(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1)
    first_channels = _Channels()
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=first_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert first.hire_service is not None
    admitted = first.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = first.hire_service.get_state(admitted.intent_id)
        if pending is not None and pending.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert pending is not None
    old_nonce = pending.verification_nonce
    callback = first_channels.callbacks[pending.agent_id]
    callback({  # type: ignore[operator]
        "event": "rawMessageMeta",
        "data": {
            "event_id": "evt_restart",
            "tenant_key": "tenant-a",
            "message_id": "om_restart",
        },
    })
    callback({  # type: ignore[operator]
        "event": "message",
        "data": {
            "id": "om_restart",
            "content_text": "/status",
            "conversation": {"chat_type": "p2p"},
            "sender": {"open_id": "ou_admin"},
            "raw": {},
        },
    })
    while time.monotonic() < deadline:
        active = first.hire_service.get_state(admitted.intent_id)
        if active is not None and active.phase is HirePhase.ACTIVE:
            break
        time.sleep(0.02)
    assert active is not None and active.phase is HirePhase.ACTIVE
    first.close()

    restarted_channels = _Channels()
    restarted = _runtime(
        settings,
        release_evidence_ready=True,
        channel_supervisor=restarted_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert restarted.hire_service is not None
    deadline = time.monotonic() + 7
    while time.monotonic() < deadline:
        reverified = restarted.hire_service.get_state(admitted.intent_id)
        if (
            reverified is not None
            and reverified.phase is HirePhase.READY_PENDING_VERIFICATION
            and reverified.channel_generation == 2
        ):
            break
        time.sleep(0.05)

    assert reverified is not None
    assert reverified.phase is HirePhase.READY_PENDING_VERIFICATION
    assert reverified.channel_generation == 2
    assert reverified.verification_nonce != old_nonce
    assert [item[3] for item in restarted_channels.started] == [2]
    restarted.close()


def test_restart_slash_drift_cannot_reuse_old_verified_hash(tmp_path: Path) -> None:
    settings = _settings(tmp_path, limit=1)
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert first.hire_service is not None
    admitted = first.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = first.hire_service.get_state(admitted.intent_id)
        if pending is not None and pending.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert pending is not None
    old_nonce = pending.verification_nonce
    first.close()

    restarted_channels = _Channels()
    restarted = _runtime(
        settings,
        release_evidence_ready=True,
        channel_supervisor=restarted_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _FailingSlash(),
        notification_link=lambda *_: None,
    )
    assert restarted.hire_service is not None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = restarted.hire_service.get_state(admitted.intent_id)
        if state is not None and state.phase is HirePhase.VALIDATING:
            time.sleep(0.1)
            break
        time.sleep(0.02)

    state = restarted.hire_service.get_state(admitted.intent_id)
    assert state is not None
    assert state.phase is HirePhase.VALIDATING
    assert state.verification_nonce == old_nonce
    assert restarted_channels.started == []
    restarted.close()


def test_restart_after_failed_channel_attempts_forces_new_generation_slash(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1)
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_FailingStartChannels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert first.hire_service is not None
    admitted = first.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        crashed = first.hire_service.get_state(admitted.intent_id)
        if (
                crashed is not None
                and crashed.effect_state("slash-reconcile:2:1")
                is HireEffectState.COMMITTED
                and crashed.effect_state("channel-start:2")
                is HireEffectState.ACTION_REQUIRED
        ):
            break
        time.sleep(0.02)
    assert crashed is not None
    first.close()

    restarted_channels = _Channels()
    restarted = _runtime(
        settings,
        release_evidence_ready=True,
        channel_supervisor=restarted_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _FailingSlash(),
        notification_link=lambda *_: None,
    )
    assert restarted.hire_service is not None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = restarted.hire_service.get_state(admitted.intent_id)
        if state is not None and state.effect_state("slash-reconcile:3:1") is not None:
            break
        time.sleep(0.02)

    state = restarted.hire_service.get_state(admitted.intent_id)
    assert state is not None
    assert state.phase is HirePhase.CONFIGURING
    assert state.effect_state("slash-reconcile:3:1") is HireEffectState.EXECUTING
    assert restarted_channels.started == []
    restarted.close()


def test_restart_after_channel_commit_fences_generation_before_slash_refresh(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1)
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )
    assert first.hire_service is not None
    first._verification_router = _FailingVerificationRouter()  # type: ignore[assignment]
    admitted = first.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        crashed = first.hire_service.get_state(admitted.intent_id)
        if (
            crashed is not None
            and crashed.phase is HirePhase.CONFIGURING
            and crashed.channel_generation == 1
            and crashed.effect_state("channel-start:1")
            is HireEffectState.COMMITTED
        ):
            break
        time.sleep(0.02)
    assert crashed is not None
    first.close()

    restarted_channels = _Channels()
    restarted = _runtime(
        settings,
        release_evidence_ready=True,
        channel_supervisor=restarted_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _FailingSlash(),
        notification_link=lambda *_: None,
    )
    assert restarted.hire_service is not None
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = restarted.hire_service.get_state(admitted.intent_id)
        if state is not None and state.effect_state("slash-reconcile:2:1") is not None:
            break
        time.sleep(0.02)

    state = restarted.hire_service.get_state(admitted.intent_id)
    assert state is not None
    assert state.phase is HirePhase.VALIDATING
    assert state.channel_generation == 1
    assert state.effect_state("slash-reconcile:2:1") is HireEffectState.EXECUTING
    assert restarted_channels.started == []
    restarted.close()
