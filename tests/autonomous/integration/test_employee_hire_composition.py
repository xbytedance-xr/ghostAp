from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import SecretStr

from src.autonomous.context import (
    AuthorizedContextRequest,
    ContextLayer,
    ContextMessage,
    MessagePage,
    ResolvedThread,
)
from src.autonomous.data.models import DataKind, ExecutionAttemptContext
from src.autonomous.data.ports import PublishEmployeeDocumentCommand
from src.autonomous.gateway.coordinator import EmployeeDispatchCoordinator
from src.autonomous.gateway.env_scope import EmployeeProcessEnvironmentMaterial
from src.autonomous.ingress.models import EmployeeIngressMetadata, EmployeeIngressPayload
from src.autonomous.journal.anchor import MemoryAnchor
from src.autonomous.journal.frame import JournalEvent
from src.autonomous.provisioning.composition import EmployeeDepartmentRuntime
from src.autonomous.provisioning.hire_port import EmployeeHireRequest
from src.autonomous.provisioning.hire_state import (
    DurableHireState,
    HireEffectState,
    HirePhase,
)
from src.autonomous.provisioning.lark_app import RegistrationResult
from src.autonomous.provisioning.slash_commands import VerifiedSlashState
from src.autonomous.supervisor.employee_channels import ChannelProcessState, ChannelSendReceipt
from src.autonomous.workforce.projection import commit_workforce_events


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _settings(
    tmp_path: Path,
    *,
    limit: int,
    context_configured: bool = False,
) -> SimpleNamespace:
    keyring = {
        "version": 1,
        "keys": {"k1": _b64(b"v" * 32)},
    }
    settings = SimpleNamespace(
        autonomous_visible_employee_limit=limit,
        autonomous_journal_dir=str(tmp_path / "journal"),
        autonomous_journal_hmac_key=SecretStr(_b64(b"j" * 32)),
        autonomous_anchor_provider="file",
        autonomous_anchor_path=str(tmp_path / "anchor.json"),
        autonomous_credential_dir=str(tmp_path / "vault"),
        autonomous_credential_keys=SecretStr(json.dumps(keyring)),
        autonomous_credential_active_key_id="k1",
        autonomous_worker_sandbox_verified=True,
        autonomous_employee_service_instance_id="ghostap-prod-a",
        autonomous_main_bot_audit_dir=str(tmp_path / "main-bot-audit"),
        autonomous_main_bot_audit_anchor_path=str(tmp_path / "main-bot-audit.anchor"),
    )
    if context_configured:
        settings.autonomous_data_keys = SecretStr(json.dumps(keyring))
        settings.autonomous_data_active_key_id = "k1"
        settings.autonomous_data_blob_dir = str(tmp_path / "data-blobs")
        settings.autonomous_employee_ingress_blob_dir = str(tmp_path / "ingress-blobs")
        settings.autonomous_employee_outbox_blob_dir = str(tmp_path / "outbox-blobs")
        settings.autonomous_employee_attachment_staging_dir = str(tmp_path / "attachments")
        settings.autonomous_employee_system_prompt_token_reserve = 4096
        settings.autonomous_employee_queue_per_employee_limit = 8
        settings.autonomous_employee_queue_per_team_limit = 32
        settings.autonomous_employee_queue_global_limit = 128
        settings.autonomous_state_dir = str(tmp_path / "state")
        settings.autonomous_slock_storage_base = str(tmp_path / "slock")
        settings.autonomous_history_timezone = "UTC"
        settings.autonomous_history_max_range_days = 31
        settings.autonomous_history_page_size = 50
        settings.autonomous_thread_context_max_messages = 200
        settings.autonomous_thread_context_max_chars = 400_000
        settings.autonomous_group_context_max_messages = 50
        settings.autonomous_context_max_tokens = 128_000
        settings.autonomous_thread_context_page_size = 50
        settings.autonomous_group_context_page_size = 20
        settings.autonomous_context_fetch_timeout_seconds = 30.0
        settings.autonomous_context_max_pages = 200
        settings.autonomous_manager_acl = "ou_admin"
        settings.admin_user_ids = frozenset({"ou_admin"})
        settings.allowed_chat_ids = frozenset()
        settings.app_id = "cli_manager"
    return settings


class _Registrar:
    async def register(self, request, *, on_link, on_status=None):
        del request, on_status
        on_link("https://open.feishu.cn/register/one-shot", 60)
        return RegistrationResult("cli_employee", "runtime-vault-only-secret")


class _DistinctRegistrar:
    async def register(self, request, *, on_link, on_status=None):
        del on_status
        on_link("https://open.feishu.cn/register/one-shot", 60)
        suffix = request.name.casefold()
        return RegistrationResult(
            f"cli_employee_{suffix}",
            f"runtime-vault-only-secret-{suffix}",
        )


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
            agent_id=agent_id,
            app_id=app_id,
            tenant_key="tenant-a",
            bot_principal_id="bot_" + agent_id.removeprefix("agt_"),
            state=ChannelProcessState.READY,
            generation=generation,
            identity={"app_id": app_id, "open_id": "ou_employee"},
            ready_metadata={"connection_id": "conn_runtime"},
        )
        self.statuses[agent_id] = status
        return status

    def status(self, agent_id):
        return self.statuses.get(agent_id)

    @contextmanager
    def employee_dispatch_guard(self):
        yield

    def crash(self, agent_id: str) -> None:
        status = self.statuses[agent_id]
        self.statuses[agent_id] = SimpleNamespace(**{**vars(status), "state": ChannelProcessState.CRASHED})

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

    def update_card(self, agent_id, *, generation, message_id, card):
        status = self.statuses[agent_id]
        self.sent.append((agent_id, generation, message_id, card, "update"))
        return ChannelSendReceipt(
            request_id="update_runtime",
            success=True,
            app_id=status.identity["app_id"],
            generation=generation,
            connection_id=status.ready_metadata["connection_id"],
            message_id=message_id,
        )

    def recover(self, desired):
        return {
            item.agent_id: self.start(
                item.agent_id,
                item.app_id,
                item.credential_ref,
                item.generation,
                item.on_event,
            )
            for item in desired
        }

    def close(self):
        self.closed = True


