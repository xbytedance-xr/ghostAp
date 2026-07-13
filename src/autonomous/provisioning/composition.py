"""Single-owner production composition for durable visible employee hiring."""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import lark_oapi as lark

from ..journal.anchor import FileAnchor
from ..journal.projections import ProjectionState
from ..journal.writer import JOURNAL_FILENAME, JournalWriter
from ..supervisor.employee_channels import (
    ChannelProcessState,
    EmployeeChannelSupervisor,
)
from ..workforce.credential_vault import CredentialKeyring, CredentialVault
from .hire_service import HireReadiness, ProductionEmployeeHireService
from .hire_state import DurableHireState, HireEffectState, HirePhase
from .lark_app import LarkAppRegistrar
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

    def close(self) -> None: ...


class _SlashReconciler(Protocol):
    async def reconcile(self) -> VerifiedSlashState: ...


SlashReconcilerFactory = Callable[[str, str], _SlashReconciler]
MainBotSendAudit = Callable[[str, float, float], int]


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
        external_resume_allowed: bool = False,
    ) -> None:
        self._blockers = blockers
        self._external_resume_allowed = external_resume_allowed is True
        self._service: ProductionEmployeeHireService | None = None
        self._writer: JournalWriter | None = None
        self._vault: CredentialVault | None = None
        self._channels: _ChannelSupervisor | None = None
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
        self._monitor_task: asyncio.Task[None] | None = None
        self._raw_message_metadata: dict[
            tuple[str, int, str], tuple[str, str]
        ] = {}

    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        registrar: Any = None,
        channel_supervisor: _ChannelSupervisor | None = None,
        slash_reconciler_factory: SlashReconcilerFactory | None = None,
        main_bot_send_audit: MainBotSendAudit | None = None,
        notification_link: Callable[[DurableHireState, str, int], object]
        | None = None,
        notification_status: Callable[[DurableHireState, str], object]
        | None = None,
    ) -> EmployeeDepartmentRuntime:
        limit = getattr(settings, "autonomous_visible_employee_limit", 0)
        journal_exists = (
            Path(settings.autonomous_journal_dir).expanduser() / JOURNAL_FILENAME
        ).is_file()
        if limit == 0 and not journal_exists:
            return cls(blockers=("visible_employee_limit",))
        release_evidence_ready = cls._release_evidence_ready(settings)
        if release_evidence_ready is not True and not journal_exists:
            return cls(blockers=("release_evidence",))
        if release_evidence_ready is True and notification_link is None:
            return cls(blockers=("registration_notifier",))
        if release_evidence_ready is True and main_bot_send_audit is None:
            return cls(blockers=("main_bot_send_audit",))
        if getattr(settings, "autonomous_anchor_provider", "") != "file":
            return cls(blockers=("production_anchor",))
        if (
            release_evidence_ready is True
            and getattr(settings, "autonomous_worker_sandbox_verified", False)
            is not True
        ):
            return cls(blockers=("worker_sandbox",))

        runtime = cls(external_resume_allowed=release_evidence_ready is True)
        try:
            hmac_key = cls._decode_hmac_key(settings)
            keyring = CredentialKeyring.from_settings(settings)
            vault = CredentialVault(
                Path(settings.autonomous_credential_dir).expanduser(),
                keyring,
            )
            writer = JournalWriter.open(
                Path(settings.autonomous_journal_dir).expanduser(),
                anchor=FileAnchor(settings.autonomous_anchor_path),
                hmac_key=hmac_key,
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
        try:
            runtime._channels = channel_supervisor or EmployeeChannelSupervisor(
                secret_resolver=vault.resolve,
            )
            runtime._slash_factory = (
                slash_reconciler_factory or cls._default_slash_factory
            )
            runtime._main_bot_send_audit = main_bot_send_audit
            runtime._start_loop()
            service = ProductionEmployeeHireService(
                writer,
                ProjectionState(),
                visible_employee_limit=limit,
                release_evidence_ready=release_evidence_ready is True,
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

    def readiness(self) -> RuntimeReadiness:
        if self._service is None:
            return RuntimeReadiness(False, self._blockers or ("not_composed",))
        service_readiness: HireReadiness = self._service.readiness()
        return RuntimeReadiness(service_readiness.ready, service_readiness.blockers)

    def recover(self) -> None:
        """Replay first, then resume only recoverable durable phases."""
        if self._service is None:
            return
        projection = self._service.recover()
        if not self._external_resume_allowed:
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

    def journal_frames(self) -> tuple[Any, ...]:
        return tuple(self._writer.replay()) if self._writer is not None else ()

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._service is not None:
            self._service.stop_admission()
        with self._future_lock:
            futures = tuple(self._futures)
        if futures:
            _done, pending = concurrent.futures.wait(futures, timeout=5.0)
            for future in pending:
                future.cancel()
        if self._loop is not None and self._loop.is_running():
            quiesce = asyncio.run_coroutine_threadsafe(
                self._quiesce_loop(),
                self._loop,
            )
            quiesce.result()
        if self._channels is not None:
            self._channels.close()
        if self._service is not None:
            self._service.close()
        elif self._writer is not None:
            self._writer.close()
        if self._vault is not None:
            self._vault.close()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)

    @staticmethod
    async def _quiesce_loop() -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.get_running_loop().shutdown_default_executor()

    @staticmethod
    def _decode_hmac_key(settings: Any) -> bytes:
        raw = settings.autonomous_journal_hmac_key.get_secret_value()
        try:
            decoded = base64.b64decode(raw, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("invalid Journal HMAC key") from None
        if len(decoded) < 32:
            raise ValueError("invalid Journal HMAC key")
        return decoded

    @staticmethod
    def _release_evidence_ready(settings: Any) -> bool:
        """Reject config-only evidence until external provenance is integrated.

        The operator-side bundle remains useful for acceptance reporting, but
        settings can neither attest the running image/workload nor provide an
        immutable QA trust root or a monotonic consumption ledger.  Treating
        the same settings as both claim and authority would permit replay.
        """
        del settings
        logger.info(
            "employee release remains closed: external build/workload provenance "
            "and monotonic attestation consumption are unavailable"
        )
        return False

    @staticmethod
    def _default_slash_factory(app_id: str, app_secret: str) -> _SlashReconciler:
        client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .build()
        )
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
                *(
                    self._configure_intent(intent_id, force_slash_refresh=True)
                    for intent_id in pending_intents
                ),
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
                    sum(
                        isinstance(result, BaseException)
                        for result in retry_results
                    ),
                )
                return
        self._require_service().mark_runtime_recovered()

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
        if event_name == "rawMessageMeta":
            metadata = payload.get("data")
            if isinstance(metadata, dict):
                event_id = metadata.get("event_id")
                tenant_key = metadata.get("tenant_key")
                message_id = metadata.get("message_id")
                if all(
                    isinstance(value, str) and value
                    for value in (event_id, tenant_key, message_id)
                ):
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
        if (
            isinstance(main_bot_send_count, bool)
            or not isinstance(main_bot_send_count, int)
            or main_bot_send_count < 0
        ):
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

    @staticmethod
    def _parse_status_ingress(
        data: object,
        raw_metadata: tuple[str, str] | None,
    ) -> tuple[str, str, str, str, str, bool] | None:
        if not isinstance(data, dict) or raw_metadata is None:
            return None
        conversation = data.get("conversation")
        sender = data.get("sender")
        if not all(
            isinstance(value, dict)
            for value in (conversation, sender)
        ):
            return None
        event_id, tenant_key = raw_metadata
        message_id = data.get("id")
        sender_id = sender.get("open_id")
        text = data.get("safe_content_text") or data.get("content_text")
        chat_type = conversation.get("chat_type")
        if not all(
            isinstance(value, str) and value
            for value in (event_id, tenant_key, message_id, sender_id, text)
        ):
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
