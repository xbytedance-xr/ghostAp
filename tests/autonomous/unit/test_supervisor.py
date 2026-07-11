"""Unit tests for Supervisor lifecycle."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from src.autonomous.supervisor.supervisor import (
    ChannelHealth,
    RecoveryReport,
    Supervisor,
    SupervisorState,
    WorkerProcess,
    WorkerState,
)


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def write_event(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
def state_dir() -> str:
    d = tempfile.mkdtemp(prefix="supervisor_test_")
    yield d
    # cleanup
    import shutil
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def journal() -> FakeJournal:
    return FakeJournal()


@pytest.fixture
def supervisor(state_dir: str, journal: FakeJournal) -> Supervisor:
    return Supervisor(
        state_dir=state_dir,
        journal=journal,
        max_workers=3,
        heartbeat_interval=1.0,
        worker_timeout=2.0,
    )


class TestSupervisorLifecycle:
    @pytest.mark.asyncio
    async def test_start(self, supervisor: Supervisor, journal: FakeJournal, state_dir: str) -> None:
        await supervisor.start()
        assert supervisor.state == SupervisorState.RUNNING

        # PID file written
        pid_path = os.path.join(state_dir, "supervisor.pid")
        assert os.path.exists(pid_path)
        with open(pid_path) as f:
            assert f.read() == str(os.getpid())

        # Journal events
        event_types = [e[0] for e in journal.events]
        assert "supervisor.starting" in event_types
        assert "supervisor.started" in event_types

    @pytest.mark.asyncio
    async def test_shutdown(self, supervisor: Supervisor, journal: FakeJournal, state_dir: str) -> None:
        await supervisor.start()
        await supervisor.shutdown()
        assert supervisor.state == SupervisorState.STOPPED

        # PID file removed
        pid_path = os.path.join(state_dir, "supervisor.pid")
        assert not os.path.exists(pid_path)

        event_types = [e[0] for e in journal.events]
        assert "supervisor.stopping" in event_types
        assert "supervisor.stopped" in event_types

    @pytest.mark.asyncio
    async def test_recover(self, supervisor: Supervisor, journal: FakeJournal) -> None:
        await supervisor.start()
        report = await supervisor.recover()

        assert isinstance(report, RecoveryReport)
        assert supervisor.state == SupervisorState.RUNNING

        event_types = [e[0] for e in journal.events]
        assert "supervisor.recovery_start" in event_types
        assert "supervisor.recovery_complete" in event_types

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, supervisor: Supervisor) -> None:
        await supervisor.start()
        assert supervisor.state == SupervisorState.RUNNING

        await supervisor.recover()
        assert supervisor.state == SupervisorState.RUNNING

        await supervisor.shutdown()
        assert supervisor.state == SupervisorState.STOPPED


class TestWorkerManagement:
    @pytest.mark.asyncio
    async def test_spawn_worker(self, supervisor: Supervisor, journal: FakeJournal) -> None:
        await supervisor.start()
        worker = await supervisor.spawn_worker(
            employee_id="emp_1",
            run_id="run_1",
            attempt_id="att_1",
            command=["sleep", "60"],
        )
        assert worker is not None
        assert worker.state == WorkerState.RUNNING
        assert worker.pid is not None
        assert worker.employee_id == "emp_1"

        event_types = [e[0] for e in journal.events]
        assert "supervisor.worker_spawned" in event_types

        # Cleanup
        await supervisor.shutdown()

    @pytest.mark.asyncio
    async def test_max_workers_limit(self, supervisor: Supervisor) -> None:
        await supervisor.start()

        workers = []
        for i in range(3):
            w = await supervisor.spawn_worker(
                employee_id=f"emp_{i}",
                run_id=f"run_{i}",
                attempt_id=f"att_{i}",
                command=["sleep", "60"],
            )
            workers.append(w)
            assert w is not None

        # Fourth should fail
        w4 = await supervisor.spawn_worker(
            employee_id="emp_3",
            run_id="run_3",
            attempt_id="att_3",
            command=["sleep", "60"],
        )
        assert w4 is None

        await supervisor.shutdown()

    @pytest.mark.asyncio
    async def test_heartbeat(self, supervisor: Supervisor) -> None:
        await supervisor.start()
        worker = await supervisor.spawn_worker(
            employee_id="emp_1",
            run_id="run_1",
            attempt_id="att_1",
            command=["sleep", "60"],
        )
        assert worker is not None

        assert supervisor.heartbeat(worker.worker_id) is True
        assert supervisor.heartbeat("nonexistent_worker") is False

        await supervisor.shutdown()

    @pytest.mark.asyncio
    async def test_check_workers_timeout(self, supervisor: Supervisor) -> None:
        await supervisor.start()
        worker = await supervisor.spawn_worker(
            employee_id="emp_1",
            run_id="run_1",
            attempt_id="att_1",
            command=["sleep", "60"],
        )
        assert worker is not None

        # Force heartbeat to be old
        import time
        worker.last_heartbeat = time.time() - 100

        failed = await supervisor.check_workers()
        assert len(failed) == 1
        assert failed[0].worker_id == worker.worker_id
        assert failed[0].state == WorkerState.FAILED

        await supervisor.shutdown()


class TestChannelManagement:
    @pytest.mark.asyncio
    async def test_register_channel(self, supervisor: Supervisor) -> None:
        await supervisor.start()
        supervisor.register_channel("feishu_ws")

        status = supervisor.get_status()
        assert "feishu_ws" in status["channels"]

        await supervisor.shutdown()

    @pytest.mark.asyncio
    async def test_channel_health_degraded(self, supervisor: Supervisor) -> None:
        await supervisor.start()
        supervisor.register_channel("ch1")
        supervisor.update_channel_health("ch1", connected=False, error="timeout")

        assert supervisor.state == SupervisorState.DEGRADED

        supervisor.update_channel_health("ch1", connected=True)
        assert supervisor.state == SupervisorState.RUNNING

        await supervisor.shutdown()

    @pytest.mark.asyncio
    async def test_get_status(self, supervisor: Supervisor) -> None:
        await supervisor.start()
        status = supervisor.get_status()
        assert status["state"] == "running"
        assert status["max_workers"] == 3
        assert status["active_workers"] == 0
        assert "uptime" in status

        await supervisor.shutdown()


class TestSupervisorNoJournal:
    @pytest.mark.asyncio
    async def test_works_without_journal(self, state_dir: str) -> None:
        sup = Supervisor(state_dir=state_dir, journal=None, max_workers=2)
        await sup.start()
        assert sup.state == SupervisorState.RUNNING
        await sup.recover()
        assert sup.state == SupervisorState.RUNNING
        await sup.shutdown()
        assert sup.state == SupervisorState.STOPPED