class _SlockManager:
    @contextmanager
    def employee_activation_guard(self, **_kwargs):
        raise RuntimeError("no activated Slock in composition-only test")
        yield

    def resolve_employee_engine(self, **_kwargs):
        raise RuntimeError("no activated Slock in composition-only test")


class _HealthyMembership:
    def is_degraded(self, _agent_id: str, _team_id: str) -> bool:
        return False


class _ContextSourceFactory:
    def __init__(self, *, probe_ready: bool = True) -> None:
        self.probe_ready = probe_ready
        self.probed: list[tuple[str, str, str]] = []
        self.invalidated: list[str] = []
        self.reactivated: list[str] = []
        self.closed = False

    def probe(self, principal):
        self.probed.append((principal.agent_id, principal.app_id, principal.credential_ref))
        return self.probe_ready

    def open(self, **_kwargs):
        raise AssertionError("readiness must not perform message API calls")

    def invalidate_employee(self, agent_id):
        self.invalidated.append(agent_id)

    def reactivate_employee(self, agent_id):
        self.reactivated.append(agent_id)

    def close(self):
        self.closed = True


class _GroupMemory:
    def read_group_memory(self, _chat_id):
        return ""


class _StableGroupMemory(_GroupMemory):
    def __init__(self, content: str) -> None:
        self.content = content

    def read_group_memory(self, _chat_id):
        return self.content


class _RuntimeMessageSource:
    def __init__(self, scope) -> None:
        self.scope = scope
        self._thread_pass = 0

    def resolve_thread(self):
        return ResolvedThread(
            self.scope.thread_root_message_id,
            self.scope.feishu_thread_id,
            self.scope.current_message_id,
        )

    def list_thread_messages(self, *, page_token="", page_size=50):
        del page_token, page_size
        self._thread_pass += 1
        messages = (
            self._message(
                self.scope.thread_root_message_id,
                text="root",
                create_time=1_000,
                position=0,
                root_id="",
                current=False,
            ),
            self._message(
                self.scope.current_message_id,
                text="current",
                create_time=2_000,
                position=1,
                root_id=self.scope.thread_root_message_id,
                current=True,
            ),
        )
        return MessagePage(messages, False)

    def list_chat_messages(self, *, page_token="", page_size=20):
        del page_token, page_size
        return MessagePage((), False)

    def reset_chat_traversal(self):
        return None

    def close(self):
        return None

    def _message(
        self,
        message_id,
        *,
        text,
        create_time,
        position,
        root_id,
        current,
    ):
        return ContextMessage(
            message_id=message_id,
            sender_id="ou_admin",
            sender_type="user",
            text=text,
            timestamp=create_time / 1000,
            is_system=False,
            edited=False,
            deleted=False,
            chat_id=self.scope.chat_id,
            thread_id=self.scope.feishu_thread_id,
            root_id=root_id,
            parent_id=root_id,
            sender_id_type="open_id",
            sender_tenant_key=self.scope.tenant_key,
            msg_type="text",
            create_time_ms=create_time,
            update_time_ms=create_time,
            message_position=position,
            thread_message_position=position,
            is_current=current,
        )


class _AssemblingContextSourceFactory(_ContextSourceFactory):
    @contextmanager
    def open(self, *, scope, principal):
        del principal
        yield _RuntimeMessageSource(scope)


class _ReplayContextSource(_RuntimeMessageSource):
    def __init__(self, scope, *, split_pages: bool) -> None:
        super().__init__(scope)
        self._split_pages = split_pages

    def _thread_messages(self):
        root = replace(
            self._message(
                self.scope.thread_root_message_id,
                text="root-edited",
                create_time=1_000,
                position=0,
                root_id="",
                current=False,
            ),
            update_time_ms=1_500,
            edited=True,
        )
        deleted = replace(
            self._message(
                "om_runtime_deleted",
                text="",
                create_time=1_800,
                position=1,
                root_id=self.scope.thread_root_message_id,
                current=False,
            ),
            update_time_ms=2_200,
            deleted=True,
        )
        current = self._message(
            self.scope.current_message_id,
            text="current",
            create_time=3_000,
            position=2,
            root_id=self.scope.thread_root_message_id,
            current=True,
        )
        return root, deleted, current

    def list_thread_messages(self, *, page_token="", page_size=50):
        del page_size
        messages = self._thread_messages()
        if self._split_pages:
            if not page_token:
                return MessagePage(messages[:2], True, "next")
            assert page_token == "next"
            return MessagePage(messages[2:], False)
        assert not page_token
        return MessagePage(messages, False)

    def list_chat_messages(self, *, page_token="", page_size=20):
        del page_token, page_size
        root = self._thread_messages()[0]
        group = self._message(
            "om_runtime_group",
            text="group",
            create_time=2_500,
            position=10,
            root_id="",
            current=False,
        )
        return MessagePage((group, root), False)


class _ReplayContextSourceFactory(_ContextSourceFactory):
    def __init__(self, *, split_pages: bool) -> None:
        super().__init__()
        self._split_pages = split_pages

    @contextmanager
    def open(self, *, scope, principal):
        del principal
        yield _ReplayContextSource(
            scope,
            split_pages=self._split_pages,
        )


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
        kwargs.setdefault("slock_engine_manager", _SlockManager())
        kwargs.setdefault(
            "employee_environment_provider",
            lambda authority: EmployeeProcessEnvironmentMaterial(
                authority.tenant_key,
                authority.agent_id,
                authority.employee_version,
                authority.credential_ref,
                {"PATH": "/usr/bin"},
                {},
            ),
        )
        return EmployeeDepartmentRuntime.from_settings(settings, **kwargs)


