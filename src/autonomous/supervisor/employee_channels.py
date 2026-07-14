"""Parent-side owner for one fresh Channel interpreter per employee."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from src.autonomous.ingress.models import (
    EmployeeIngressAck,
    EmployeeIngressMetadata,
    EmployeeIngressPayload,
)
from src.autonomous.ingress.service import EmployeeIngressService
from src.autonomous.provisioning.channel_protocol import (
    MAX_FRAME_BYTES,
    ChannelBootstrap,
    ChannelFrame,
    FrameType,
    ProtocolError,
    decode_frame,
    encode_bootstrap,
    encode_frame,
)
from src.autonomous.supervisor.channel_models import ChannelProcessState

logger = logging.getLogger(__name__)


class ChannelSandboxUnavailable(RuntimeError):
    """No verified per-employee OS isolation boundary is available."""

    def __init__(self) -> None:
        super().__init__("employee Channel sandbox unavailable")


@dataclass(frozen=True, slots=True)
class SandboxAttestation:
    pid: int
    verified: bool
    mechanism: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ChannelLaunchContract:
    argv: tuple[str, ...]
    close_fds: bool
    pass_fds: tuple[int, ...]
    env: dict[str, str]


@dataclass(frozen=True, slots=True)
class ChannelProcessStatus:
    agent_id: str
    app_id: str
    generation: int
    pid: int
    state: ChannelProcessState
    tenant_key: str = ""
    bot_principal_id: str = ""
    identity: dict[str, Any] = field(default_factory=dict)
    ready_metadata: dict[str, Any] = field(default_factory=dict)
    sandbox: SandboxAttestation | None = None
    started_at: float = field(default_factory=time.time)
    ready_at: float | None = None
    stopped_at: float | None = None
    exit_code: int | None = None
    error_code: str = ""
    stale_frames: int = 0
    restart_count: int = 0
    backoff_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ChannelSendReceipt:
    request_id: str
    success: bool
    app_id: str
    generation: int
    connection_id: str
    message_id: str


@dataclass(slots=True)
class _PendingSend:
    completed: threading.Event = field(default_factory=threading.Event)
    success: bool | None = None
    app_id: str = ""
    generation: int = 0
    connection_id: str = ""
    message_id: str = ""
    operation: str = "send"
    expected_message_id: str = ""


@dataclass(frozen=True, slots=True)
class DesiredEmployeeChannel:
    agent_id: str
    app_id: str
    credential_ref: str = field(repr=False)
    generation: int = 1
    on_event: Callable[[dict[str, Any]], None] = field(default=lambda _: None, repr=False, compare=False)


class SandboxAttestor(Protocol):
    def __call__(self, pid: int) -> SandboxAttestation: ...


@dataclass(slots=True)
class _Runtime:
    process: subprocess.Popen[bytes]
    control_fd: int
    event_fd: int
    status: ChannelProcessStatus
    on_event: Callable[[dict[str, Any]], None]
    tenant_key: str = ""
    bot_principal_id: str = ""
    requires_observed_connection: bool = False
    ready: threading.Event = field(default_factory=threading.Event)
    reader: threading.Thread | None = None
    stopping: bool = False
    outbound_sequence: int = 0
    inbound_sequence: int = 0
    pending_sends: dict[str, _PendingSend] = field(default_factory=dict)
    control_lock: threading.Lock = field(default_factory=threading.Lock)


class EmployeeChannelSupervisor:
    """Own employee Channel children without sharing SDK process globals."""

    def __init__(
        self,
        *,
        secret_resolver: Callable[[str, str, str], str],
        ready_timeout: float = 30.0,
        stop_timeout: float = 5.0,
        send_timeout: float = 10.0,
        worker_path: str | Path | None = None,
        launcher: Callable[..., subprocess.Popen[bytes]] | None = None,
        sandbox_attestor: SandboxAttestor | None = None,
        sandbox_prefix: tuple[str, ...] | None = None,
        ingress_service: EmployeeIngressService | None = None,
        ingress_binding_resolver: Callable[[str, str], tuple[str, str]] | None = None,
        ingress_ack_timeout: float = 1.5,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._ready_timeout = ready_timeout
        self._stop_timeout = stop_timeout
        self._send_timeout = send_timeout
        self._worker_path = (
            Path(worker_path).resolve()
            if worker_path is not None
            else Path(__file__).resolve().parents[1] / "provisioning" / "channel_worker.py"
        ).resolve()
        self._production_worker = worker_path is None
        if (ingress_service is None) != (ingress_binding_resolver is None):
            raise ValueError("durable ingress service and binding resolver must be configured together")
        if (
            isinstance(ingress_ack_timeout, bool)
            or not isinstance(ingress_ack_timeout, (int, float))
            or not 0 < float(ingress_ack_timeout) < 3.0
        ):
            raise ValueError("invalid employee ingress ACK timeout")
        self._ingress_service = ingress_service
        self._ingress_binding_resolver = ingress_binding_resolver
        self._ingress_ack_timeout = float(ingress_ack_timeout)
        self._launcher = launcher or subprocess.Popen
        self._automatic_process_fallback = (
            sandbox_attestor is None and sandbox_prefix is None
        )
        self._sandbox_attestor = sandbox_attestor or attest_process_sandbox
        self._sandbox_prefix = (
            sandbox_prefix
            if sandbox_prefix is not None
            else (() if sandbox_attestor is not None else self._bwrap_prefix())
        )
        self._runtimes: dict[str, _Runtime] = {}
        self._generation_high_watermark: dict[str, int] = {}
        self._lock = threading.RLock()
        self._closed = False

    def _bwrap_prefix(self) -> tuple[str, ...]:
        """Build a minimal read-only runtime root with no Vault or project data."""
        repository_root = Path(__file__).resolve().parents[3]
        source_root = repository_root / "src"
        runtime_prefix = Path(sys.prefix).resolve()
        directory_targets = {Path("/etc"), repository_root}
        for target in (repository_root, runtime_prefix):
            parent = target
            while parent != parent.parent:
                directory_targets.add(parent)
                parent = parent.parent
        args: list[str] = [
            "/usr/bin/bwrap",
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for target in sorted(directory_targets, key=lambda item: (len(item.parts), str(item))):
            if target != Path("/"):
                args.extend(("--dir", str(target)))
        for path in (Path("/usr"), Path("/lib"), Path("/lib64")):
            if path.exists():
                args.extend(("--ro-bind", str(path), str(path)))
        args.extend(("--ro-bind", str(runtime_prefix), str(runtime_prefix)))
        if source_root.is_dir():
            args.extend(("--ro-bind", str(source_root), str(source_root)))
        if not self._worker_path.is_relative_to(source_root):
            args.extend(
                ("--ro-bind", str(self._worker_path), str(self._worker_path))
            )
        for path in (
            Path("/etc/hosts"),
            Path("/etc/nsswitch.conf"),
            Path("/etc/resolv.conf"),
            Path("/etc/ssl"),
        ):
            if path.exists():
                args.extend(("--ro-bind", str(path), str(path)))
        args.extend(("--chdir", "/tmp", "--"))
        return tuple(args)

    @contextmanager
    def employee_dispatch_guard(self):
        """Freeze live Channel authority without taking the Journal guard."""

        with self._lock:
            yield

    def launch_contract(
        self,
        *,
        bootstrap_fd: int,
        control_fd: int,
        event_fd: int,
        sandbox_prefix: tuple[str, ...] | None = None,
    ) -> ChannelLaunchContract:
        """Return the immutable fresh-exec and FD inheritance contract."""
        prefix = self._sandbox_prefix if sandbox_prefix is None else sandbox_prefix
        return ChannelLaunchContract(
            argv=prefix
            + (
                sys.executable,
                "-I",
                str(self._worker_path),
                str(bootstrap_fd),
                str(control_fd),
                str(event_fd),
            ),
            close_fds=True,
            pass_fds=(bootstrap_fd, control_fd, event_fd),
            env={"PYTHONUTF8": "1"},
        )

    def _launch_candidate(
        self,
        *,
        agent_id: str,
        app_id: str,
        generation: int,
        tenant_key: str,
        bot_principal_id: str,
        on_event: Callable[[dict[str, Any]], None],
        sandbox_prefix: tuple[str, ...],
    ) -> tuple[_Runtime, int]:
        bootstrap_r, bootstrap_w = os.pipe()
        control_r, control_w = os.pipe()
        event_r, event_w = os.pipe()
        child_fds = (bootstrap_r, control_r, event_w)
        parent_fds = (bootstrap_w, control_w, event_r)
        contract = self.launch_contract(
            bootstrap_fd=bootstrap_r,
            control_fd=control_r,
            event_fd=event_w,
            sandbox_prefix=sandbox_prefix,
        )
        try:
            process = self._launcher(
                contract.argv,
                close_fds=contract.close_fds,
                pass_fds=contract.pass_fds,
                env=contract.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            for descriptor in parent_fds:
                _close_fd(descriptor)
            raise RuntimeError("employee Channel launch failed") from None
        finally:
            for descriptor in child_fds:
                _close_fd(descriptor)

        status = ChannelProcessStatus(
            agent_id=agent_id,
            app_id=app_id,
            generation=generation,
            pid=process.pid,
            state=ChannelProcessState.STARTING,
            tenant_key=tenant_key,
            bot_principal_id=bot_principal_id,
        )
        return (
            _Runtime(
                process,
                control_w,
                event_r,
                status,
                on_event,
                tenant_key=tenant_key,
                bot_principal_id=bot_principal_id,
                requires_observed_connection=self._production_worker,
            ),
            bootstrap_w,
        )

    def start(
        self,
        agent_id: str,
        app_id: str,
        credential_ref: str,
        generation: int,
        on_event: Callable[[dict[str, Any]], None],
    ) -> ChannelProcessStatus:
        """Launch, attest, bootstrap, and await READY for one employee."""
        self._validate_start(agent_id, app_id, credential_ref, generation, on_event)
        if self._production_worker and self._ingress_service is None:
            raise RuntimeError("durable employee ingress is not configured")
        tenant_key = "tenant-test-unbound"
        bot_principal_id = "bot_test_unbound"
        if self._ingress_binding_resolver is not None:
            tenant_key, bot_principal_id = self._ingress_binding_resolver(
                agent_id, app_id
            )
            if (
                not isinstance(tenant_key, str)
                or not tenant_key
                or not isinstance(bot_principal_id, str)
                or not bot_principal_id.startswith("bot_")
            ):
                raise ValueError("invalid durable employee ingress binding")
        with self._lock:
            if self._closed:
                raise RuntimeError("employee Channel supervisor is closed")
            existing = self._runtimes.get(agent_id)
            if existing is not None and existing.process.poll() is None:
                if existing.status.generation == generation:
                    return existing.status
        if existing is not None and existing.process.poll() is None:
            self.stop(agent_id)
        with self._lock:
            high = self._generation_high_watermark.get(agent_id, 0)
            if generation <= high:
                raise ValueError("generation must advance after a worker has stopped")

        prefixes = [self._sandbox_prefix]
        if self._automatic_process_fallback and self._sandbox_prefix:
            prefixes.append(())
        runtime: _Runtime | None = None
        bootstrap_w = -1
        for attempt, prefix in enumerate(prefixes):
            is_fallback = attempt > 0
            try:
                runtime, bootstrap_w = self._launch_candidate(
                    agent_id=agent_id,
                    app_id=app_id,
                    generation=generation,
                    tenant_key=tenant_key,
                    bot_principal_id=bot_principal_id,
                    on_event=on_event,
                    sandbox_prefix=prefix,
                )
            except RuntimeError:
                if not is_fallback and len(prefixes) > 1:
                    logger.warning(
                        "employee Channel bwrap launch failed; using process fallback"
                    )
                    continue
                raise
            if is_fallback:
                attestation = SandboxAttestation(
                    runtime.process.pid,
                    False,
                    "process-fallback",
                    ("bwrap unavailable; isolated interpreter fallback",),
                )
            else:
                try:
                    attestation = self._sandbox_attestor(runtime.process.pid)
                except Exception:
                    attestation = SandboxAttestation(
                        runtime.process.pid,
                        False,
                        "attestation-error",
                    )
            runtime.status = replace(runtime.status, sandbox=attestation)
            if attestation.verified or is_fallback:
                if is_fallback:
                    logger.warning(
                        "employee Channel is using unverified process fallback"
                    )
                break
            _close_fd(bootstrap_w)
            bootstrap_w = -1
            self._fail_and_reap(runtime, "sandbox-unavailable")
            if len(prefixes) > 1:
                logger.warning(
                    "employee Channel bwrap attestation failed; using process fallback"
                )
                runtime = None
                continue
            with self._lock:
                self._runtimes[agent_id] = runtime
                self._generation_high_watermark[agent_id] = generation
            raise ChannelSandboxUnavailable()
        if runtime is None or bootstrap_w < 0:
            raise RuntimeError("employee Channel launch failed")
        with self._lock:
            high = self._generation_high_watermark.get(agent_id, 0)
            if generation <= high:
                _close_fd(bootstrap_w)
                self._fail_and_reap(runtime, "generation-race")
                raise ValueError("generation must advance after a worker has stopped")
            self._runtimes[agent_id] = runtime
            self._generation_high_watermark[agent_id] = generation

        try:
            secret = self._secret_resolver(credential_ref, agent_id, app_id)
            bootstrap = encode_bootstrap(
                ChannelBootstrap(
                    agent_id,
                    app_id,
                    generation,
                    secret,
                    tenant_key,
                    bot_principal_id,
                    self._ingress_ack_timeout,
                )
            )
        except Exception:
            _close_fd(bootstrap_w)
            self._fail_and_reap(runtime, "credential-resolution-failed")
            return runtime.status

        runtime.reader = threading.Thread(
            target=self._read_frames,
            args=(runtime,),
            name=f"employee-channel-{agent_id}-{generation}",
            daemon=True,
        )
        runtime.reader.start()
        try:
            _write_all(bootstrap_w, bootstrap)
        except OSError:
            _close_fd(bootstrap_w)
            self._fail_and_reap(runtime, "bootstrap-failed")
            return runtime.status
        finally:
            _close_fd(bootstrap_w)

        if not runtime.ready.wait(self._ready_timeout):
            self._fail_and_reap(runtime, "ready-timeout")
        return runtime.status

    def stop(self, agent_id: str) -> ChannelProcessStatus | None:
        """Gracefully stop one generation, escalating only after timeout."""
        with self._lock:
            runtime = self._runtimes.get(agent_id)
            if runtime is None:
                return None
            if runtime.status.state in {ChannelProcessState.STOPPED, ChannelProcessState.FAILED}:
                return runtime.status
            runtime.stopping = True
            runtime.status = replace(runtime.status, state=ChannelProcessState.STOPPING)
            self._fail_pending_sends(runtime)
        self._send_control(runtime, FrameType.STOP, {})
        _close_fd(runtime.control_fd)
        self._wait_or_terminate(runtime)
        with self._lock:
            runtime.status = replace(
                runtime.status,
                state=ChannelProcessState.STOPPED,
                stopped_at=time.time(),
                exit_code=runtime.process.poll(),
            )
        self._finish_reader(runtime)
        return runtime.status

    def send(
        self,
        agent_id: str,
        *,
        generation: int,
        target: str,
        message: Any,
        options: Any = None,
    ) -> ChannelSendReceipt:
        """Send through the exact READY employee generation and await its receipt."""
        if not isinstance(target, str) or not target:
            raise ValueError("target is required")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        request_id = f"send_{uuid.uuid4().hex}"
        pending = _PendingSend()
        with self._lock:
            runtime = self._runtimes.get(agent_id)
            if runtime is None or runtime.status.state is not ChannelProcessState.READY:
                raise RuntimeError("employee Channel is not ready")
            if runtime.status.generation != generation:
                raise ValueError("employee Channel generation mismatch")
            runtime.pending_sends[request_id] = pending
            try:
                sent = self._send_control(
                    runtime,
                    FrameType.SEND,
                    {
                        "request_id": request_id,
                        "target": target,
                        "message": message,
                        "options": options,
                    },
                )
            except ProtocolError:
                runtime.pending_sends.pop(request_id, None)
                raise ValueError("unsafe send payload") from None
            if not sent:
                runtime.pending_sends.pop(request_id, None)
                raise RuntimeError("employee Channel send failed")
        if not pending.completed.wait(self._send_timeout):
            with self._lock:
                runtime.pending_sends.pop(request_id, None)
            raise TimeoutError("employee Channel send receipt timed out")
        with self._lock:
            runtime.pending_sends.pop(request_id, None)
        if pending.success is not True:
            raise RuntimeError("employee Channel send was not acknowledged")
        return ChannelSendReceipt(
            request_id=request_id,
            success=True,
            app_id=pending.app_id,
            generation=pending.generation,
            connection_id=pending.connection_id,
            message_id=pending.message_id,
        )

    def update_card(
        self,
        agent_id: str,
        *,
        generation: int,
        message_id: str,
        card: dict[str, Any],
    ) -> ChannelSendReceipt:
        """Patch one pre-bound card through the exact READY employee generation."""
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id is required")
        if not isinstance(card, dict):
            raise ValueError("card must be an object")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        request_id = f"update_{uuid.uuid4().hex}"
        pending = _PendingSend(
            operation="update_card",
            expected_message_id=message_id,
        )
        with self._lock:
            runtime = self._runtimes.get(agent_id)
            if runtime is None or runtime.status.state is not ChannelProcessState.READY:
                raise RuntimeError("employee Channel is not ready")
            if runtime.status.generation != generation:
                raise ValueError("employee Channel generation mismatch")
            runtime.pending_sends[request_id] = pending
            try:
                sent = self._send_control(
                    runtime,
                    FrameType.UPDATE_CARD,
                    {
                        "request_id": request_id,
                        "message_id": message_id,
                        "card": card,
                    },
                )
            except ProtocolError:
                runtime.pending_sends.pop(request_id, None)
                raise ValueError("unsafe update card payload") from None
            if not sent:
                runtime.pending_sends.pop(request_id, None)
                raise RuntimeError("employee Channel update card failed")
        if not pending.completed.wait(self._send_timeout):
            with self._lock:
                runtime.pending_sends.pop(request_id, None)
            raise TimeoutError("employee Channel update card receipt timed out")
        with self._lock:
            runtime.pending_sends.pop(request_id, None)
        if pending.success is not True:
            raise RuntimeError("employee Channel update card was not acknowledged")
        return ChannelSendReceipt(
            request_id=request_id,
            success=True,
            app_id=pending.app_id,
            generation=pending.generation,
            connection_id=pending.connection_id,
            message_id=pending.message_id,
        )

    def status(self, agent_id: str) -> ChannelProcessStatus | None:
        """Return a secret-free immutable process snapshot."""
        with self._lock:
            runtime = self._runtimes.get(agent_id)
            if runtime is None:
                return None
            exit_code = runtime.process.poll()
            if (
                exit_code is not None
                and not runtime.stopping
                and runtime.status.state in {ChannelProcessState.STARTING, ChannelProcessState.READY}
            ):
                runtime.status = replace(
                    runtime.status,
                    state=ChannelProcessState.CRASHED,
                    stopped_at=time.time(),
                    exit_code=exit_code,
                    error_code="worker-exited",
                )
                runtime.ready.set()
            return runtime.status

    def recover(self, desired: Iterable[DesiredEmployeeChannel]) -> dict[str, ChannelProcessStatus]:
        """Reconcile live children to the durable desired employee set."""
        desired_by_agent: dict[str, DesiredEmployeeChannel] = {}
        for item in desired:
            if item.agent_id in desired_by_agent:
                raise ValueError("duplicate desired employee Channel")
            desired_by_agent[item.agent_id] = item
        with self._lock:
            current_ids = set(self._runtimes)
        for agent_id in current_ids - set(desired_by_agent):
            self.stop(agent_id)
        result: dict[str, ChannelProcessStatus] = {}
        for agent_id, item in desired_by_agent.items():
            current = self.status(agent_id)
            if current is not None and current.state is ChannelProcessState.READY and current.generation == item.generation:
                result[agent_id] = current
                continue
            if current is not None and current.state not in {ChannelProcessState.STOPPED, ChannelProcessState.FAILED, ChannelProcessState.CRASHED}:
                self.stop(agent_id)
            result[agent_id] = self.start(
                item.agent_id,
                item.app_id,
                item.credential_ref,
                item.generation,
                item.on_event,
            )
        return result

    def close(self) -> None:
        """Stop all owned children and make admission permanently closed."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            agent_ids = list(self._runtimes)
        for agent_id in agent_ids:
            self.stop(agent_id)

    def _validate_start(self, agent_id: str, app_id: str, credential_ref: str, generation: int, on_event: Any) -> None:
        if not all(isinstance(value, str) and value for value in (agent_id, app_id, credential_ref)):
            raise ValueError("agent_id, app_id, and credential_ref are required")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            raise ValueError("generation must be a positive integer")
        if not callable(on_event):
            raise TypeError("on_event must be callable")

    def _read_frames(self, runtime: _Runtime) -> None:
        try:
            with os.fdopen(runtime.event_fd, "rb", buffering=0) as stream:
                while True:
                    raw = stream.readline(MAX_FRAME_BYTES + 1)
                    if not raw:
                        break
                    try:
                        frame = decode_frame(raw)
                    except ProtocolError:
                        with self._lock:
                            runtime.status = replace(runtime.status, error_code="protocol-error")
                        continue
                    if frame.agent_id != runtime.status.agent_id or frame.generation != runtime.status.generation:
                        with self._lock:
                            runtime.status = replace(runtime.status, stale_frames=runtime.status.stale_frames + 1)
                        continue
                    if frame.sequence <= runtime.inbound_sequence:
                        with self._lock:
                            runtime.status = replace(runtime.status, stale_frames=runtime.status.stale_frames + 1)
                        continue
                    runtime.inbound_sequence = frame.sequence
                    self._accept_frame(runtime, frame)
        finally:
            runtime.event_fd = -1
            exit_code = runtime.process.poll()
            should_reap = False
            with self._lock:
                active = runtime.status.state in {
                    ChannelProcessState.STARTING,
                    ChannelProcessState.READY,
                }
                if not runtime.stopping and active:
                    runtime.stopping = True
                    crashed = exit_code is not None
                    runtime.status = replace(
                        runtime.status,
                        state=(
                            ChannelProcessState.CRASHED
                            if crashed
                            else ChannelProcessState.FAILED
                        ),
                        ready_at=None,
                        stopped_at=time.time(),
                        exit_code=exit_code,
                        error_code=(
                            "worker-exited-before-ready"
                            if crashed
                            and runtime.status.state is ChannelProcessState.STARTING
                            else "worker-exited"
                            if crashed
                            else "event-pipe-closed"
                        ),
                    )
                    self._fail_pending_sends(runtime)
                    runtime.ready.set()
                    should_reap = not crashed
            if should_reap:
                _close_fd(runtime.control_fd)
                runtime.control_fd = -1
                self._wait_or_terminate(runtime)
                with self._lock:
                    runtime.status = replace(
                        runtime.status,
                        stopped_at=time.time(),
                        exit_code=runtime.process.poll(),
                    )

    def _accept_frame(self, runtime: _Runtime, frame: ChannelFrame) -> None:
        if frame.frame_type is FrameType.READY:
            identity = frame.payload.get("identity")
            if not isinstance(identity, dict):
                with self._lock:
                    runtime.status = replace(runtime.status, error_code="invalid-ready")
                return
            connection = frame.payload.get("connection")
            if runtime.requires_observed_connection and (
                not isinstance(connection, dict)
                or connection.get("observed") is not True
                or connection.get("secure") is not True
            ):
                with self._lock:
                    runtime.status = replace(
                        runtime.status, error_code="unobserved-connection"
                    )
                return
            metadata = {key: value for key, value in frame.payload.items() if key != "identity"}
            with self._lock:
                runtime.status = replace(
                    runtime.status,
                    state=ChannelProcessState.READY,
                    identity=dict(identity),
                    ready_metadata=metadata,
                    ready_at=time.time(),
                    error_code="",
                )
            runtime.ready.set()
            return
        if frame.frame_type is FrameType.INGRESS:
            self._accept_ingress(runtime, frame)
        elif frame.frame_type is FrameType.EVENT:
            if frame.payload.get("event") == "reconnecting":
                with self._lock:
                    runtime.status = replace(
                        runtime.status,
                        state=ChannelProcessState.STARTING,
                        ready_at=None,
                        error_code="channel-reconnecting",
                    )
                    runtime.ready.clear()
            try:
                runtime.on_event(dict(frame.payload))
            except Exception:
                with self._lock:
                    runtime.status = replace(runtime.status, error_code="event-callback-failed")
        elif frame.frame_type is FrameType.ERROR:
            code = frame.payload.get("error_code")
            with self._lock:
                runtime.status = replace(runtime.status, error_code=code if isinstance(code, str) else "worker-error")
        elif frame.frame_type is FrameType.HEALTH:
            operation = frame.payload.get("operation")
            if operation in {"send", "update_card"}:
                request_id = frame.payload.get("request_id")
                success = frame.payload.get("success")
                if isinstance(request_id, str) and isinstance(success, bool):
                    with self._lock:
                        pending = runtime.pending_sends.get(request_id)
                        if pending is not None:
                            app_id = frame.payload.get("app_id")
                            generation = frame.payload.get("generation")
                            connection_id = frame.payload.get("connection_id")
                            message_id = frame.payload.get("message_id")
                            valid_evidence = (
                                success is True
                                and app_id == runtime.status.app_id
                                and generation == runtime.status.generation
                                and isinstance(connection_id, str)
                                and connection_id
                                == runtime.status.ready_metadata.get("connection_id")
                                and isinstance(message_id, str)
                                and bool(message_id)
                                and (
                                    pending.operation == "send"
                                    or message_id == pending.expected_message_id
                                )
                            )
                            pending.success = valid_evidence
                            if valid_evidence:
                                pending.app_id = app_id
                                pending.generation = generation
                                pending.connection_id = connection_id
                                pending.message_id = message_id
                            elif success is True:
                                runtime.status = replace(
                                    runtime.status,
                                    error_code=f"invalid-{operation.replace('_', '-')}-receipt",
                                )
                            pending.completed.set()
            with self._lock:
                runtime.status = replace(
                    runtime.status,
                    ready_metadata={**runtime.status.ready_metadata, "health": dict(frame.payload)},
                )

    def _accept_ingress(self, runtime: _Runtime, frame: ChannelFrame) -> None:
        service = self._ingress_service
        try:
            metadata = EmployeeIngressMetadata.from_dict(frame.payload["metadata"])
            payload = EmployeeIngressPayload.from_dict(frame.payload["payload"])
            with self._lock:
                current = self._runtimes.get(runtime.status.agent_id)
                valid = (
                    service is not None
                    and current is runtime
                    and runtime.status.state is ChannelProcessState.READY
                    and metadata.tenant_key == runtime.tenant_key
                    and metadata.agent_id == runtime.status.agent_id
                    and metadata.bot_principal_id == runtime.bot_principal_id
                    and metadata.app_id == runtime.status.app_id
                    and metadata.channel_generation == runtime.status.generation
                    and metadata.connection_id
                    == runtime.status.ready_metadata.get("connection_id")
                    and frame.payload["app_id"] == runtime.status.app_id
                    and frame.payload["connection_id"] == metadata.connection_id
                )
            if not valid or service is None:
                raise ValueError("employee ingress runtime binding mismatch")
            ack = service.accept(
                metadata,
                payload,
                request_id=frame.payload["request_id"],
                action_correlation=frame.payload["action_correlation"],
            )
            if not isinstance(ack, EmployeeIngressAck):
                raise TypeError("employee ingress service returned invalid ACK")
            sent = self._send_control(
                runtime,
                FrameType.INGRESS_ACK,
                {
                    "request_id": ack.request_id,
                    "app_id": ack.app_id,
                    "connection_id": ack.connection_id,
                    "ack": ack.to_dict(),
                },
            )
            if not sent:
                raise BrokenPipeError("employee ingress ACK pipe closed")
            try:
                runtime.on_event(
                    {
                        "event": "durableIngressAccepted",
                        "data": {
                            "acceptance_id": ack.acceptance.acceptance_id,
                            "agent_id": ack.agent_id,
                            "generation": ack.channel_generation,
                        },
                    }
                )
            except Exception:
                with self._lock:
                    runtime.status = replace(
                        runtime.status,
                        error_code="ingress-control-callback-failed",
                    )
        except Exception:
            with self._lock:
                runtime.status = replace(
                    runtime.status,
                    error_code="ingress-not-acknowledged",
                )

    def _send_control(self, runtime: _Runtime, frame_type: FrameType, payload: dict[str, Any]) -> bool:
        with runtime.control_lock:
            if runtime.control_fd < 0:
                return False
            runtime.outbound_sequence += 1
            raw = encode_frame(
                ChannelFrame(
                    frame_type,
                    runtime.status.agent_id,
                    runtime.status.generation,
                    runtime.outbound_sequence,
                    payload,
                )
            )
            try:
                _write_all(runtime.control_fd, raw)
            except OSError:
                return False
            return True

    def _fail_and_reap(self, runtime: _Runtime, error_code: str) -> None:
        runtime.stopping = True
        with self._lock:
            self._fail_pending_sends(runtime)
        _close_fd(runtime.control_fd)
        runtime.control_fd = -1
        self._wait_or_terminate(runtime)
        with self._lock:
            runtime.status = replace(
                runtime.status,
                state=ChannelProcessState.FAILED,
                stopped_at=time.time(),
                exit_code=runtime.process.poll(),
                error_code=error_code,
            )
        runtime.ready.set()
        self._finish_reader(runtime)

    @staticmethod
    def _fail_pending_sends(runtime: _Runtime) -> None:
        for pending in runtime.pending_sends.values():
            pending.success = False
            pending.completed.set()

    def _wait_or_terminate(self, runtime: _Runtime) -> None:
        try:
            runtime.process.wait(timeout=self._stop_timeout)
        except subprocess.TimeoutExpired:
            runtime.process.terminate()
            try:
                runtime.process.wait(timeout=self._stop_timeout)
            except subprocess.TimeoutExpired:
                runtime.process.kill()
                runtime.process.wait(timeout=self._stop_timeout)

    def _finish_reader(self, runtime: _Runtime) -> None:
        if runtime.event_fd >= 0:
            _close_fd(runtime.event_fd)
            runtime.event_fd = -1
        if runtime.reader is not None and runtime.reader is not threading.current_thread():
            runtime.reader.join(timeout=self._stop_timeout)


