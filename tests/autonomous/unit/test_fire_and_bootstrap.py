"""Tests for /fire saga and department bootstrap."""

from __future__ import annotations

import pytest

from src.autonomous.provisioning.bootstrap import (
    AgentDepartmentBootstrap,
    DepartmentBootstrapResult,
)
from src.autonomous.provisioning.fire import (
    FirePhase,
    FireSaga,
    FireState,
)


class _FakeChannel:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.stopped: list[str] = []

    def stop(self, agent_id):
        if self._fail:
            raise RuntimeError("channel stop failed")
        self.stopped.append(agent_id)


class _FakeSlash:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.cleaned: list[str] = []

    def cleanup_all(self, app_id):
        if self._fail:
            raise RuntimeError("slash cleanup failed")
        self.cleaned.append(app_id)


class _FakeVault:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.destroyed: list[str] = []

    def destroy(self, credential_ref):
        if self._fail:
            raise RuntimeError("vault destroy failed")
        self.destroyed.append(credential_ref)
        return True


class _FakeArchive:
    def __init__(self) -> None:
        self.archived: list[str] = []

    def archive_employee(self, agent_id):
        self.archived.append(agent_id)


class _FakeJournal:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def record_fire_event(self, *, agent_id, phase, payload):
        self.events.append({"agent_id": agent_id, "phase": phase})


class TestFireSaga:
    def test_full_fire_happy_path(self) -> None:
        channel = _FakeChannel()
        slash = _FakeSlash()
        vault = _FakeVault()
        archive = _FakeArchive()
        journal = _FakeJournal()
        saga = FireSaga(
            channel=channel, slash=slash, vault=vault,
            archive=archive, journal=journal,
        )
        state = saga.fire(agent_id="agt_alpha", app_id="app_1", credential_ref="cred_1")
        assert state.phase == FirePhase.COMPLETED
        assert state.is_terminal
        assert "agt_alpha" in channel.stopped
        assert "app_1" in slash.cleaned
        assert "cred_1" in vault.destroyed
        assert "agt_alpha" in archive.archived
        assert len(journal.events) == 4

    def test_channel_failure_stops_saga(self) -> None:
        saga = FireSaga(
            channel=_FakeChannel(fail=True),
            slash=_FakeSlash(),
            vault=_FakeVault(),
            archive=_FakeArchive(),
            journal=_FakeJournal(),
        )
        state = saga.fire(agent_id="agt_alpha", app_id="app_1", credential_ref="cred_1")
        assert state.phase == FirePhase.FAILED
        assert "channel" in state.error

    def test_slash_failure_stops_saga(self) -> None:
        saga = FireSaga(
            channel=_FakeChannel(),
            slash=_FakeSlash(fail=True),
            vault=_FakeVault(),
            archive=_FakeArchive(),
            journal=_FakeJournal(),
        )
        state = saga.fire(agent_id="agt_alpha", app_id="app_1", credential_ref="cred_1")
        assert state.phase == FirePhase.FAILED
        assert "slash" in state.error

    def test_vault_failure_stops_saga(self) -> None:
        saga = FireSaga(
            channel=_FakeChannel(),
            slash=_FakeSlash(),
            vault=_FakeVault(fail=True),
            archive=_FakeArchive(),
            journal=_FakeJournal(),
        )
        state = saga.fire(agent_id="agt_alpha", app_id="app_1", credential_ref="cred_1")
        assert state.phase == FirePhase.FAILED
        assert "vault" in state.error


class TestDepartmentBootstrap:
    def test_dormant_mode_when_limit_zero(self) -> None:
        bootstrap = AgentDepartmentBootstrap(settings=None, visible_employee_limit=0)
        result = bootstrap.start()
        assert result.healthy
        assert bootstrap.is_ready

    def test_shutdown(self) -> None:
        bootstrap = AgentDepartmentBootstrap(settings=None, visible_employee_limit=0)
        bootstrap.start()
        bootstrap.shutdown()
        assert not bootstrap.is_ready
