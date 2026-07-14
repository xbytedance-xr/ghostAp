"""Single-owner production composition for durable visible employee hiring."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import lark_oapi as lark

from src.slock_engine.memory_manager import (
    MemoryManager,
    default_slock_storage_base,
)
from src.utils.path import canonicalize_user_home_path

from ..acceptance.main_bot_audit import MainBotSendAuditLog
from ..acceptance.release_trust import ReleaseTrustProvider
from ..context.lark_source import LarkEmployeeMessageSourceFactory
from ..context.models import ThreadContextConfig
from ..context.runtime import (
    RuntimeEmployeeGenerationAuthority,
    parse_requester_acl,
)
from ..context.service import AuthorizedGroupMemoryReader, EmployeeContextService
from ..context.source import EmployeeMessageSourceFactory
from ..data.composition import (
    EmployeeDataComposition,
    build_employee_data_composition,
)
from ..data.keyring import EmployeeDataKeyring
from ..data.ports import HistoryQuerySpec, MemoryQuerySpec
from ..data.query import AuthenticatedDataRequest, EmployeeDataSubject, QueryDeniedError
from ..domain import EmployeeState
from ..gateway.coordinator import EmployeeDispatchCoordinator
from ..gateway.env_scope import (
    EmployeeEnvironmentAuthority,
    EmployeeProcessEnvironmentMaterial,
)
from ..ingress.attachments import AttachmentStagingService
from ..ingress.projection import IngressProjectionState
from ..ingress.router import DurableEmployeeIngressRouter, RouterQueueLimits
from ..ingress.service import EmployeeIngressService, IngressConflictError
from ..journal.anchor import FileAnchor
from ..journal.projections import ProjectionState
from ..journal.writer import JournalWriter
from ..membership import (
    EmployeeMembershipService,
    LarkMembershipAPI,
    MembershipBindingError,
)
from ..outbox.delivery import (
    EmployeeDeliveryAuthority,
    EmployeeOutboxDeliveryCoordinator,
)
from ..outbox.lifecycle import EmployeeOutboxLifecycle
from ..outbox.projection import OutboxProjectionState
from ..outbox.service import EmployeeOutboxService
from ..supervisor.employee_channels import (
    ChannelProcessState,
    EmployeeChannelSupervisor,
)
from ..workforce.credential_vault import CredentialReceipt, CredentialVault
from ..workforce.registry import ProjectedAgentRegistry
from .fire_authority import JournalFireAuthority
from .fire_effects import (
    AtomicEmployeeArchive,
    ChannelStopEffect,
    CredentialDestroyEffect,
    ExecutionQuiesceEffect,
    MembershipCleanupEffect,
    SlashCleanupEffect,
)
from .fire_service import EmployeeFireService
from .hire_service import HireReadiness, ProductionEmployeeHireService
from .hire_state import DurableHireState, HireEffectState, HirePhase
from .lark_app import LarkAppRegistrar
from .local_bootstrap import resolve_employee_runtime_material
from .slash_commands import SlashCommandReconciler, VerifiedSlashState
from .slash_lark import LarkSlashCommandAPI
from .verification import (
    ChannelVerificationEvidence,
    SlashVerificationEvidence,
    TenantIngressEvidence,
    VerificationBinding,
    VerificationChallenge,
    VerificationCoordinates,
    VerificationOutcome,
    VerificationRouter,
)

logger = logging.getLogger(__name__)


def _zero_main_bot_send_count(_tenant: str, _start: float, _end: float) -> int:
    return 0


class _ChannelSupervisor(Protocol):
    def start(
        self,
        agent_id: str,
        app_id: str,
        credential_ref: str,
        generation: int,
        on_event: Callable[[dict[str, Any]], None],
    ) -> Any: ...

    def status(self, agent_id: str) -> Any: ...

    def send(
        self,
        agent_id: str,
        *,
        generation: int,
        target: str,
        message: Any,
        options: Any = None,
    ) -> Any: ...

    def update_card(
        self,
        agent_id: str,
        *,
        generation: int,
        message_id: str,
        card: dict[str, Any],
    ) -> Any: ...

    def close(self) -> None: ...


class _SlashReconciler(Protocol):
    async def reconcile(self) -> VerifiedSlashState: ...

    async def cleanup(self) -> VerifiedSlashState: ...

    async def observe_empty(self) -> bool: ...


SlashReconcilerFactory = Callable[[str, str], _SlashReconciler]
MainBotSendAudit = Callable[[str, float, float], int]
EmployeeEnvironmentProvider = Callable[
    [EmployeeEnvironmentAuthority],
    EmployeeProcessEnvironmentMaterial,
]


class _SlockMembershipHealth:
    """Treat an unavailable or ambiguous activated Slock as degraded."""

    def __init__(self, manager: object) -> None:
        self._manager = manager

    def is_degraded(self, _agent_id: str, team_id: str) -> bool:
        try:
            self._manager.resolve_employee_engine(chat_id=team_id)
        except Exception:
            return True
        return False


@dataclass(frozen=True)
class RuntimeReadiness:
    ready: bool
    blockers: tuple[str, ...]


class EmployeeDepartmentRuntime:
    """Own Journal, Vault, Saga, Channel children and the activity loop."""

    def __init__(
        self,
        *,
        blockers: tuple[str, ...] = (),
        runtime_enabled: bool = False,
    ) -> None:
        self._blockers = blockers
        self._runtime_enabled = runtime_enabled is True
        self._service: ProductionEmployeeHireService | None = None
        self._writer: JournalWriter | None = None
        self._vault: CredentialVault | None = None
        self._data_keyring: EmployeeDataKeyring | None = None
        self._channels: _ChannelSupervisor | None = None
        self._data: EmployeeDataComposition | None = None
        self._ingress: EmployeeIngressService | None = None
        self._router: DurableEmployeeIngressRouter | None = None
        self._attachments: AttachmentStagingService | None = None
        self._dispatch: EmployeeDispatchCoordinator | None = None
        self._outbox: EmployeeOutboxService | None = None
        self._outbox_delivery: EmployeeOutboxDeliveryCoordinator | None = None
        self._outbox_lifecycle: EmployeeOutboxLifecycle | None = None
        self._membership: EmployeeMembershipService | None = None
        self._fire: EmployeeFireService | None = None
        self._dispatch_thread: threading.Thread | None = None
        self._dispatch_stop = threading.Event()
        self._execution_blockers: tuple[str, ...] = ()
        self._slock_manager: object | None = None
        self._environment_provider: EmployeeEnvironmentProvider | None = None
        self._context_source_factory: EmployeeMessageSourceFactory | None = None
        self._context_service: EmployeeContextService | None = None
        self._context_acl: Any = None
        self._group_memory_backend: Any = None
        self._owns_group_memory_backend = False
        self._context_blockers: tuple[str, ...] = ()
        self._context_bindings: dict[str, tuple[str, str, int]] = {}
        self._context_projection_invalidations: set[str] = set()
        self._context_explicit_invalidations: set[str] = set()
        self._context_binding_lock = threading.RLock()
        self._slash_factory: SlashReconcilerFactory | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()
        self._futures: set[concurrent.futures.Future[Any]] = set()
        self._intent_futures: dict[str, concurrent.futures.Future[Any]] = {}
        self._future_lock = threading.Lock()
        self._closing = False
        self._challenges: dict[str, VerificationChallenge] = {}
        self._verification_router: VerificationRouter | None = None
        self._main_bot_send_audit: MainBotSendAudit | None = None
        self._owned_main_bot_send_audit: MainBotSendAuditLog | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._raw_message_metadata: dict[tuple[str, int, str], tuple[str, str]] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        registrar: Any = None,
        channel_supervisor: _ChannelSupervisor | None = None,
        slash_reconciler_factory: SlashReconcilerFactory | None = None,
        main_bot_send_audit: MainBotSendAudit | None = None,
        release_trust_provider: ReleaseTrustProvider | None = None,
        notification_link: Callable[[DurableHireState, str, int], object] | None = None,
        notification_status: Callable[[DurableHireState, str], object] | None = None,
        context_source_factory: EmployeeMessageSourceFactory | None = None,
        group_memory_backend: Any = None,
        slock_engine_manager: object | None = None,
        employee_environment_provider: EmployeeEnvironmentProvider | None = None,
        membership_health: Any = None,
        manager_client_factory: Callable[[], Any] | None = None,
    ) -> EmployeeDepartmentRuntime:
        limit = getattr(settings, "autonomous_visible_employee_limit", 0)
        if limit == 0:
            if release_trust_provider is not None:
                release_trust_provider.close()
            return cls(blockers=("visible_employee_limit",))
        if notification_link is None:
            if release_trust_provider is not None:
                release_trust_provider.close()
            return cls(blockers=("registration_notifier",))
        if release_trust_provider is not None:
            try:
                release_trust_provider.close()
            except Exception:
                logger.warning("unused employee release provider close failed")

        runtime = cls(runtime_enabled=True)
        try:
            material = resolve_employee_runtime_material(settings)
            credential_root = canonicalize_user_home_path(
                settings.autonomous_credential_dir
            )
            vault = CredentialVault(credential_root, material.credential_keyring)
            writer = JournalWriter.open(
                Path(settings.autonomous_journal_dir).expanduser(),
                anchor=FileAnchor(settings.autonomous_anchor_path),
                hmac_key=material.journal_hmac_key,
                writer_epoch=time.time_ns(),
            )
        except Exception as exc:
            logger.error(
                "employee department durable composition unavailable: %s",
                type(exc).__name__,
            )
            try:
                vault.close()  # type: ignore[possibly-undefined]
            except Exception:
                pass
            return cls(blockers=("durable_configuration",))

        runtime._writer = writer
        runtime._vault = vault
        runtime._data_keyring = material.data_keyring
        runtime._slock_manager = slock_engine_manager
        runtime._environment_provider = employee_environment_provider
        owned_main_bot_send_audit: MainBotSendAuditLog | None = None
        if main_bot_send_audit is None:
            try:
                owned_main_bot_send_audit = MainBotSendAuditLog.open(
                    settings.autonomous_main_bot_audit_dir,
                    anchor_path=settings.autonomous_main_bot_audit_anchor_path,
                    hmac_key=material.journal_hmac_key,
                )
                main_bot_send_audit = owned_main_bot_send_audit.count_attempts
            except Exception as exc:
                logger.warning(
                    "local main Bot send audit unavailable: %s",
                    type(exc).__name__,
                )
                main_bot_send_audit = _zero_main_bot_send_count
        runtime._owned_main_bot_send_audit = owned_main_bot_send_audit
        try:
            runtime._start_loop()
            runtime._compose_execution_storage(settings)
            runtime._channels = channel_supervisor or EmployeeChannelSupervisor(
                secret_resolver=vault.resolve,
                ingress_service=runtime._ingress,
                ingress_binding_resolver=(runtime._resolve_ingress_binding if runtime._ingress is not None else None),
                ingress_ack_timeout=getattr(
                    settings,
                    "autonomous_employee_ingress_ack_timeout_seconds",
                    1.5,
                ),
            )
            runtime._slash_factory = slash_reconciler_factory or cls._default_slash_factory
            runtime._main_bot_send_audit = main_bot_send_audit
            service = ProductionEmployeeHireService(
                writer,
                ProjectionState(),
                visible_employee_limit=limit,
                release_evidence_ready=True,
                credential_keyring_ready=True,
                registrar=registrar or LarkAppRegistrar(),
                credential_vault=vault,
                on_registration_link=notification_link,
                on_registration_status=notification_status,
                provisioning_submitter=runtime._submit_intent,
                runtime_recovery_ready=False,
            )
            runtime._service = service
            runtime._verification_router = VerificationRouter(nonce_consumer=service)
            runtime._compose_membership(
                settings,
                manager_client_factory=manager_client_factory,
            )
            runtime._compose_context(
                settings,
                context_source_factory=context_source_factory,
                group_memory_backend=group_memory_backend,
            )
            runtime._compose_dispatch(
                settings,
                membership_health=membership_health or runtime._membership,
            )
            runtime._compose_fire(settings)
            runtime.recover()
            return runtime
        except Exception as exc:
            logger.error(
                "employee department runtime composition unavailable: %s",
                type(exc).__name__,
            )
            runtime.close()
            return cls(blockers=("runtime_composition",))

    @property
    def hire_service(self) -> ProductionEmployeeHireService | None:
        return self._service

    @property
    def context_service(self) -> EmployeeContextService | None:
        return self._context_service

    @property
    def membership_service(self) -> EmployeeMembershipService | None:
        return self._membership

    @property
    def fire_service(self) -> EmployeeFireService | None:
        return self._fire

    @property
    def data_composition(self) -> EmployeeDataComposition | None:
        return self._data

    @property
    def ingress_service(self) -> EmployeeIngressService | None:
        return self._ingress

    @property
    def ingress_router(self) -> DurableEmployeeIngressRouter | None:
        return self._router

    @property
    def dispatch_coordinator(self) -> EmployeeDispatchCoordinator | None:
        return self._dispatch

    @property
    def outbox_service(self) -> EmployeeOutboxService | None:
        return self._outbox

    @property
    def outbox_delivery(self) -> EmployeeOutboxDeliveryCoordinator | None:
        return self._outbox_delivery

    @property
    def main_bot_outbound_audit(self) -> MainBotSendAuditLog | None:
        return self._owned_main_bot_send_audit

    def readiness(self) -> RuntimeReadiness:
        return self.hire_readiness()

    def hire_readiness(self) -> RuntimeReadiness:
        if self._service is None:
            return RuntimeReadiness(False, self._blockers or ("not_composed",))
        service_readiness: HireReadiness = self._service.readiness()
        return RuntimeReadiness(service_readiness.ready, service_readiness.blockers)

    def execution_readiness(self, agent_id: str | None = None) -> RuntimeReadiness:
        """Probe ACTIVE employee Context without weakening hire readiness."""
        hire = self.hire_readiness()
        if not hire.ready:
            return hire
        if self._execution_blockers:
            return RuntimeReadiness(False, self._execution_blockers)
        if (
            self._ingress is None
            or self._router is None
            or self._dispatch is None
            or self._outbox is None
            or self._outbox_delivery is None
            or self._data is None
            or self._context_service is None
        ):
            return RuntimeReadiness(False, ("employee_gateway",))
        if self._channels is None or not callable(getattr(self._channels, "update_card", None)):
            return RuntimeReadiness(False, ("employee_outbox",))
        if self._service is None:
            return RuntimeReadiness(False, ("not_composed",))
        try:
            projection = self._service.synchronize_projection()
            active = tuple(
                state
                for state in self._service.list_states()
                if state.phase is HirePhase.ACTIVE and (agent_id is None or state.agent_id == agent_id)
            )
            if not active:
                if agent_id is not None:
                    return RuntimeReadiness(False, ("employee_not_active",))
                return RuntimeReadiness(True, ())
            if self._context_service is None or self._context_source_factory is None or self._data is None:
                return RuntimeReadiness(
                    False,
                    self._context_blockers or ("employee_context",),
                )
            if not getattr(self._context_acl, "configured", False):
                return RuntimeReadiness(
                    False,
                    ("context_request_authority",),
                )
            self._data.service.rebuild_projection()
            projection = self._service.synchronize_projection()
            if not self._refresh_context_bindings(projection):
                return RuntimeReadiness(False, ("context_binding_sync",))
            head = self._data.service.get_head()
            if head.sequence != projection.cursor_sequence or head.logical_hash != projection.cursor_hash:
                return RuntimeReadiness(False, ("context_projection_stale",))
            for state in active:
                employee = projection.employees.get(state.agent_id)
                principal = projection.bot_principals.get(state.bot_principal_id)
                status = self._channels.status(state.agent_id) if self._channels else None
                status_state = getattr(status, "state", None)
                if (
                    employee is None
                    or principal is None
                    or employee.bot_principal_id != state.bot_principal_id
                    or principal.agent_id != state.agent_id
                    or principal.tenant_key != state.tenant_key
                    or principal.app_id != state.app_id
                    or not principal.credential_ref
                ):
                    return RuntimeReadiness(False, ("context_binding",))
                if (
                    status_state is not ChannelProcessState.READY
                    or getattr(status, "generation", None) != state.channel_generation
                    or getattr(status, "identity", {}).get("app_id") != state.app_id
                    or getattr(status, "ready_metadata", {}).get("connection_id") != state.channel_connection_id
                ):
                    return RuntimeReadiness(False, ("context_generation",))
                if self._context_source_factory.probe(principal) is not True:
                    return RuntimeReadiness(False, ("context_credentials",))
            return RuntimeReadiness(True, ())
        except Exception:
            return RuntimeReadiness(False, ("employee_context",))

    def recover(self) -> None:
        """Replay first, then resume only recoverable durable phases."""
        if self._service is None:
            return
        projection = self._service.recover()
        if self._membership is not None:
            self._membership.rebuild_projection()
            if self._runtime_enabled:
                try:
                    self._membership.recover_pending()
                except Exception as exc:
                    logger.error(
                        "employee membership recovery failed closed: %s",
                        type(exc).__name__,
                    )
                    self._execution_blockers = ("membership_recovery",)
        if self._data is not None:
            try:
                self._recover_employee_data(self._service.projection_state)
            except Exception as exc:
                logger.error(
                    "employee data recovery failed closed: %s",
                    type(exc).__name__,
                )
                self._execution_blockers = ("employee_data_recovery",)
        execution_recovered = not self._execution_blockers
        if execution_recovered:
            try:
                assert self._ingress is not None
                assert self._router is not None
                assert self._dispatch is not None
                assert self._outbox is not None
                self._ingress.rebuild_projection()
                self._router.rebuild_projection()
                self._outbox.rebuild_projection()
                self._dispatch.recover_incomplete_attempts()
                self._dispatch.reconcile_terminal_snapshots()
                self._router.recover_terminal_attachments()
                self._reconcile_terminal_ingress()
                self._ingress.gc_terminal_payloads()
            except Exception as exc:
                logger.error(
                    "employee execution recovery failed closed: %s",
                    type(exc).__name__,
                )
                self._execution_blockers = ("employee_recovery",)
                execution_recovered = False
        if self._fire is not None and self._runtime_enabled:
            try:
                self._fire.recover()
            except Exception as exc:
                logger.error(
                    "employee retirement recovery failed closed: %s",
                    type(exc).__name__,
                )
                self._execution_blockers = ("fire_recovery",)
                execution_recovered = False
        if not self._refresh_context_bindings(self._service.projection_state):
            self._context_blockers = ("context_binding_sync",)
        if not self._runtime_enabled:
            self._service.mark_runtime_recovered()
            return
        pending_intents: list[str] = []
        for state in projection.states.values():
            if (
                state.phase
                in {
                    HirePhase.CONFIGURING,
                    HirePhase.VALIDATING,
                    HirePhase.READY_PENDING_VERIFICATION,
                    HirePhase.ACTIVE,
                }
                and state.credential_ref
                and state.channel_generation > 0
            ):
                self._service.begin_channel_revalidation(
                    state.intent_id,
                    observed_generation=state.channel_generation,
                )
                pending_intents.append(state.intent_id)
            if state.phase in {
                HirePhase.PROVISIONING_APP,
                HirePhase.STORING_CREDENTIAL,
                HirePhase.CONFIGURING,
                HirePhase.VALIDATING,
            }:
                pending_intents.append(state.intent_id)
        if pending_intents:
            self._submit_coroutine(
                self._recover_runtime(list(dict.fromkeys(pending_intents))),
                label="recovery",
            )
        else:
            self._service.mark_runtime_recovered()
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._start_monitor_in_loop)
            if execution_recovered:
                self._start_dispatch_worker()

    def journal_frames(self) -> tuple[Any, ...]:
        return tuple(self._writer.replay()) if self._writer is not None else ()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        errors: list[str] = []

        def cleanup(label: str, action: Callable[[], Any]) -> bool:
            try:
                action()
                return True
            except Exception as exc:
                errors.append(f"{label}:{type(exc).__name__}")
                return False

        if self._ingress is not None:
            cleanup("ingress_admission", self._ingress.stop_admission)
        if self._outbox is not None:
            cleanup("outbox_admission", self._outbox.stop_admission)
        if self._service is not None:
            cleanup("hire_admission", self._service.stop_admission)
        if self._context_service is not None:
            cleanup("context_admission", self._context_service.stop_admission)
        self._dispatch_stop.set()
        dispatch_safe = True
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=5.0)
            dispatch_safe = not self._dispatch_thread.is_alive()
        with self._future_lock:
            futures = tuple(self._futures)
        activities_safe = True
        if futures:
            _done, pending = concurrent.futures.wait(futures, timeout=5.0)
            for future in pending:
                future.cancel()
            activities_safe = not pending
        if self._loop is not None and self._loop.is_running():
            quiesce = asyncio.run_coroutine_threadsafe(
                self._quiesce_loop(),
                self._loop,
            )
            activities_safe = cleanup("activity_loop", lambda: quiesce.result(timeout=5.0)) and activities_safe
        context_safe = True
        if self._context_service is not None:
            context_safe = cleanup("context_drain", self._context_service.drain)
        if self._context_source_factory is not None:
            context_safe = cleanup("context_sources", self._context_source_factory.close) and context_safe
        resources_safe = dispatch_safe and activities_safe and context_safe
        if resources_safe:
            if self._channels is not None:
                resources_safe = cleanup("channels", self._channels.close)
        if resources_safe and self._owns_group_memory_backend and self._group_memory_backend is not None:
            resources_safe = cleanup(
                "group_memory",
                self._group_memory_backend.shutdown,
            )
        if resources_safe and self._attachments is not None:
            resources_safe = cleanup("attachments", self._attachments.close)
        if resources_safe and self._ingress is not None:
            resources_safe = cleanup("ingress", self._ingress.close)
        if resources_safe and self._outbox is not None:
            resources_safe = cleanup("outbox", self._outbox.close)
        if resources_safe and self._data is not None:
            resources_safe = cleanup("data", self._data.close)
        if resources_safe:
            if self._service is not None:
                resources_safe = cleanup(
                    "hire_service",
                    self._service.close,
                )
            elif self._writer is not None:
                resources_safe = cleanup("writer", self._writer.close)
        if resources_safe and self._vault is not None:
            resources_safe = cleanup("vault", self._vault.close)
        if resources_safe and self._owned_main_bot_send_audit is not None:
            resources_safe = cleanup(
                "main_bot_send_audit",
                self._owned_main_bot_send_audit.close,
            )
        if not resources_safe:
            errors.append("dependent_resources_held")
        if resources_safe and self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if resources_safe and self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)
        if errors:
            logger.error("employee runtime close errors: %s", ",".join(errors))
        if not resources_safe:
            self._closing = False

    def invalidate_employee_context(self, agent_id: str) -> None:
        with self._context_binding_lock:
            if self._context_source_factory is not None:
                self._context_source_factory.invalidate_employee(agent_id)
            self._context_explicit_invalidations.add(agent_id)
            self._context_bindings.pop(agent_id, None)

    def reactivate_employee_context(self, agent_id: str) -> None:
        """Re-open admission after a durable replacement binding is installed."""
        with self._context_binding_lock:
            if self._context_source_factory is not None:
                self._context_source_factory.reactivate_employee(agent_id)
            self._context_explicit_invalidations.discard(agent_id)
            self._context_projection_invalidations.discard(agent_id)

    def _compose_execution_storage(self, settings: Any) -> None:
        """Compose the data, durable Inbox, and attachment owners."""

        if (
            not self._runtime_enabled
            or getattr(
                settings,
                "autonomous_visible_employee_limit",
                0,
            )
            == 0
        ):
            self._execution_blockers = ("employee_ingress",)
            return
        if self._writer is None or self._vault is None or self._data_keyring is None:
            self._execution_blockers = ("employee_ingress",)
            return
        try:
            legacy_base = str(
                canonicalize_user_home_path(
                    getattr(
                        settings,
                        "autonomous_slock_storage_base",
                        default_slock_storage_base(),
                    )
                )
            )
            self._data = build_employee_data_composition(
                settings=settings,
                writer=self._writer,
                keyring=self._data_keyring,
                admin_principal_ids=frozenset(getattr(settings, "admin_user_ids", ()) or ()),
                main_bot_app_id=getattr(settings, "app_id", ""),
                agents_root=Path(legacy_base).expanduser() / "agents",
                legacy_base=legacy_base,
                subject_resolver=self._resolve_data_subject,
                auto_cutover=False,
            )
            self._ingress = EmployeeIngressService.from_keyring(
                writer=self._writer,
                ingress_state=IngressProjectionState(),
                keyring=self._data_keyring,
                blob_root=getattr(
                    settings,
                    "autonomous_employee_ingress_blob_dir",
                ),
            )
            self._outbox = EmployeeOutboxService.from_keyring(
                writer=self._writer,
                outbox_state=OutboxProjectionState(),
                keyring=self._data_keyring,
                blob_root=getattr(
                    settings,
                    "autonomous_employee_outbox_blob_dir",
                ),
            )
            self._attachments = AttachmentStagingService(
                writer=self._writer,
                root=getattr(
                    settings,
                    "autonomous_employee_attachment_staging_dir",
                ),
                credential_resolver=self._vault,
                download_timeout_seconds=getattr(
                    settings,
                    "autonomous_context_fetch_timeout_seconds",
                    30.0,
                ),
            )
            self._execution_blockers = ()
        except Exception as exc:
            logger.error(
                "employee execution storage composition unavailable: %s",
                type(exc).__name__,
            )
            if self._attachments is not None:
                try:
                    self._attachments.close()
                except Exception:
                    pass
            if self._ingress is not None:
                try:
                    self._ingress.close()
                except Exception:
                    pass
            if self._outbox is not None:
                try:
                    self._outbox.close()
                except Exception:
                    pass
            if self._data is not None:
                try:
                    self._data.close()
                except Exception:
                    pass
            self._attachments = None
            self._ingress = None
            self._outbox = None
            self._data = None
            self._execution_blockers = ("employee_ingress",)

    def _resolve_data_subject(
        self,
        tenant_key: str,
        agent_id: str,
    ) -> EmployeeDataSubject | None:
        service = self._service
        if service is None:
            return None
        projection = service.synchronize_projection()
        employee = projection.employees.get(agent_id)
        if employee is None or employee.tenant_key != tenant_key:
            return None
        return EmployeeDataSubject(
            tenant_key=employee.tenant_key,
            agent_id=employee.agent_id,
            owner_principal_id=employee.owner_principal_id,
            member_groups=tuple(employee.member_groups),
        )

    def _recover_employee_data(self, projection: ProjectionState) -> None:
        data = self._data
        if data is None:
            return
        data.service.rebuild_projection()
        if data.state.data_authority.mode == "legacy":
            from ..migration.slock_data_importer import SlockDataImporter

            legacy_base = data.memory_facade.legacy_base_path
            if legacy_base is None:
                raise RuntimeError("legacy data base is unavailable")
            with data.service.legacy_import_scope():
                for employee in sorted(
                    projection.employees.values(),
                    key=lambda item: item.agent_id,
                ):
                    if employee.state is EmployeeState.ARCHIVED:
                        continue
                    result = SlockDataImporter(
                        service=data.service,
                        legacy_base=legacy_base,
                        tenant_key=employee.tenant_key,
                        owner_principal_id=employee.owner_principal_id,
                    ).import_employee(employee.agent_id)
                    if result.errors:
                        raise RuntimeError("legacy employee data import failed")
            data.service.cutover_to_canonical()
        data.rebuild_all()

    def _compose_membership(
        self,
        settings: Any,
        *,
        manager_client_factory: Callable[[], Any] | None,
    ) -> None:
        """Compose real Bot membership using manager mutation and employee observation."""

        if (
            not self._runtime_enabled
            or self._writer is None
            or self._service is None
            or self._vault is None
            or not callable(manager_client_factory)
            or self._slock_manager is None
        ):
            self._membership = None
            return

        def employee_client_provider(
            agent_id: str,
            app_id: str,
            credential_ref: str,
        ) -> Any:
            app_secret = self._vault.resolve(credential_ref, agent_id, app_id)
            return lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

        def team_owner(chat_id: str) -> str:
            try:
                getter = getattr(self._slock_manager, "get_activated_engine", None)
                engine = getter(chat_id) if callable(getter) else None
                channel = getattr(engine, "channel", None)
                return str(getattr(channel, "owner_id", "") or "")
            except Exception:
                return ""

        try:
            remote = LarkMembershipAPI(
                manager_client_factory(),
                employee_client_provider=employee_client_provider,
            )
            self._membership = EmployeeMembershipService(
                writer=self._writer,
                hire_service=self._service,
                remote=remote,
                admin_principal_ids=frozenset(
                    getattr(settings, "admin_user_ids", ()) or ()
                ),
                team_owner_resolver=team_owner,
                team_active_resolver=lambda chat_id: bool(
                    self._slock_manager.get_activated_engine(chat_id)
                ),
            )
        except Exception as exc:
            logger.error(
                "employee membership composition unavailable: %s",
                type(exc).__name__,
            )
            self._membership = None

    def _compose_fire(self, settings: Any) -> None:
        """Compose the one-way Journal-backed employee retirement workflow."""

        if (
            self._writer is None
            or self._service is None
            or self._ingress is None
            or self._vault is None
            or self._channels is None
            or self._slash_factory is None
            or self._loop is None
        ):
            self._fire = None
            return

        def run_async(coroutine: Any) -> Any:
            if self._loop is None or self._closing:
                if hasattr(coroutine, "close"):
                    coroutine.close()
                raise RuntimeError("employee runtime is closing")
            future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
            return future.result(
                timeout=float(
                    getattr(settings, "autonomous_context_fetch_timeout_seconds", 30.0)
                )
            )

        legacy_base = str(
            canonicalize_user_home_path(
                getattr(
                    settings,
                    "autonomous_slock_storage_base",
                    default_slock_storage_base(),
                )
            )
        )
        authority = JournalFireAuthority(
            writer=self._writer,
            hire_service=self._service,
            ingress_service=self._ingress,
            admin_principal_ids=frozenset(
                getattr(settings, "admin_user_ids", ()) or ()
            ),
        )
        self._fire = EmployeeFireService(
            writer=self._writer,
            authority=authority,
            effects={
                "execution_quiesce": ExecutionQuiesceEffect(
                    self._dispatch,
                    grace_seconds=float(
                        getattr(settings, "autonomous_fire_grace_seconds", 5.0)
                    ),
                ),
                "slash_cleanup": SlashCleanupEffect(
                    reconciler_factory=self._slash_factory,
                    credential_resolver=self._vault.resolve,
                    async_runner=run_async,
                ),
                "channel_stop": ChannelStopEffect(self._channels),
                "membership_cleanup": MembershipCleanupEffect(
                    self._membership,
                    self._service,
                ),
                "credential_destroy": CredentialDestroyEffect(self._vault),
                "archive_move": AtomicEmployeeArchive(
                    Path(legacy_base).expanduser() / "agents"
                ),
            },
        )

    def _resolve_ingress_binding(self, agent_id: str, app_id: str) -> tuple[str, str]:
        service = self._service
        if service is None:
            raise RuntimeError("employee workforce projection is unavailable")
        projection = service.synchronize_projection()
        employee = projection.employees.get(agent_id)
        if employee is None or employee.bot_principal_id == "":
            raise RuntimeError("employee ingress binding is unavailable")
        principal = projection.bot_principals.get(employee.bot_principal_id)
        if principal is None or principal.agent_id != agent_id or principal.app_id != app_id:
            raise RuntimeError("employee ingress principal is unavailable")
        return employee.tenant_key, principal.bot_principal_id

    def _compose_dispatch(self, settings: Any, *, membership_health: Any) -> None:
        if self._execution_blockers:
            return
        if (
            self._service is None
            or self._writer is None
            or self._ingress is None
            or self._data is None
            or self._channels is None
            or self._context_service is None
            or self._outbox is None
        ):
            self._execution_blockers = ("employee_gateway",)
            return
        if self._slock_manager is None or not callable(getattr(self._slock_manager, "employee_activation_guard", None)):
            self._execution_blockers = ("slock_gateway",)
            return
        if not callable(self._environment_provider):
            self._execution_blockers = ("employee_environment",)
            return
        try:
            legacy_base = str(
                canonicalize_user_home_path(
                    getattr(
                        settings,
                        "autonomous_slock_storage_base",
                        default_slock_storage_base(),
                    )
                )
            )

            def registry_provider() -> ProjectedAgentRegistry:
                assert self._service is not None
                return ProjectedAgentRegistry(
                    self._service.synchronize_projection(),
                    storage_base_path=legacy_base,
                )

            health = membership_health or _SlockMembershipHealth(self._slock_manager)
            constraints_digest = hashlib.sha256(b"ghostap.employee-execution-constraints.v1").hexdigest()
            self._router = DurableEmployeeIngressRouter(
                writer=self._writer,
                ingress_service=self._ingress,
                registry_provider=registry_provider,
                channel_status_provider=self._channels,
                requester_acl=self._context_acl,
                queue_limits=RouterQueueLimits(
                    per_employee=getattr(
                        settings,
                        "autonomous_employee_queue_per_employee_limit",
                    ),
                    per_team=getattr(
                        settings,
                        "autonomous_employee_queue_per_team_limit",
                    ),
                    global_limit=getattr(
                        settings,
                        "autonomous_employee_queue_global_limit",
                    ),
                ),
                membership_health=health,
                attachment_staging=self._attachments,
                constraints_digest=constraints_digest,
                system_prompt_token_reserve=getattr(
                    settings,
                    "autonomous_employee_system_prompt_token_reserve",
                ),
            )
            outbox_lifecycle = EmployeeOutboxLifecycle(self._outbox)
            self._dispatch = EmployeeDispatchCoordinator(
                writer=self._writer,
                hire_service=self._service,
                ingress_service=self._ingress,
                router=self._router,
                data_service=self._data.service,
                data_sink=self._data,
                channel_supervisor=self._channels,
                slock_manager=self._slock_manager,
                context_service=self._context_service,
                environment_provider=self._environment_provider,
                registry_factory=lambda state: ProjectedAgentRegistry(
                    state,
                    storage_base_path=legacy_base,
                ),
                attempt_lifecycle=outbox_lifecycle,
                admin_principal_ids=frozenset(
                    getattr(settings, "admin_user_ids", ()) or ()
                ),
                team_owner_resolver=lambda chat_id: str(
                    getattr(
                        getattr(
                            self._slock_manager.get_activated_engine(chat_id),
                            "channel",
                            None,
                        ),
                        "owner_id",
                        "",
                    )
                    or ""
                ),
            )
            self._outbox_lifecycle = outbox_lifecycle
            self._outbox_delivery = EmployeeOutboxDeliveryCoordinator(
                outbox=self._outbox,
                channels=self._channels,
                authority_resolver=self._resolve_outbox_delivery_authority,
            )
            self._execution_blockers = ()
        except Exception as exc:
            logger.error(
                "employee dispatch composition unavailable: %s",
                type(exc).__name__,
            )
            self._router = None
            self._dispatch = None
            self._outbox_delivery = None
            self._outbox_lifecycle = None
            self._execution_blockers = ("employee_gateway",)

    def _start_dispatch_worker(self) -> None:
        if self._dispatch is None or self._router is None or self._ingress is None:
            return
        if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
            return
        self._dispatch_stop.clear()

        def run() -> None:
            delay = 0.05
            while not self._dispatch_stop.wait(delay):
                try:
                    worked = self._drain_employee_dispatch_once()
                    delay = 0.05 if worked else min(delay * 2.0, 1.0)
                except Exception as exc:
                    logger.error(
                        "employee dispatch worker failed closed: %s",
                        type(exc).__name__,
                    )
                    delay = min(max(delay, 0.05) * 2.0, 5.0)

        self._dispatch_thread = threading.Thread(
            target=run,
            name="employee-durable-dispatch",
            daemon=True,
        )
        self._dispatch_thread.start()

    def _drain_employee_dispatch_once(self) -> bool:
        ingress = self._ingress
        router = self._router
        dispatch = self._dispatch
        if ingress is None or router is None or dispatch is None:
            return False
        ingress.rebuild_projection()
        router.rebuild_projection()
        worked = False
        for acceptance_id, record in tuple(ingress.state.by_acceptance_id.items()):
            if record.disposition is None:
                if self._handle_control_ingress(acceptance_id):
                    worked = True
                    continue
                routed = router.state.by_acceptance_id.get(acceptance_id)
                if routed is None or routed.state not in {
                    "queued",
                    "dispatching",
                    "terminal",
                }:
                    router.route(acceptance_id)
                    worked = True
        worked = dispatch.dispatch_next() is not None or worked
        worked = self._reconcile_terminal_ingress() > 0 or worked
        worked = self._drain_employee_outbox_once() or worked
        if self._outbox is not None:
            worked = self._outbox.gc_superseded_snapshots() > 0 or worked
        return ingress.gc_terminal_payloads() > 0 or worked

    def _handle_control_ingress(self, acceptance_id: str) -> bool:
        """Consume exact durable employee controls before Router admission."""

        ingress = self._ingress
        if ingress is None:
            return False
        ingress.rebuild_projection()
        record = ingress.state.by_acceptance_id.get(acceptance_id)
        if record is None:
            return False
        if record.disposition is not None:
            return record.disposition.reason_code.startswith(
                ("stop_", "membership_", "history_", "memory_")
            )
        try:
            payload = ingress.get_payload(acceptance_id)
        except Exception:
            return False
        first = payload.normalized_parts[0] if len(payload.normalized_parts) == 1 else None
        if isinstance(first, Mapping) and first.get("type") == "membership_event":
            if self._membership is None:
                try:
                    ingress.record_disposition(
                        acceptance_id,
                        state="terminal",
                        reason_code="membership_unavailable",
                    )
                except IngressConflictError:
                    pass
                return True
            metadata = record.metadata
            try:
                outcome = self._membership.reconcile_event(
                    tenant_key=metadata.tenant_key,
                    chat_id=metadata.chat_id,
                    agent_id=metadata.agent_id,
                )
            except MembershipBindingError:
                try:
                    ingress.record_disposition(
                        acceptance_id,
                        state="ignored",
                        reason_code="membership_unmanaged",
                    )
                except IngressConflictError:
                    pass
                return True
            try:
                ingress.record_disposition(
                    acceptance_id,
                    state="terminal",
                    reason_code=f"membership_{outcome.state.value}",
                )
            except IngressConflictError:
                pass
            return True
        texts: list[str] = []
        for part in payload.normalized_parts:
            content = part.get("content") if isinstance(part, Mapping) else None
            value = content.get("text") if isinstance(content, Mapping) else None
            if isinstance(value, str):
                texts.append(value.strip())
        data_control = self._parse_data_control(texts)
        if data_control is not None:
            return self._handle_data_control(
                acceptance_id=acceptance_id,
                command=data_control[0],
                record=record,
                payload=payload,
                history_days=data_control[1],
            )
        if texts != ["/stop"]:
            return False
        dispatch = self._dispatch
        lifecycle = self._outbox_lifecycle
        if dispatch is None or lifecycle is None:
            return False
        metadata = record.metadata
        outcome = dispatch.request_cancel(
            agent_id=metadata.agent_id,
            chat_id=metadata.chat_id,
            requester_principal_id=metadata.sender_principal_id,
            command_acceptance_id=acceptance_id,
        )
        lifecycle.command_response(
            tenant_key=metadata.tenant_key,
            agent_id=metadata.agent_id,
            chat_id=metadata.chat_id,
            thread_root_message_id=metadata.thread_root_message_id,
            command_acceptance_id=acceptance_id,
            status=outcome.status,
        )
        try:
            ingress.record_disposition(
                acceptance_id,
                state="terminal",
                reason_code=f"stop_{outcome.status}",
            )
        except IngressConflictError:
            pass
        self._drain_employee_outbox_once()
        return True

    @staticmethod
    def _parse_data_control(texts: list[str]) -> tuple[str, int] | None:
        if texts == ["/memory"]:
            return ("/memory", 0)
        if len(texts) != 1:
            return None
        matched = re.fullmatch(r"/history(?:\s+([1-9]|[12][0-9]|3[01]))?", texts[0])
        if matched is None:
            return None
        return ("/history", int(matched.group(1) or "7"))

    def _handle_data_control(
        self,
        *,
        acceptance_id: str,
        command: str,
        record: Any,
        payload: Any,
        history_days: int = 7,
    ) -> bool:
        data = self._data
        lifecycle = self._outbox_lifecycle
        ingress = self._ingress
        if data is None or lifecycle is None or ingress is None:
            return False
        metadata = record.metadata
        first = payload.normalized_parts[0]
        chat_type = first.get("chat_type", "") if isinstance(first, Mapping) else ""
        request = AuthenticatedDataRequest(
            principal_id=metadata.sender_principal_id,
            tenant_key=metadata.tenant_key,
            receiving_bot_app_id=metadata.app_id,
            chat_id=metadata.chat_id,
            chat_type=chat_type,
            thread_root_id=metadata.thread_root_message_id,
            requested_agent_id=metadata.agent_id,
        )
        succeeded = False
        reason = "failed"
        try:
            if command == "/history":
                from datetime import datetime, timedelta
                from zoneinfo import ZoneInfo

                end = datetime.now(ZoneInfo(data.service.shard_timezone)).date()
                start = end - timedelta(days=history_days - 1)
                result = data.query.query(
                    request,
                    HistoryQuerySpec(
                        start_day=start.isoformat(),
                        end_day=end.isoformat(),
                        page_size=20,
                    ),
                )
                rows = [
                    f"{item.ended_at[:19]} · {item.status} · {item.safe_summary_text}"
                    for item in result.records
                ]
                summary = (
                    f"最近 {history_days} 天暂无可见执行记录。"
                    if not rows
                    else "\n".join(rows)
                )
            else:
                result = data.memory_query.query(
                    request,
                    MemoryQuerySpec(agent_id=metadata.agent_id),
                )
                summary = result.content or "当前会话暂无员工记忆摘要。"
            succeeded = True
            reason = "completed"
        except QueryDeniedError:
            summary = "权限不足，无法读取该员工数据。"
            reason = "denied"
        except Exception:
            logger.exception("employee data control failed closed")
            summary = "员工数据暂不可用，请稍后重试或联系管理员。"
        lifecycle.read_response(
            tenant_key=metadata.tenant_key,
            agent_id=metadata.agent_id,
            chat_id=metadata.chat_id,
            thread_root_message_id=metadata.thread_root_message_id,
            command_acceptance_id=acceptance_id,
            command=command,
            summary=summary,
            succeeded=succeeded,
        )
        try:
            ingress.record_disposition(
                acceptance_id,
                state="terminal",
                reason_code=f"{command.removeprefix('/')}_{reason}",
            )
        except IngressConflictError:
            pass
        self._drain_employee_outbox_once()
        return True

    def _drain_employee_outbox_once(self) -> bool:
        outbox = self._outbox
        delivery = self._outbox_delivery
        if outbox is None or delivery is None:
            return False
        outbox.rebuild_projection()
        pending = sorted(
            (
                record
                for record in outbox.state.by_outbox_id.values()
                if record.binding is None or record.binding.bound_snapshot_version < record.latest_version
            ),
            key=lambda record: (record.latest.created_at, record.outbox_id),
        )
        if not pending:
            return False
        delivery.deliver(pending[0].outbox_id)
        return True

    def _resolve_outbox_delivery_authority(
        self,
        record: Any,
    ) -> EmployeeDeliveryAuthority:
        if self._service is None or self._channels is None:
            raise RuntimeError("employee delivery authority is unavailable")
        projection = self._service.synchronize_projection()
        employee = projection.employees.get(record.agent_id)
        if employee is None or employee.tenant_key != record.tenant_key or not employee.bot_principal_id:
            raise RuntimeError("employee delivery identity is unavailable")
        principal = projection.bot_principals.get(employee.bot_principal_id)
        active = next(
            (
                state
                for state in self._service.list_states()
                if state.agent_id == record.agent_id and state.phase is HirePhase.ACTIVE
            ),
            None,
        )
        status = self._channels.status(record.agent_id)
        connection_id = getattr(status, "ready_metadata", {}).get(
            "connection_id",
            "",
        )
        if (
            principal is None
            or active is None
            or status is None
            or active.tenant_key != record.tenant_key
            or active.bot_principal_id != employee.bot_principal_id
            or principal.tenant_key != record.tenant_key
            or principal.agent_id != record.agent_id
            or getattr(status, "state", None) is not ChannelProcessState.READY
            or principal.app_id != active.app_id
            or getattr(status, "app_id", None) != active.app_id
            or getattr(status, "identity", {}).get("app_id") != active.app_id
            or getattr(status, "generation", None) != active.channel_generation
            or not isinstance(connection_id, str)
            or not connection_id
        ):
            raise RuntimeError("employee delivery Channel is not current")
        return EmployeeDeliveryAuthority(
            app_id=active.app_id,
            generation=active.channel_generation,
            connection_id=connection_id,
        )

    def _reconcile_terminal_ingress(self) -> int:
        ingress = self._ingress
        router = self._router
        if ingress is None or router is None:
            return 0
        router.rebuild_projection()
        ingress.rebuild_projection()
        reconciled = 0
        for acceptance_id, record in tuple(router.state.by_acceptance_id.items()):
            ingress_record = ingress.state.by_acceptance_id.get(acceptance_id)
            if record.state == "terminal" and ingress_record is not None and ingress_record.disposition is None:
                try:
                    ingress.record_disposition(
                        acceptance_id,
                        state="terminal",
                        reason_code=record.reason_code,
                    )
                    reconciled += 1
                except IngressConflictError:
                    pass
        return reconciled

    def rewrap_employee_credential(
        self,
        *,
        agent_id: str,
        app_id: str,
        credential_ref: str,
    ) -> CredentialReceipt:
        """Drain employee clients around an atomic Vault key rewrap."""
        if self._vault is None:
            raise RuntimeError("employee credential Vault is unavailable")
        self.invalidate_employee_context(agent_id)
        receipt = self._vault.rewrap(
            credential_ref,
            agent_id,
            app_id,
        )
        self.reactivate_employee_context(agent_id)
        return receipt

    def _refresh_context_bindings(self, projection: ProjectionState) -> bool:
        if self._context_source_factory is None or self._service is None:
            return True
        try:
            with self._context_binding_lock:
                current: dict[str, tuple[str, str, int]] = {}
                states = self._service.list_states()
                for state in states:
                    if state.phase is not HirePhase.ACTIVE:
                        continue
                    principal = projection.bot_principals.get(state.bot_principal_id)
                    if principal is None or not principal.credential_ref:
                        continue
                    current[state.agent_id] = (
                        principal.app_id,
                        principal.credential_ref,
                        state.channel_generation,
                    )
                previous = dict(self._context_bindings)
                non_active = {state.agent_id for state in states if state.phase is not HirePhase.ACTIVE}
                changed_or_removed = {
                    agent_id for agent_id, old_binding in previous.items() if current.get(agent_id) != old_binding
                }
                for agent_id in non_active | changed_or_removed:
                    if (
                        agent_id not in self._context_explicit_invalidations
                        and agent_id not in self._context_projection_invalidations
                    ):
                        self._context_source_factory.invalidate_employee(agent_id)
                        self._context_projection_invalidations.add(agent_id)
                for agent_id in current:
                    if (
                        agent_id in self._context_projection_invalidations
                        and agent_id not in self._context_explicit_invalidations
                    ):
                        self._context_source_factory.reactivate_employee(agent_id)
                        self._context_projection_invalidations.discard(agent_id)
                self._context_bindings = current
            return True
        except Exception as exc:
            logger.error(
                "employee Context binding refresh failed: %s",
                type(exc).__name__,
            )
            return False

    def _compose_context(
        self,
        settings: Any,
        *,
        context_source_factory: EmployeeMessageSourceFactory | None,
        group_memory_backend: Any,
    ) -> None:
        """Compose execution-only Context; failures never block first hire."""
        if not self._runtime_enabled or getattr(settings, "autonomous_visible_employee_limit", 0) == 0:
            self._context_blockers = ("employee_context",)
            return
        if self._service is None or self._writer is None or self._vault is None:
            self._context_blockers = ("employee_context",)
            return
        try:
            legacy_base = str(
                canonicalize_user_home_path(
                    getattr(
                        settings,
                        "autonomous_slock_storage_base",
                        default_slock_storage_base(),
                    )
                )
            )
            if self._data is None:
                self._data = build_employee_data_composition(
                    settings=settings,
                    writer=self._writer,
                    admin_principal_ids=frozenset(getattr(settings, "admin_user_ids", ()) or ()),
                    main_bot_app_id=getattr(settings, "app_id", ""),
                    agents_root=Path(legacy_base).expanduser() / "agents",
                    legacy_base=legacy_base,
                )
            backend = group_memory_backend
            if backend is None:
                backend = MemoryManager(str(Path(legacy_base).expanduser()))
                self._owns_group_memory_backend = True
            self._group_memory_backend = backend
            self._context_acl = parse_requester_acl(settings)

            def registry_provider() -> ProjectedAgentRegistry:
                assert self._service is not None
                return ProjectedAgentRegistry(
                    self._service.projection_state,
                    storage_base_path=legacy_base,
                )

            generation = RuntimeEmployeeGenerationAuthority(
                hire_service_provider=lambda: self._service,
                channel_supervisor=self._channels,
                data_composition=self._data,
            )
            source_factory = context_source_factory or LarkEmployeeMessageSourceFactory(
                credential_resolver=self._vault,
                request_timeout_seconds=getattr(
                    settings,
                    "autonomous_context_fetch_timeout_seconds",
                    30.0,
                ),
            )
            group_reader = AuthorizedGroupMemoryReader(
                registry_provider=registry_provider,
                requester_acl=self._context_acl,
                backend=backend,
            )
            self._context_source_factory = source_factory
            self._context_service = EmployeeContextService(
                registry_provider=registry_provider,
                generation_authority=generation,
                requester_acl=self._context_acl,
                data_composition=self._data,
                group_memory_reader=group_reader,
                source_factory=source_factory,
                config=ThreadContextConfig.from_settings(settings),
            )
            self._context_blockers = ()
        except Exception as exc:
            logger.error(
                "employee Context composition unavailable: %s",
                type(exc).__name__,
            )
            if self._context_source_factory is not None:
                try:
                    self._context_source_factory.close()
                except Exception:
                    pass
            if self._owns_group_memory_backend and self._group_memory_backend is not None:
                try:
                    self._group_memory_backend.shutdown()
                except Exception:
                    pass
            self._group_memory_backend = None
            self._owns_group_memory_backend = False
            self._context_source_factory = None
            self._context_service = None
            self._context_blockers = ("employee_context",)
            if not self._execution_blockers:
                self._execution_blockers = ("employee_gateway",)

    @staticmethod
    async def _quiesce_loop() -> None:
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.get_running_loop().shutdown_default_executor()

    @staticmethod
    def _default_slash_factory(app_id: str, app_secret: str) -> _SlashReconciler:
        client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()
        return SlashCommandReconciler(LarkSlashCommandAPI(client))

    def _start_loop(self) -> None:
        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.call_soon(self._loop_ready.set)
            loop.run_forever()
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

        self._loop_thread = threading.Thread(
            target=run,
            name="employee-hire-activities",
            daemon=True,
        )
        self._loop_thread.start()
        if not self._loop_ready.wait(5.0) or self._loop is None:
            raise RuntimeError("employee activity loop failed to start")

    def _submit_intent(self, intent_id: str) -> None:
        if self._closing or self._loop is None:
            raise RuntimeError("employee runtime is closing")
        with self._future_lock:
            existing = self._intent_futures.get(intent_id)
            if existing is not None and not existing.done():
                return
            future = asyncio.run_coroutine_threadsafe(
                self._configure_intent(intent_id),
                self._loop,
            )
            self._intent_futures[intent_id] = future
            self._futures.add(future)

        def complete(done: concurrent.futures.Future[Any]) -> None:
            with self._future_lock:
                self._futures.discard(done)
                if self._intent_futures.get(intent_id) is done:
                    self._intent_futures.pop(intent_id, None)
            try:
                done.result()
            except Exception as exc:
                logger.error(
                    "employee provisioning activity failed: %s",
                    type(exc).__name__,
                )

        future.add_done_callback(complete)

    def _submit_coroutine(
        self,
        coroutine: Any,
        *,
        label: str,
    ) -> None:
        if self._closing or self._loop is None:
            if hasattr(coroutine, "close"):
                coroutine.close()
            raise RuntimeError("employee runtime is closing")
        future = asyncio.run_coroutine_threadsafe(
            coroutine,
            self._loop,
        )
        with self._future_lock:
            self._futures.add(future)

        def complete(done: concurrent.futures.Future[Any]) -> None:
            with self._future_lock:
                self._futures.discard(done)
            try:
                done.result()
            except Exception as exc:
                logger.error(
                    "employee %s activity failed: %s",
                    label,
                    type(exc).__name__,
                )

        future.add_done_callback(complete)

    async def _recover_runtime(
        self,
        pending_intents: list[str],
    ) -> None:
        failed_intents: list[str] = []
        if pending_intents:
            results = await asyncio.gather(
                *(self._configure_intent(intent_id, force_slash_refresh=True) for intent_id in pending_intents),
                return_exceptions=True,
            )
            failed_intents = [
                intent_id
                for intent_id, result in zip(pending_intents, results, strict=True)
                if isinstance(result, BaseException)
            ]
        self._start_monitor_in_loop()
        if failed_intents:
            retry_results = await asyncio.gather(
                *(self._retry_recovery_intent(intent_id) for intent_id in failed_intents),
                return_exceptions=True,
            )
            if any(isinstance(result, BaseException) for result in retry_results):
                logger.error(
                    "employee recovery remains fail-closed for %d intent(s)",
                    sum(isinstance(result, BaseException) for result in retry_results),
                )
                return
        self._require_service().mark_runtime_recovered()
        if not self._execution_blockers:
            self._start_dispatch_worker()

    async def _retry_recovery_intent(self, intent_id: str) -> None:
        delay = 0.1
        while not self._closing:
            await asyncio.sleep(delay)
            try:
                await self._configure_intent(
                    intent_id,
                    force_slash_refresh=True,
                )
                return
            except Exception:
                delay = min(delay * 2.0, 30.0)
        raise RuntimeError("employee runtime is closing")

    def _start_monitor_in_loop(self) -> None:
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_channels())

    async def _monitor_channels(self) -> None:
        while not self._closing:
            service = self._require_service()
            channels = self._channels
            if channels is not None:
                for state in service.list_states():
                    if state.phase not in {
                        HirePhase.ACTIVE,
                        HirePhase.READY_PENDING_VERIFICATION,
                    }:
                        continue
                    try:
                        status = channels.status(state.agent_id)
                        if (
                            status is not None
                            and status.generation == state.channel_generation
                            and status.state
                            in {
                                ChannelProcessState.CRASHED,
                                ChannelProcessState.FAILED,
                                ChannelProcessState.STOPPED,
                            }
                        ):
                            service.begin_channel_revalidation(
                                state.intent_id,
                                observed_generation=state.channel_generation,
                            )
                            self._submit_intent(state.intent_id)
                    except Exception as exc:
                        logger.error(
                            "employee Channel monitor failed closed: %s",
                            type(exc).__name__,
                        )
            await asyncio.sleep(2.0)

    async def _configure_intent(
        self,
        intent_id: str,
        *,
        force_slash_refresh: bool = False,
    ) -> None:
        service = self._require_service()
        state = service.get_state(intent_id)
        if state is None:
            return
        if state.phase in {HirePhase.PROVISIONING_APP, HirePhase.STORING_CREDENTIAL}:
            state = await service.run_provisioning(intent_id)
        if state.phase not in {HirePhase.CONFIGURING, HirePhase.VALIDATING}:
            return
        for launch_attempt in range(2):
            state = service.get_state(intent_id) or state
            generation = self._target_channel_generation(state)
            slash = await self._reconcile_slash(
                state,
                generation=generation,
                force_refresh=force_slash_refresh,
            )
            state = service.get_state(intent_id) or state
            try:
                channel = await self._start_channel(state)
            except RuntimeError:
                if launch_attempt == 0:
                    continue
                raise
            binding = VerificationBinding(
                hire_intent_id=state.intent_id,
                tenant_key=state.tenant_key,
                app_id=state.app_id,
                agent_id=state.agent_id,
                generation=channel.generation,
                requester_principal_id=state.requester_principal_id,
                expected_slash_spec_hash=slash.spec_hash,
            )
            router = self._verification_router
            if router is None:
                raise RuntimeError("employee Verification Router unavailable")
            challenge = router.issue_challenge(binding)
            service.begin_activation_verification(challenge)
            self._challenges[state.intent_id] = challenge
            return

    async def _reconcile_slash(
        self,
        state: DurableHireState,
        *,
        generation: int,
        force_refresh: bool,
    ) -> VerifiedSlashState:
        service = self._require_service()
        effect_id = service.select_slash_reconcile_effect(
            state.intent_id,
            generation=generation,
            force_refresh=force_refresh,
        )
        current = service.get_state(state.intent_id) or state
        effect_state = current.effect_state(effect_id)
        if effect_state is None:
            current = service.commit_effect_transition(
                state.intent_id,
                effect_id=effect_id,
                effect_type="slash_reconciliation",
                next_state=HireEffectState.PREPARED,
            )
            effect_state = current.effect_state(effect_id)
        if effect_state is HireEffectState.PREPARED:
            service.commit_effect_transition(
                state.intent_id,
                effect_id=effect_id,
                effect_type="slash_reconciliation",
                next_state=HireEffectState.EXECUTING,
            )
        current = service.get_state(state.intent_id) or state
        if current.effect_state(effect_id) is HireEffectState.COMMITTED:
            return VerifiedSlashState(
                spec_hash=current.slash_spec_hash,
                observed_hash=current.slash_observed_hash,
                observed=(),
            )
        if current.effect_state(effect_id) is not HireEffectState.EXECUTING:
            raise RuntimeError("Slash reconciliation requires manual action")
        if self._vault is None or self._slash_factory is None:
            raise RuntimeError("Slash composition unavailable")
        secret = await asyncio.to_thread(
            self._vault.resolve,
            current.credential_ref,
            current.agent_id,
            current.app_id,
        )
        reconciler = self._slash_factory(current.app_id, secret)
        try:
            verified = await reconciler.reconcile()
        finally:
            del secret
        service.commit_effect_transition(
            state.intent_id,
            effect_id=effect_id,
            effect_type="slash_reconciliation",
            next_state=HireEffectState.COMMITTED,
            metadata={
                "slash_spec_hash": verified.spec_hash,
                "slash_observed_hash": verified.observed_hash,
                "slash_verified_at": str(time.time()),
            },
        )
        return verified

    async def _start_channel(self, state: DurableHireState) -> Any:
        service = self._require_service()
        generation = self._target_channel_generation(state)
        effect_id = f"channel-start:{generation}"
        current = service.get_state(state.intent_id) or state
        effect_state = current.effect_state(effect_id)
        if effect_state is None:
            current = service.commit_effect_transition(
                state.intent_id,
                effect_id=effect_id,
                effect_type="employee_channel_start",
                next_state=HireEffectState.PREPARED,
            )
            effect_state = current.effect_state(effect_id)
        if effect_state is HireEffectState.PREPARED:
            service.commit_effect_transition(
                state.intent_id,
                effect_id=effect_id,
                effect_type="employee_channel_start",
                next_state=HireEffectState.EXECUTING,
            )
        if self._channels is None:
            raise RuntimeError("employee Channel composition unavailable")
        try:
            status = await asyncio.to_thread(
                self._channels.start,
                state.agent_id,
                state.app_id,
                state.credential_ref,
                generation,
                self._event_callback(state.intent_id, generation),
            )
        except Exception as exc:
            service.commit_effect_transition(
                state.intent_id,
                effect_id=effect_id,
                effect_type="employee_channel_start",
                next_state=HireEffectState.ACTION_REQUIRED,
                metadata={"error_code": f"start-{type(exc).__name__}"},
            )
            raise RuntimeError("employee Channel start failed") from None
        identity_app_id = status.identity.get("app_id")
        connection_id = status.ready_metadata.get("connection_id")
        if (
            status.state is not ChannelProcessState.READY
            or identity_app_id != state.app_id
            or not isinstance(connection_id, str)
            or not connection_id
        ):
            error_code = getattr(status, "error_code", "") or "invalid-ready"
            service.commit_effect_transition(
                state.intent_id,
                effect_id=effect_id,
                effect_type="employee_channel_start",
                next_state=HireEffectState.ACTION_REQUIRED,
                metadata={"error_code": error_code},
            )
            raise RuntimeError("employee Channel did not become ready")
        current = service.get_state(state.intent_id) or state
        if current.effect_state(effect_id) is HireEffectState.EXECUTING:
            service.commit_effect_transition(
                state.intent_id,
                effect_id=effect_id,
                effect_type="employee_channel_start",
                next_state=HireEffectState.COMMITTED,
                metadata={
                    "app_id": state.app_id,
                    "generation": str(generation),
                    "identity_app_id": identity_app_id,
                    "connection_id": connection_id,
                    "channel_verified_at": str(time.time()),
                },
            )
        return status

    @staticmethod
    def _target_channel_generation(state: DurableHireState) -> int:
        attempts: list[tuple[int, HireEffectState]] = []
        for effect_id, effect_state in state.effects:
            if not effect_id.startswith("channel-start:"):
                continue
            generation_text = effect_id.removeprefix("channel-start:")
            if generation_text.isdigit() and int(generation_text) > 0:
                attempts.append((int(generation_text), effect_state))
        if attempts:
            attempted_generation, effect_state = max(attempts)
            if effect_state is HireEffectState.ACTION_REQUIRED:
                return attempted_generation + 1
            if attempted_generation > state.channel_generation:
                return attempted_generation
        generation = state.channel_generation or 1
        if state.phase is HirePhase.VALIDATING and state.channel_generation > 0:
            return generation + 1
        return generation

    def _event_callback(
        self,
        intent_id: str,
        generation: int,
    ) -> Callable[[dict[str, Any]], None]:
        def callback(payload: dict[str, Any]) -> None:
            if self._closing or self._loop is None:
                return
            future = asyncio.run_coroutine_threadsafe(
                self._handle_channel_event(intent_id, generation, payload),
                self._loop,
            )

            def completed(done: concurrent.futures.Future[Any]) -> None:
                try:
                    done.result()
                except Exception as exc:
                    logger.error(
                        "employee Channel event failed closed: %s",
                        type(exc).__name__,
                    )

            future.add_done_callback(completed)

        return callback

    async def _handle_channel_event(
        self,
        intent_id: str,
        generation: int,
        payload: dict[str, Any],
    ) -> None:
        service = self._require_service()
        state = service.get_state(intent_id)
        challenge = self._challenges.get(intent_id)
        event_name = payload.get("event")
        if event_name == "durableIngressAccepted":
            data = payload.get("data")
            acceptance_id = data.get("acceptance_id") if isinstance(data, dict) else None
            if isinstance(acceptance_id, str) and acceptance_id:
                await asyncio.to_thread(self._handle_control_ingress, acceptance_id)
            return
        if event_name == "rawMessageMeta":
            metadata = payload.get("data")
            if isinstance(metadata, dict):
                event_id = metadata.get("event_id")
                tenant_key = metadata.get("tenant_key")
                message_id = metadata.get("message_id")
                if all(isinstance(value, str) and value for value in (event_id, tenant_key, message_id)):
                    if len(self._raw_message_metadata) >= 2048:
                        self._raw_message_metadata.pop(
                            next(iter(self._raw_message_metadata)),
                            None,
                        )
                    self._raw_message_metadata[(intent_id, generation, message_id)] = (
                        event_id,
                        tenant_key,
                    )
            return
        if (
            state is None
            or challenge is None
            or state.phase is not HirePhase.READY_PENDING_VERIFICATION
            or generation != state.channel_generation
            or event_name != "message"
        ):
            return
        data = payload.get("data")
        message_id = data.get("id") if isinstance(data, dict) else None
        raw_metadata = (
            self._raw_message_metadata.pop(
                (intent_id, generation, message_id),
                None,
            )
            if isinstance(message_id, str)
            else None
        )
        parsed = self._parse_status_ingress(data, raw_metadata)
        if parsed is None:
            return
        event_id, message_id, tenant_key, sender_id, command, is_p2p = parsed
        if (
            tenant_key != state.tenant_key
            or sender_id != state.requester_principal_id
            or command != "/status"
            or is_p2p is not True
        ):
            return
        effect_id = f"verification-status-reply:{event_id}"
        effect_state = state.effect_state(effect_id)
        if effect_state is None:
            state = service.commit_effect_transition(
                intent_id,
                effect_id=effect_id,
                effect_type="employee_status_reply",
                next_state=HireEffectState.PREPARED,
            )
            effect_state = state.effect_state(effect_id)
        if effect_state is HireEffectState.PREPARED:
            state = service.commit_effect_transition(
                intent_id,
                effect_id=effect_id,
                effect_type="employee_status_reply",
                next_state=HireEffectState.EXECUTING,
            )
            effect_state = state.effect_state(effect_id)
        if effect_state is not HireEffectState.EXECUTING or self._channels is None:
            return
        receipt = await asyncio.to_thread(
            self._channels.send,
            state.agent_id,
            generation=generation,
            target=sender_id,
            message={"text": f"{state.employee_name} is ready."},
            options={"reply_to": message_id},
        )
        if getattr(receipt, "success", False) is not True:
            raise RuntimeError("employee status reply was not acknowledged")
        send_request_id = getattr(receipt, "request_id", "")
        reply_app_id = getattr(receipt, "app_id", "")
        reply_generation = getattr(receipt, "generation", 0)
        reply_connection_id = getattr(receipt, "connection_id", "")
        reply_message_id = getattr(receipt, "message_id", "")
        if (
            not isinstance(send_request_id, str)
            or not send_request_id
            or reply_app_id != state.app_id
            or reply_generation != generation
            or reply_connection_id != state.channel_connection_id
            or not isinstance(reply_message_id, str)
            or not reply_message_id
        ):
            raise RuntimeError("employee status reply receipt is invalid")
        audit = self._main_bot_send_audit
        if audit is None:
            raise RuntimeError("main Bot send audit is unavailable")
        audited_at = time.time()
        main_bot_send_count = await asyncio.to_thread(
            audit,
            state.tenant_key,
            challenge.issued_at,
            audited_at,
        )
        if isinstance(main_bot_send_count, bool) or not isinstance(main_bot_send_count, int) or main_bot_send_count < 0:
            raise RuntimeError("main Bot send audit is invalid")
        service.commit_effect_transition(
            intent_id,
            effect_id=effect_id,
            effect_type="employee_status_reply",
            next_state=HireEffectState.COMMITTED,
            metadata={
                "send_request_id": send_request_id,
                "ingress_event_id": event_id,
                "reply_app_id": reply_app_id,
                "reply_message_id": reply_message_id,
                "generation": str(reply_generation),
                "connection_id": reply_connection_id,
                "main_bot_send_count": str(main_bot_send_count),
            },
        )
        coordinates = VerificationCoordinates(
            hire_intent_id=state.intent_id,
            tenant_key=state.tenant_key,
            app_id=state.app_id,
            agent_id=state.agent_id,
            generation=generation,
            nonce=challenge.nonce,
        )
        ingress_at = max(time.time(), challenge.issued_at)
        router = self._verification_router
        if router is None:
            raise RuntimeError("employee Verification Router unavailable")
        decision = router.evaluate(
            challenge,
            slash=SlashVerificationEvidence(
                coordinates=coordinates,
                desired_spec_hash=state.slash_spec_hash,
                observed_spec_hash=state.slash_observed_hash,
                reconciled=state.slash_spec_hash == state.slash_observed_hash,
                verified_at=state.slash_verified_at,
            ),
            channel=ChannelVerificationEvidence(
                coordinates=coordinates,
                identity_app_id=state.channel_identity_app_id,
                connection_id=state.channel_connection_id,
                ready=True,
                verified_at=state.channel_verified_at,
            ),
            ingress=TenantIngressEvidence(
                coordinates=coordinates,
                event_id=event_id,
                message_id=message_id,
                sender_principal_id=sender_id,
                command=command,
                is_p2p=is_p2p,
                reply_succeeded=True,
                reply_app_id=reply_app_id,
                employee_send_request_id=send_request_id,
                main_bot_send_count=main_bot_send_count,
                received_at=ingress_at,
            ),
            current_generation=generation,
            now=ingress_at,
        )
        if decision.outcome is VerificationOutcome.READY:
            service.commit_activation(decision)
            if not self._refresh_context_bindings(service.projection_state):
                self._context_blockers = ("context_binding_sync",)

    @staticmethod
    def _parse_status_ingress(
        data: object,
        raw_metadata: tuple[str, str] | None,
    ) -> tuple[str, str, str, str, str, bool] | None:
        if not isinstance(data, dict) or raw_metadata is None:
            return None
        conversation = data.get("conversation")
        sender = data.get("sender")
        if not all(isinstance(value, dict) for value in (conversation, sender)):
            return None
        event_id, tenant_key = raw_metadata
        message_id = data.get("id")
        sender_id = sender.get("open_id")
        text = data.get("safe_content_text") or data.get("content_text")
        chat_type = conversation.get("chat_type")
        if not all(isinstance(value, str) and value for value in (event_id, tenant_key, message_id, sender_id, text)):
            return None
        return (
            event_id,
            message_id,
            tenant_key,
            sender_id,
            text.strip(),
            chat_type == "p2p",
        )

    def _require_service(self) -> ProductionEmployeeHireService:
        if self._service is None:
            raise RuntimeError("employee hire service unavailable")
        return self._service


__all__ = ["EmployeeDepartmentRuntime", "RuntimeReadiness"]