def test_task7_runtime_owns_durable_ingress_router_and_gateway(tmp_path: Path) -> None:
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
    )

    assert runtime.ingress_service is not None
    assert runtime.ingress_router is not None
    assert runtime.dispatch_coordinator is not None
    assert runtime.outbox_service is not None
    assert runtime.outbox_delivery is not None
    assert runtime.fire_service is not None
    assert runtime.execution_readiness().ready is True
    runtime.close()


def test_runtime_composes_canonical_membership_service(tmp_path: Path) -> None:
    manager_client = object()
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
        manager_client_factory=lambda: manager_client,
    )

    assert runtime.membership_service is not None
    assert runtime.ingress_router is not None
    assert runtime.ingress_router._membership_health is runtime.membership_service
    runtime.close()


def test_task7_execution_readiness_requires_slock_gateway(tmp_path: Path) -> None:
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
        slock_engine_manager=None,
    )

    assert runtime.hire_readiness().ready is True
    assert runtime.execution_readiness().blockers == ("slock_gateway",)
    runtime.close()


def test_task7_execution_readiness_requires_environment_provider(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
        employee_environment_provider=None,
    )

    assert runtime.hire_readiness().ready is True
    assert runtime.execution_readiness().blockers == ("employee_environment",)
    runtime.close()


def test_phase4_execution_readiness_requires_employee_card_update_capability(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    channels.update_card = None  # type: ignore[method-assign]
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
    )

    assert runtime.hire_readiness().ready is True
    assert runtime.execution_readiness().blockers == ("employee_outbox",)
    runtime.close()


def test_task7_recovers_committed_attempts_before_starting_dispatch(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    with (
        patch.object(
            EmployeeDispatchCoordinator,
            "recover_incomplete_attempts",
            autospec=True,
            side_effect=lambda _self: events.append("recover") or (),
        ),
        patch.object(
            EmployeeDepartmentRuntime,
            "_start_dispatch_worker",
            autospec=True,
            side_effect=lambda _self: events.append("start"),
        ),
    ):
        runtime = _runtime(
            _settings(tmp_path, limit=1, context_configured=True),
            release_evidence_ready=True,
            registrar=_Registrar(),
            channel_supervisor=_Channels(),
            slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
            notification_link=lambda *_: None,
            context_source_factory=_ContextSourceFactory(),
            group_memory_backend=_GroupMemory(),
        )

    assert events == ["recover", "start"]
    runtime.close()


def test_task7_execution_recovery_failure_is_a_stable_blocker(
    tmp_path: Path,
) -> None:
    with patch.object(
        EmployeeDispatchCoordinator,
        "recover_incomplete_attempts",
        autospec=True,
        side_effect=RuntimeError("injected recovery failure"),
    ):
        runtime = _runtime(
            _settings(tmp_path, limit=1, context_configured=True),
            release_evidence_ready=True,
            registrar=_Registrar(),
            channel_supervisor=_Channels(),
            slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
            notification_link=lambda *_: None,
            context_source_factory=_ContextSourceFactory(),
            group_memory_backend=_GroupMemory(),
        )

    assert runtime.hire_readiness().ready is True
    assert runtime.execution_readiness().blockers == ("employee_recovery",)
    assert runtime._dispatch_thread is None
    runtime.close()


def test_task7_stuck_dispatch_holds_journal_and_vault_open(tmp_path: Path) -> None:
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
    )
    assert runtime._dispatch_thread is not None
    runtime._dispatch_stop.set()
    runtime._dispatch_thread.join(timeout=1)

    class _StuckThread:
        alive = True

        def join(self, timeout: float) -> None:
            assert timeout == 5.0

        def is_alive(self) -> bool:
            return self.alive

    stuck = _StuckThread()
    runtime._dispatch_thread = stuck  # type: ignore[assignment]
    assert runtime._writer is not None
    assert runtime._vault is not None

    runtime.close()

    assert runtime._writer._closed is False
    assert runtime._vault._root_finalizer.alive is True
    stuck.alive = False
    runtime.close()
    assert runtime._writer._closed is True
    assert runtime._vault._root_finalizer.alive is False