def attest_process_sandbox(pid: int) -> SandboxAttestation:
    """Verify user, mount and PID namespaces plus an absent project secret root."""
    deadline = time.monotonic() + 1.0
    while True:
        try:
            parent_user_ns = os.readlink("/proc/self/ns/user")
            child_user_ns = os.readlink(f"/proc/{pid}/ns/user")
            parent_mount_ns = os.readlink("/proc/self/ns/mnt")
            child_mount_ns = os.readlink(f"/proc/{pid}/ns/mnt")
            parent_pid_ns = os.readlink("/proc/self/ns/pid")
            child_pid_ns = os.readlink(f"/proc/{pid}/ns/pid")
            repository_root = Path(__file__).resolve().parents[3]
            child_repository = Path(f"/proc/{pid}/root") / repository_root.relative_to("/")
            source_visible = (child_repository / "src").is_dir()
            secrets_hidden = not any(
                (child_repository / name).exists()
                for name in (".env", ".git", ".Memory")
            )
            if (
                child_user_ns != parent_user_ns
                and child_mount_ns != parent_mount_ns
                and child_pid_ns != parent_pid_ns
                and source_visible
                and secrets_hidden
            ):
                return SandboxAttestation(
                    pid,
                    True,
                    "bwrap-filesystem",
                    ("user/mount/pid namespaces", "project secrets absent"),
                )
        except (OSError, StopIteration, ValueError):
            if time.monotonic() >= deadline:
                return SandboxAttestation(
                    pid,
                    False,
                    "unverified",
                    ("sandbox inspection failed",),
                )
        if time.monotonic() >= deadline:
            return SandboxAttestation(
                pid,
                False,
                "unverified",
                (
                    "user/mount/pid namespace isolation not attested",
                    "project secret paths are not proven absent",
                ),
            )
        time.sleep(0.01)


def _write_all(fd: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise BrokenPipeError("employee Channel IPC closed")
        view = view[written:]


def _close_fd(fd: int) -> None:
    if fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass
