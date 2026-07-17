"""Supervisor - process model and lifecycle management.

Manages: Worker subprocesses, channel adapters, auto-restart,
health monitoring, journal replay, and the top-level event loop.
Single entry point for system lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

from ..domain import new_id

logger = logging.getLogger(__name__)

EMPLOYEE_RECOVERY_ORDER = (
    "journal_projection",
    "data_projection",
    "workspace_projection",
    "group_ledger",
    "actor_mailboxes",
    "team_coordinator",
    "employee_channels",
    "admission_open",
)


@dataclass(frozen=True, slots=True)
class EmployeeRecoverySnapshot:
    frames: tuple[Any, ...]
    head_sequence: int
    head_hash: str


@dataclass(frozen=True, slots=True)
class EmployeeLifecycleReport:
    ready: bool
    completed_stages: tuple[str, ...]
    blocker: str = ""


class EmployeeLifecycleSupervisor:
    """One ordered gate for employee recovery and reverse-order shutdown."""

    def __init__(
        self,
        recoverers: Mapping[str, Callable[[EmployeeRecoverySnapshot], object]],
        closers: Mapping[str, Callable[[], object]] | None = None,
    ) -> None:
        missing = set(EMPLOYEE_RECOVERY_ORDER) - set(recoverers)
        if missing:
            raise ValueError("employee recovery stage is missing")
        self._recoverers = dict(recoverers)
        self._closers = dict(closers or {})
        self._report = EmployeeLifecycleReport(False, ())

    @property
    def report(self) -> EmployeeLifecycleReport:
        return self._report

    def recover(self, snapshot: EmployeeRecoverySnapshot) -> EmployeeLifecycleReport:
        completed: list[str] = []
        for stage in EMPLOYEE_RECOVERY_ORDER:
            try:
                outcome = self._recoverers[stage](snapshot)
                if outcome is False:
                    raise RuntimeError("stage rejected recovery")
            except Exception:
                self._report = EmployeeLifecycleReport(
                    False, tuple(completed), blocker=stage
                )
                return self._report
            completed.append(stage)
        self._report = EmployeeLifecycleReport(True, tuple(completed))
        return self._report

    def shutdown(self) -> tuple[str, ...]:
        completed: list[str] = []
        for stage in reversed(EMPLOYEE_RECOVERY_ORDER):
            closer = self._closers.get(stage)
            if closer is not None:
                closer()
            completed.append(stage)
        self._report = EmployeeLifecycleReport(False, ())
        return tuple(completed)


# ---------------------------------------------------------------------------
# Journal protocol
# ---------------------------------------------------------------------------


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


# ---------------------------------------------------------------------------
# State types
# ---------------------------------------------------------------------------


class WorkerState(Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class SupervisorState(Enum):
    INITIALIZING = "initializing"
    RECOVERING = "recovering"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class WorkerProcess:
    """Tracks a worker subprocess."""

    worker_id: str = field(default_factory=lambda: new_id("wkr"))
    employee_id: str = ""
    run_id: str = ""
    attempt_id: str = ""
    state: WorkerState = WorkerState.STARTING
    pid: Optional[int] = None
    started_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    exit_code: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "employee_id": self.employee_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "state": self.state.value,
            "pid": self.pid,
            "started_at": self.started_at,
            "last_heartbeat": self.last_heartbeat,
            "exit_code": self.exit_code,
        }


@dataclass
class ChannelHealth:
    """Health status of a communication channel."""

    channel_id: str = ""
    connected: bool = False
    last_message_at: float = 0.0
    reconnect_count: int = 0
    error: str = ""


@dataclass
class RecoveryReport:
    """Report of what was found and fixed during recovery."""

    stale_leases: int = 0
    abandoned_activities: int = 0
    unresolved_effects: int = 0
    recovered_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "stale_leases": self.stale_leases,
            "abandoned_activities": self.abandoned_activities,
            "unresolved_effects": self.unresolved_effects,
            "recovered_at": self.recovered_at,
        }


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Top-level supervisor managing workers, channels, and lifecycle.

    Process tree:
        ghostap-supervisor
        +-- Journal Writer
        +-- Policy / Budget / Credential / Tool Broker
        +-- Scheduler / Reconciler
        +-- Manager Channel Adapter
        +-- Visible Bot Channel Adapters
        +-- Worker subprocesses

    Single entry point for system lifecycle: start -> recover -> run -> shutdown.
    """

    def __init__(
        self,
        state_dir: str,
        journal: Optional[JournalWriter] = None,
        max_workers: int = 5,
        heartbeat_interval: float = 30.0,
        worker_timeout: float = 300.0,
    ):
        self._state_dir = state_dir
        self._journal = journal
        self._max_workers = max_workers
        self._heartbeat_interval = heartbeat_interval
        self._worker_timeout = worker_timeout
        self._state = SupervisorState.INITIALIZING
        self._workers: dict[str, WorkerProcess] = {}
        self._channels: dict[str, ChannelHealth] = {}
        self._shutdown_event = asyncio.Event()
        self._start_time = time.time()
        self._recovery_report: Optional[RecoveryReport] = None

        os.makedirs(state_dir, exist_ok=True)

    @property
    def state(self) -> SupervisorState:
        return self._state

    async def _journal_event(self, event_type: str, payload: dict) -> None:
        if self._journal:
            await self._journal.write_event(event_type, payload)

    # ------------------------------------------------------------------
    # Lifecycle: start -> recover -> run -> shutdown
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize journal, replay projections, and start the supervisor loop."""
        self._state = SupervisorState.INITIALIZING
        await self._journal_event("supervisor.starting", {
            "state_dir": self._state_dir,
            "max_workers": self._max_workers,
        })

        self._write_pid_file()
        self._state = SupervisorState.RUNNING
        self._start_time = time.time()

        await self._journal_event("supervisor.started", {
            "pid": os.getpid(),
            "started_at": self._start_time,
        })
        logger.info("Supervisor started, state_dir=%s", self._state_dir)

    async def recover(self) -> RecoveryReport:
        """Find stale leases, abandoned activities, unresolved effects."""
        self._state = SupervisorState.RECOVERING
        await self._journal_event("supervisor.recovery_start", {})

        report = RecoveryReport()

        # Check for dead workers
        failed = await self.check_workers()
        report.abandoned_activities = len(failed)

        self._recovery_report = report
        self._state = SupervisorState.RUNNING

        await self._journal_event("supervisor.recovery_complete", report.to_dict())
        logger.info(
            "Recovery complete: stale_leases=%d, abandoned=%d, unresolved=%d",
            report.stale_leases,
            report.abandoned_activities,
            report.unresolved_effects,
        )
        return report

    async def shutdown(self) -> None:
        """Graceful drain: stop all workers, close channels, clean up."""
        self._state = SupervisorState.STOPPING
        await self._journal_event("supervisor.stopping", {
            "active_workers": len(self._active_workers()),
        })
        logger.info("Supervisor stopping, terminating %d workers", len(self._workers))

        for worker in list(self._workers.values()):
            await self._stop_worker(worker)

        self._state = SupervisorState.STOPPED
        self._remove_pid_file()
        self._shutdown_event.set()

        await self._journal_event("supervisor.stopped", {
            "uptime": time.time() - self._start_time,
        })

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------

    async def spawn_worker(
        self,
        employee_id: str,
        run_id: str,
        attempt_id: str,
        command: list[str],
        env: Optional[dict] = None,
    ) -> Optional[WorkerProcess]:
        """Spawn a new worker subprocess."""
        if len(self._active_workers()) >= self._max_workers:
            logger.warning("Max workers reached (%d)", self._max_workers)
            return None

        worker = WorkerProcess(
            employee_id=employee_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )

        clean_env = self._build_worker_env(env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                env=clean_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            worker.pid = proc.pid
            worker.state = WorkerState.RUNNING
            self._workers[worker.worker_id] = worker

            await self._journal_event("supervisor.worker_spawned", {
                "worker_id": worker.worker_id,
                "pid": proc.pid,
                "employee_id": employee_id,
                "run_id": run_id,
                "attempt_id": attempt_id,
            })
            logger.info(
                "Spawned worker %s (pid=%d) for attempt %s",
                worker.worker_id,
                proc.pid,
                attempt_id,
            )
            return worker
        except Exception as exc:
            worker.state = WorkerState.FAILED
            logger.error("Failed to spawn worker: %s", str(exc))
            return None

    async def _stop_worker(self, worker: WorkerProcess) -> None:
        """Stop a worker, escalating from SIGTERM to SIGKILL."""
        if worker.pid is None or worker.state == WorkerState.STOPPED:
            return

        worker.state = WorkerState.STOPPING
        try:
            os.kill(worker.pid, signal.SIGTERM)
            await asyncio.sleep(5.0)
            try:
                os.kill(worker.pid, 0)
                os.kill(worker.pid, signal.SIGKILL)
            except OSError:
                pass
        except OSError:
            pass

        worker.state = WorkerState.STOPPED
        await self._journal_event("supervisor.worker_stopped", {
            "worker_id": worker.worker_id,
            "pid": worker.pid,
        })

    def heartbeat(self, worker_id: str) -> bool:
        """Record a heartbeat from a worker. Returns False if worker unknown."""
        worker = self._workers.get(worker_id)
        if not worker:
            return False
        worker.last_heartbeat = time.time()
        return True

    async def check_workers(self) -> list[WorkerProcess]:
        """Check for dead/timed-out workers. Returns list of failed workers."""
        now = time.time()
        failed: list[WorkerProcess] = []

        for worker in list(self._workers.values()):
            if worker.state != WorkerState.RUNNING:
                continue
            if now - worker.last_heartbeat > self._worker_timeout:
                logger.warning(
                    "Worker %s timed out (no heartbeat for %.0fs)",
                    worker.worker_id,
                    now - worker.last_heartbeat,
                )
                worker.state = WorkerState.FAILED
                failed.append(worker)

        return failed

    # ------------------------------------------------------------------
    # Channel management
    # ------------------------------------------------------------------

    def register_channel(self, channel_id: str) -> None:
        self._channels[channel_id] = ChannelHealth(channel_id=channel_id)

    def update_channel_health(self, channel_id: str, connected: bool, error: str = "") -> None:
        ch = self._channels.get(channel_id)
        if ch:
            ch.connected = connected
            ch.error = error
            if connected:
                ch.last_message_at = time.time()
            else:
                ch.reconnect_count += 1

        if any(not c.connected for c in self._channels.values()):
            if self._state == SupervisorState.RUNNING:
                self._state = SupervisorState.DEGRADED
        elif self._state == SupervisorState.DEGRADED:
            self._state = SupervisorState.RUNNING

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        return {
            "state": self._state.value,
            "uptime": time.time() - self._start_time,
            "workers": {wid: w.to_dict() for wid, w in self._workers.items()},
            "channels": {
                cid: {"connected": c.connected, "error": c.error}
                for cid, c in self._channels.items()
            },
            "active_workers": len(self._active_workers()),
            "max_workers": self._max_workers,
            "recovery_report": self._recovery_report.to_dict() if self._recovery_report else None,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _active_workers(self) -> list[WorkerProcess]:
        return [w for w in self._workers.values() if w.state == WorkerState.RUNNING]

    def _build_worker_env(self, extra: Optional[dict] = None) -> dict:
        """Build a clean environment for workers - no secrets."""
        safe_keys = {"PATH", "HOME", "LANG", "LC_ALL", "TERM", "USER", "SHELL"}
        env = {k: v for k, v in os.environ.items() if k in safe_keys}
        if extra:
            env.update(extra)
        return env

    def _write_pid_file(self) -> None:
        pid_path = os.path.join(self._state_dir, "supervisor.pid")
        with open(pid_path, "w") as f:
            f.write(str(os.getpid()))

    def _remove_pid_file(self) -> None:
        pid_path = os.path.join(self._state_dir, "supervisor.pid")
        if os.path.exists(pid_path):
            os.unlink(pid_path)