def test_task7_runtime_routes_anchored_inbox_into_owned_queue(tmp_path: Path) -> None:
    channels = _Channels()
    settings = _settings(tmp_path, limit=1, context_configured=True)
    settings.allowed_chat_ids = frozenset({"oc_employee_team"})
    runtime = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
        membership_health=_HealthyMembership(),
    )
    active = _activate_employee(runtime, channels)
    channels.statuses[active.agent_id].tenant_key = active.tenant_key
    channels.statuses[active.agent_id].bot_principal_id = active.bot_principal_id
    assert runtime._writer is not None
    assert runtime.hire_service is not None
    assert runtime.ingress_service is not None
    assert runtime.ingress_router is not None
    commit_workforce_events(
        runtime._writer,
        runtime.hire_service.projection_state,
        (
            JournalEvent(
                event_type="employee.membership_changed",
                aggregate_id=active.agent_id,
                payload={"member_groups": ["oc_employee_team"]},
            ),
        ),
    )
    runtime._dispatch_stop.set()
    assert runtime._dispatch_thread is not None
    runtime._dispatch_thread.join(timeout=1)

    class _NoDispatch:
        def dispatch_next(self):
            return None

    runtime._dispatch = _NoDispatch()  # type: ignore[assignment]
    suffix = hashlib.sha256(b"composition-route").hexdigest()
    payload = EmployeeIngressPayload(
        schema_version=1,
        envelope_id=f"ing_{suffix}",
        normalized_parts=(
            {
                "type": "message",
                "message_type": "text",
                "chat_type": "group",
                "content": {"text": "route me"},
                "sender_id": "ou_admin",
                "sender_id_type": "open_id",
                "sender_type": "user",
                "sender_tenant_key": active.tenant_key,
                "feishu_thread_id": "omt_composition",
            },
        ),
        attachment_descriptors=(),
    )
    metadata = EmployeeIngressMetadata(
        schema_version=1,
        envelope_id=payload.envelope_id,
        tenant_key=active.tenant_key,
        agent_id=active.agent_id,
        bot_principal_id=active.bot_principal_id,
        app_id=active.app_id,
        channel_generation=active.channel_generation,
        connection_id=active.channel_connection_id,
        event_id=f"evt_{suffix[:24]}",
        message_id=f"om_{suffix[:24]}",
        event_type="im.message.receive_v1",
        action_identity="",
        chat_id="oc_employee_team",
        thread_root_message_id="om_runtime_root",
        sender_principal_id="ou_admin",
        received_at="2026-07-14T00:00:00Z",
        semantic_digest=payload.payload_sha256,
        payload_sha256=payload.payload_sha256,
        payload_size_bytes=payload.canonical_size_bytes,
        attachment_count=0,
        attachment_total_bytes=0,
    )
    acceptance = runtime.ingress_service.accept(
        metadata,
        payload,
        request_id="req_composition_route",
    )

    assert runtime._drain_employee_dispatch_once() is True
    acceptance_id = acceptance.acceptance.acceptance_id
    routed = runtime.ingress_router.state.by_acceptance_id[acceptance_id]
    assert routed.state == "queued", routed.reason_code
    assert runtime.ingress_service.state.by_acceptance_id[acceptance_id].disposition is None
    runtime._dispatch_thread = None
    runtime.close()


def _activate_employee(
    runtime: EmployeeDepartmentRuntime,
    channels: _Channels,
) -> DurableHireState:
    assert runtime.hire_service is not None
    admitted = runtime.hire_service.start_hire(_request())
    deadline = time.monotonic() + 5
    pending = None
    while time.monotonic() < deadline:
        pending = runtime.hire_service.get_state(admitted.intent_id)
        if pending is not None and pending.phase is HirePhase.READY_PENDING_VERIFICATION:
            break
        time.sleep(0.02)
    assert pending is not None
    callback = channels.callbacks[pending.agent_id]
    callback(
        {
            "event": "rawMessageMeta",
            "data": {
                "event_id": "evt_context_ready",
                "tenant_key": "tenant-a",
                "message_id": "om_context_ready",
            },
        }
    )
    callback(
        {
            "event": "message",
            "data": {
                "id": "om_context_ready",
                "content_text": "/status",
                "conversation": {"chat_type": "p2p"},
                "sender": {"open_id": "ou_admin"},
                "raw": {},
            },
        }
    )
    active = None
    while time.monotonic() < deadline:
        active = runtime.hire_service.get_state(admitted.intent_id)
        if active is not None and active.phase is HirePhase.ACTIVE:
            return active
        time.sleep(0.02)
    raise AssertionError("employee did not become ACTIVE")


def test_production_factory_has_no_boolean_release_bypass() -> None:
    assert "release_evidence_ready" not in inspect.signature(EmployeeDepartmentRuntime.from_settings).parameters
    assert "release_trust_provider" in inspect.signature(EmployeeDepartmentRuntime.from_settings).parameters
    assert "resume_external" not in inspect.signature(EmployeeDepartmentRuntime.recover).parameters


class _ExternalTrustProvider:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ExternalTrustSession:
    def __init__(self) -> None:
        self.closed = False
        self.expired = False
        self.anchors: dict[str, MemoryAnchor] = {}
        self.audit_sequence = 0

    def valid(self, _now: float) -> bool:
        return not self.expired

    def renew_if_needed(self, _now: float, *, renewal_window_seconds: float) -> bool:
        assert renewal_window_seconds == 120.0
        return not self.expired

    def close(self) -> None:
        self.closed = True

    def journal_anchor(self, scope: str):
        assert scope in {
            "employee-journal:ghostap-prod-a",
            "main-bot-audit:ghostap-prod-a",
        }
        anchor = self.anchors.setdefault(scope, MemoryAnchor())
        anchor.production_safe = True
        return anchor

    def record_main_bot_send_attempt(self, **_kwargs) -> None:
        self.audit_sequence += 1

    def count_main_bot_send_attempts(self, _tenant_key, _start, _end) -> int:
        return self.audit_sequence


def test_external_release_session_is_owned_and_closed_by_runtime(tmp_path: Path) -> None:
    provider = _ExternalTrustProvider()
    session = _ExternalTrustSession()
    with patch(
        "src.autonomous.provisioning.composition.authorize_runtime_employee_release",
        return_value=session,
    ):
        runtime = EmployeeDepartmentRuntime.from_settings(
            _settings(tmp_path, limit=1),
            release_trust_provider=provider,
            registrar=_Registrar(),
            channel_supervisor=_Channels(),
            slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
            notification_link=lambda *_: None,
        )

    assert runtime.hire_service is not None
    assert runtime.hire_readiness().ready is True
    assert runtime.main_bot_outbound_audit is not None
    runtime.main_bot_outbound_audit.record_attempt(
        "tenant-a",
        "reply",
        "om-main-bot",
        attempted_at=time.time(),
    )
    assert session.audit_sequence == 1
    assert provider.closed is False
    runtime.close()
    assert session.closed is True


def test_expired_external_release_session_closes_all_new_admission(tmp_path: Path) -> None:
    provider = _ExternalTrustProvider()
    session = _ExternalTrustSession()
    with patch(
        "src.autonomous.provisioning.composition.authorize_runtime_employee_release",
        return_value=session,
    ):
        runtime = EmployeeDepartmentRuntime.from_settings(
            _settings(tmp_path, limit=1, context_configured=True),
            release_trust_provider=provider,
            main_bot_send_audit=lambda *_: 0,
            registrar=_Registrar(),
            channel_supervisor=_Channels(),
            slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
            notification_link=lambda *_: None,
            context_source_factory=_ContextSourceFactory(),
            group_memory_backend=_GroupMemory(),
            slock_engine_manager=_SlockManager(),
            employee_environment_provider=lambda authority: EmployeeProcessEnvironmentMaterial(
                authority.tenant_key,
                authority.agent_id,
                authority.employee_version,
                authority.credential_ref,
                {"PATH": "/usr/bin"},
                {},
            ),
        )
    session.expired = True

    asyncio.run(runtime._renew_release_trust())

    assert runtime.hire_readiness().blockers == ("release_trust",)
    assert runtime.execution_readiness().blockers == ("release_trust",)
    assert runtime.hire_service is not None
    assert "admission_closed" in runtime.hire_service.readiness().blockers
    runtime.close()


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


def test_context_configuration_failure_does_not_block_first_hire(
    tmp_path: Path,
) -> None:
    runtime = _runtime(
        _settings(tmp_path, limit=1),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
    )

    assert runtime.context_service is None
    assert runtime.hire_readiness().ready is True
    assert runtime.execution_readiness().blockers == ("employee_ingress",)
    assert runtime.hire_service is not None
    assert runtime.hire_service.start_hire(_request()).intent_id
    runtime.close()


def test_active_employee_requires_employee_scoped_context_probe_and_same_head(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    source_factory = _ContextSourceFactory()
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=source_factory,
        group_memory_backend=_GroupMemory(),
    )
    assert runtime.context_service is not None
    assert runtime.data_composition is not None
    assert runtime.data_composition.document_materializer.root == (tmp_path / "slock" / "agents")
    active = _activate_employee(runtime, channels)

    readiness = runtime.execution_readiness(active.agent_id)

    assert readiness.ready is True
    assert source_factory.probed == [(active.agent_id, active.app_id, active.credential_ref)]
    data_head = runtime.data_composition.service.get_head()
    workforce = runtime.hire_service.projection_state  # type: ignore[union-attr]
    assert (data_head.sequence, data_head.logical_hash) == (
        workforce.cursor_sequence,
        workforce.cursor_hash,
    )
    runtime.invalidate_employee_context(active.agent_id)
    assert source_factory.invalidated == [active.agent_id]
    runtime.reactivate_employee_context(active.agent_id)
    receipt = runtime.rewrap_employee_credential(
        agent_id=active.agent_id,
        app_id=active.app_id,
        credential_ref=active.credential_ref,
    )
    assert receipt.credential_ref == active.credential_ref
    assert source_factory.invalidated == [active.agent_id, active.agent_id]
    assert source_factory.reactivated == [active.agent_id, active.agent_id]
    runtime.close()
    assert source_factory.closed is True


def test_active_employee_probe_failure_blocks_execution_not_hiring(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    source_factory = _ContextSourceFactory(probe_ready=False)
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=source_factory,
        group_memory_backend=_GroupMemory(),
    )
    active = _activate_employee(runtime, channels)

    assert runtime.hire_readiness().ready is True
    readiness = runtime.execution_readiness(active.agent_id)
    assert readiness.ready is False
    assert readiness.blockers == ("context_credentials",)
    runtime.close()


def test_execution_readiness_fails_closed_when_projection_sync_raises(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
    )
    active = _activate_employee(runtime, channels)
    assert runtime.hire_service is not None

    def fail_sync():
        raise RuntimeError("injected projection replay failure")

    runtime.hire_service.synchronize_projection = fail_sync  # type: ignore[method-assign]

    readiness = runtime.execution_readiness(active.agent_id)
    assert readiness.ready is False
    assert readiness.blockers == ("employee_context",)
    runtime.close()


def test_runtime_owned_context_service_assembles_authorized_snapshot(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    source_factory = _AssemblingContextSourceFactory()
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=source_factory,
        group_memory_backend=_GroupMemory(),
    )
    active = _activate_employee(runtime, channels)
    assert runtime._writer is not None
    assert runtime.hire_service is not None
    commit_workforce_events(
        runtime._writer,
        runtime.hire_service.projection_state,
        (
            JournalEvent(
                event_type="employee.membership_changed",
                aggregate_id=active.agent_id,
                payload={"member_groups": ["oc_employee_team"]},
            ),
        ),
    )
    assert runtime.context_service is not None
    snapshot = runtime.context_service.assemble(
        AuthorizedContextRequest(
            tenant_key=active.tenant_key,
            agent_id=active.agent_id,
            bot_principal_id=active.bot_principal_id,
            app_id=active.app_id,
            channel_generation=active.channel_generation,
            chat_id="oc_employee_team",
            thread_root_message_id="om_runtime_root",
            feishu_thread_id="omt_runtime",
            current_message_id="om_runtime_current",
            requester_principal_id="ou_admin",
        )
    )

    assert [item.text for item in snapshot.thread_messages] == [
        "root",
        "current",
    ]
    assert snapshot.thread_messages[-1].is_current is True
    runtime.close()


def test_recovery_invalidates_employee_context_after_durable_retirement(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    source_factory = _ContextSourceFactory()
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=source_factory,
        group_memory_backend=_GroupMemory(),
    )
    active = _activate_employee(runtime, channels)
    assert runtime._writer is not None
    assert runtime.hire_service is not None

    commit_workforce_events(
        runtime._writer,
        runtime.hire_service.projection_state,
        (
            JournalEvent(
                event_type="employee.state_changed",
                aggregate_id=active.agent_id,
                payload={"state": HirePhase.ARCHIVED.value},
            ),
        ),
    )
    runtime.recover()

    assert source_factory.invalidated == [active.agent_id]
    assert runtime.execution_readiness(active.agent_id).blockers == ("employee_not_active",)
    runtime.close()


def test_explicit_context_invalidation_cannot_race_with_projection_reactivation(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    source_factory = _ContextSourceFactory()
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=source_factory,
        group_memory_backend=_GroupMemory(),
    )
    active = _activate_employee(runtime, channels)
    assert runtime.hire_service is not None
    source_factory.invalidate_employee(active.agent_id)
    runtime._context_projection_invalidations.add(active.agent_id)
    reactivate_entered = threading.Event()
    release_reactivate = threading.Event()
    explicit_done = threading.Event()
    refresh_done = threading.Event()
    original_reactivate = source_factory.reactivate_employee

    def blocking_reactivate(agent_id: str) -> None:
        reactivate_entered.set()
        assert release_reactivate.wait(2)
        original_reactivate(agent_id)

    source_factory.reactivate_employee = blocking_reactivate  # type: ignore[method-assign]
    refresher = threading.Thread(
        target=lambda: (
            runtime._refresh_context_bindings(
                runtime.hire_service.projection_state,
            ),
            refresh_done.set(),
        )
    )
    invalidator = threading.Thread(
        target=lambda: (
            runtime.invalidate_employee_context(active.agent_id),
            explicit_done.set(),
        )
    )

    refresher.start()
    assert reactivate_entered.wait(2)
    invalidator.start()
    assert not explicit_done.wait(0.05)
    release_reactivate.set()
    refresher.join(2)
    invalidator.join(2)

    assert not refresher.is_alive()
    assert not invalidator.is_alive()
    assert refresh_done.is_set()
    assert explicit_done.is_set()
    assert source_factory.reactivated == [active.agent_id]
    assert source_factory.invalidated == [active.agent_id, active.agent_id]
    assert active.agent_id in runtime._context_explicit_invalidations
    runtime.close()


def test_missing_requester_authority_blocks_before_employee_probe(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1, context_configured=True)
    settings.autonomous_manager_acl = ""
    settings.admin_user_ids = frozenset()
    channels = _Channels()
    source_factory = _ContextSourceFactory()
    runtime = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=source_factory,
        group_memory_backend=_GroupMemory(),
    )
    active = _activate_employee(runtime, channels)

    readiness = runtime.execution_readiness(active.agent_id)

    assert readiness.ready is False
    assert readiness.blockers == ("context_request_authority",)
    assert source_factory.probed == []
    runtime.close()


def test_runtime_closes_context_before_channels_data_writer_and_vault(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    source_factory = _ContextSourceFactory()
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=source_factory,
        group_memory_backend=_GroupMemory(),
    )
    assert runtime.context_service is not None
    assert runtime.data_composition is not None
    assert runtime.hire_service is not None
    events: list[str] = []

    runtime.context_service.stop_admission = lambda: events.append("context_admission")  # type: ignore[method-assign]
    runtime.context_service.drain = lambda: events.append("context_drain")  # type: ignore[method-assign]
    source_factory.close = lambda: events.append("context_sources")  # type: ignore[method-assign]

    channel_attempts = 0

    def failing_channels_close():
        nonlocal channel_attempts
        channel_attempts += 1
        events.append("channels")
        if channel_attempts == 1:
            raise RuntimeError("injected close failure")

    channels.close = failing_channels_close  # type: ignore[method-assign]
    original_data_close = runtime.data_composition.close
    runtime.data_composition.close = lambda: (events.append("data"), original_data_close())[-1]  # type: ignore[method-assign]
    original_service_close = runtime.hire_service.close
    runtime.hire_service.close = lambda: (events.append("writer"), original_service_close())[-1]  # type: ignore[method-assign]
    original_vault_close = runtime._vault.close
    runtime._vault.close = lambda: (events.append("vault"), original_vault_close())[-1]  # type: ignore[method-assign,union-attr]

    runtime.close()

    assert events == [
        "context_admission",
        "context_drain",
        "context_sources",
        "channels",
    ]
    runtime.close()
    assert events == [
        "context_admission",
        "context_drain",
        "context_sources",
        "channels",
        "context_admission",
        "context_drain",
        "context_sources",
        "channels",
        "data",
        "writer",
        "vault",
    ]


def test_hire_and_data_writes_resynchronize_shared_journal_heads(
    tmp_path: Path,
) -> None:
    channels = _Channels()
    runtime = _runtime(
        _settings(tmp_path, limit=2, context_configured=True),
        release_evidence_ready=True,
        registrar=_DistinctRegistrar(),
        channel_supervisor=channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
    )
    active = _activate_employee(runtime, channels)
    assert runtime.data_composition is not None
    assert runtime.hire_service is not None
    employee = runtime.hire_service.projection_state.employees[active.agent_id]

    runtime.data_composition.publish_document(
        PublishEmployeeDocumentCommand(
            agent_id=active.agent_id,
            tenant_key=active.tenant_key,
            owner_principal_id=employee.owner_principal_id,
            kind=DataKind.L1_MEMORY,
            source_id="l1_memory",
            content=b"safe memory",
            content_type="text/markdown",
        )
    )
    second_request = EmployeeHireRequest(
        employee_name="Vega",
        tool="codex",
        model="gpt-5.6-sol",
        effort="high",
        chat_id="oc_admin_dm",
        message_id="om_composition_second",
        requester_principal_id="ou_admin",
        tenant_key="tenant-a",
    )
    attempt = ExecutionAttemptContext(
        tenant_key=active.tenant_key,
        agent_id=active.agent_id,
        owner_principal_id=employee.owner_principal_id,
        requester_principal_id="ou_admin",
        task_id="task_projection_sync",
        run_id="run_projection_sync",
        attempt_id="attempt_projection_sync",
        message_id="om_projection_sync",
        thread_root_id="om_projection_sync",
        chat_id=active.chat_id,
        tool=active.tool,
        model=active.model,
        effort=active.effort,
        started_at="2026-07-13T00:00:00+00:00",
        terminal_epoch=1,
    )
    barrier = threading.Barrier(3)
    errors: list[Exception] = []
    hired: list[DurableHireState] = []

    def start_second_hire() -> None:
        barrier.wait()
        try:
            hired.append(runtime.hire_service.start_hire(second_request))
        except Exception as exc:
            errors.append(exc)

    def start_data_attempt() -> None:
        barrier.wait()
        try:
            runtime.data_composition.service.start_attempt(attempt)
        except Exception as exc:
            errors.append(exc)

    workers = [
        threading.Thread(target=start_second_hire),
        threading.Thread(target=start_data_attempt),
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(5)
    assert all(not worker.is_alive() for worker in workers)
    assert errors == []
    assert len(hired) == 1
    second = hired[0]
    assert second.agent_id != active.agent_id
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        second = runtime.hire_service.get_state(second.intent_id)
        if second is not None and second.phase in {
            HirePhase.READY_PENDING_VERIFICATION,
            HirePhase.ACTION_REQUIRED,
        }:
            break
        time.sleep(0.02)
    assert second is not None
    assert second.phase is HirePhase.READY_PENDING_VERIFICATION
    runtime.hire_service.commit_effect_transition(
        active.intent_id,
        effect_id="projection-sync-check",
        effect_type="projection_sync_check",
        next_state=HireEffectState.PREPARED,
    )
    runtime.hire_service.recover()
    runtime.data_composition.service.rebuild_projection()
    data_head = runtime.data_composition.service.get_head()
    workforce = runtime.hire_service.projection_state
    assert (data_head.sequence, data_head.logical_hash) == (
        workforce.cursor_sequence,
        workforce.cursor_hash,
    )
    runtime.close()


def test_source_close_failure_holds_writer_and_vault_until_retry(
    tmp_path: Path,
) -> None:
    source_factory = _ContextSourceFactory()
    runtime = _runtime(
        _settings(tmp_path, limit=1, context_configured=True),
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=source_factory,
        group_memory_backend=_GroupMemory(),
    )
    assert runtime.hire_service is not None
    events: list[str] = []
    original_service_close = runtime.hire_service.close
    runtime.hire_service.close = lambda: (events.append("writer"), original_service_close())[-1]  # type: ignore[method-assign]
    original_vault_close = runtime._vault.close
    runtime._vault.close = lambda: (events.append("vault"), original_vault_close())[-1]  # type: ignore[method-assign,union-attr]
    source_factory.close = lambda: (_ for _ in ()).throw(RuntimeError("blocked"))  # type: ignore[method-assign]

    runtime.close()

    assert events == []
    source_factory.close = lambda: events.append("sources")  # type: ignore[method-assign]
    runtime.close()
    assert events == ["sources", "writer", "vault"]


def test_context_binding_and_probe_recover_after_restart_reverification(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1, context_configured=True)
    settings.autonomous_thread_context_max_chars = 22
    first_channels = _Channels()
    first_source = _ReplayContextSourceFactory(split_pages=True)
    group_memory = _StableGroupMemory("l2-memory")
    first = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=first_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=first_source,
        group_memory_backend=group_memory,
    )
    active = _activate_employee(first, first_channels)
    assert first._writer is not None
    assert first.hire_service is not None
    commit_workforce_events(
        first._writer,
        first.hire_service.projection_state,
        (
            JournalEvent(
                event_type="employee.membership_changed",
                aggregate_id=active.agent_id,
                payload={"member_groups": ["oc_employee_team"]},
            ),
        ),
    )
    assert first.data_composition is not None
    employee = first.hire_service.synchronize_projection().employees[active.agent_id]
    first.data_composition.publish_document(
        PublishEmployeeDocumentCommand(
            agent_id=active.agent_id,
            tenant_key=active.tenant_key,
            owner_principal_id=employee.owner_principal_id,
            kind=DataKind.L1_MEMORY,
            source_id="l1_memory",
            content=b"l1-memory",
            content_type="text/markdown",
        )
    )
    assert (
        first.data_composition.memory_facade.read_l1(
            active.agent_id,
            active.tenant_key,
            allow_unscoped_legacy=False,
        )
        == "l1-memory"
    )
    context_request = AuthorizedContextRequest(
        tenant_key=active.tenant_key,
        agent_id=active.agent_id,
        bot_principal_id=active.bot_principal_id,
        app_id=active.app_id,
        channel_generation=active.channel_generation,
        chat_id="oc_employee_team",
        thread_root_message_id="om_runtime_root",
        feishu_thread_id="omt_runtime",
        current_message_id="om_runtime_current",
        requester_principal_id="ou_admin",
    )
    assert first.context_service is not None
    before_restart = first.context_service.assemble(context_request)
    first.close()

    restarted_channels = _Channels()
    restarted_source = _ReplayContextSourceFactory(split_pages=False)
    restarted = _runtime(
        settings,
        release_evidence_ready=True,
        channel_supervisor=restarted_channels,
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=restarted_source,
        group_memory_backend=_StableGroupMemory("l2-memory"),
    )
    assert restarted.hire_service is not None
    deadline = time.monotonic() + 7
    pending = None
    while time.monotonic() < deadline:
        pending = restarted.hire_service.get_state(active.intent_id)
        if (
            pending is not None
            and pending.phase is HirePhase.READY_PENDING_VERIFICATION
            and pending.channel_generation == 2
        ):
            break
        time.sleep(0.05)
    assert pending is not None
    callback = restarted_channels.callbacks[pending.agent_id]
    callback(
        {
            "event": "rawMessageMeta",
            "data": {
                "event_id": "evt_context_restart",
                "tenant_key": pending.tenant_key,
                "message_id": "om_context_restart",
            },
        }
    )
    callback(
        {
            "event": "message",
            "data": {
                "id": "om_context_restart",
                "content_text": "/status",
                "conversation": {"chat_type": "p2p"},
                "sender": {"open_id": "ou_admin"},
                "raw": {},
            },
        }
    )
    recovered = None
    while time.monotonic() < deadline:
        recovered = restarted.hire_service.get_state(active.intent_id)
        if recovered is not None and recovered.phase is HirePhase.ACTIVE:
            break
        time.sleep(0.02)
    assert recovered is not None
    assert restarted.execution_readiness(recovered.agent_id).ready is True
    assert restarted_source.probed == [(recovered.agent_id, recovered.app_id, recovered.credential_ref)]
    assert restarted.data_composition is not None
    data_head = restarted.data_composition.service.get_head()
    projection = restarted.hire_service.projection_state
    assert (data_head.sequence, data_head.logical_hash) == (
        projection.cursor_sequence,
        projection.cursor_hash,
    )
    assert restarted.context_service is not None
    after_restart = restarted.context_service.assemble(
        replace(
            context_request,
            channel_generation=recovered.channel_generation,
        )
    )
    assert after_restart.snapshot_hash == before_restart.snapshot_hash
    assert after_restart.watermark == before_restart.watermark
    assert after_restart.thread_messages == before_restart.thread_messages
    assert after_restart.group_messages == before_restart.group_messages
    assert after_restart.layer_metrics == before_restart.layer_metrics
    assert after_restart.trimming_trace == before_restart.trimming_trace
    assert [record.layer for record in after_restart.trimming_trace] == [
        ContextLayer.L2_GROUP,
        ContextLayer.L1_MEMORY,
        ContextLayer.GROUP_RECENT,
    ]
    assert after_restart.l1_summary == ""
    assert after_restart.l2_summary == ""
    assert after_restart.group_messages == ()
    deleted = next(message for message in after_restart.thread_messages if message.message_id == "om_runtime_deleted")
    assert deleted.deleted is True
    assert deleted.text == ""
    assert after_restart.thread_messages[0].edited is True
    assert after_restart.thread_messages[0].text == "root-edited"
    restarted.close()


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


def test_existing_journal_with_revoked_release_stays_dormant_not_miscomposed(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1, context_configured=True)
    initial = _runtime(
        settings,
        release_evidence_ready=True,
        registrar=_Registrar(),
        channel_supervisor=_Channels(),
        slash_reconciler_factory=lambda _app_id, _secret: _Slash(),
        notification_link=lambda *_: None,
        context_source_factory=_ContextSourceFactory(),
        group_memory_backend=_GroupMemory(),
    )
    initial.close()

    restarted = _runtime(settings, release_evidence_ready=False)

    assert restarted.hire_service is not None
    assert restarted.hire_readiness().blockers == ("release_evidence",)
    assert restarted.execution_readiness().blockers == ("release_evidence",)
    restarted.close()


def test_settings_driven_release_evaluator_defaults_to_pending(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, limit=1)
    settings.autonomous_employee_release_id = ""
    settings.autonomous_employee_commit_sha = ""
    settings.autonomous_employee_service_instance_id = ""
    settings.autonomous_employee_staging_tenant_hash = ""
    settings.autonomous_employee_production_tenant_hash = ""
    settings.autonomous_employee_release_evidence_bundle = str(tmp_path / "missing-evidence.jsonl")
    settings.autonomous_employee_release_checkpoint = str(tmp_path / "missing-checkpoint.json")

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
    assert channels.started == [(state.agent_id, state.app_id, state.credential_ref, 1)]
    event_types = [event.event_type for frame in runtime.journal_frames() for event in frame.events]
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
            and restarted.readiness().ready is True
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
    callback(
        {  # type: ignore[operator]
            "event": "rawMessageMeta",
            "data": {
                "event_id": "evt_forged",
                "tenant_key": "tenant-a",
                "message_id": "om_forged_status",
            },
        }
    )
    callback(
        {  # type: ignore[operator]
            "event": "message",
            "data": {
                "id": "om_forged_status",
                "content_text": "/status",
                "conversation": {"chat_type": "p2p"},
                "sender": {"open_id": "ou_admin"},
                "raw": {},
            },
        }
    )
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
            and failed.effect_state("slash-reconcile:1:1") is HireEffectState.EXECUTING
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
    callback(
        {  # type: ignore[operator]
            "event": "rawMessageMeta",
            "data": {"event_id": "evt_1", "tenant_key": "tenant-a", "message_id": "om_1"},
        }
    )
    callback(
        {  # type: ignore[operator]
            "event": "message",
            "data": {
                "id": "om_1",
                "content_text": "/status",
                "conversation": {"chat_type": "p2p"},
                "sender": {"open_id": "ou_admin"},
                "raw": {},
            },
        }
    )
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
    callback(
        {  # type: ignore[operator]
            "event": "rawMessageMeta",
            "data": {
                "event_id": "evt_restart",
                "tenant_key": "tenant-a",
                "message_id": "om_restart",
            },
        }
    )
    callback(
        {  # type: ignore[operator]
            "event": "message",
            "data": {
                "id": "om_restart",
                "content_text": "/status",
                "conversation": {"chat_type": "p2p"},
                "sender": {"open_id": "ou_admin"},
                "raw": {},
            },
        }
    )
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
            and crashed.effect_state("slash-reconcile:2:1") is HireEffectState.COMMITTED
            and crashed.effect_state("channel-start:2") is HireEffectState.ACTION_REQUIRED
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
            and crashed.effect_state("channel-start:1") is HireEffectState.COMMITTED
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
