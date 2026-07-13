"""Tests for /fire saga and department bootstrap."""

from __future__ import annotations

from src.autonomous.provisioning.bootstrap import (
    AgentDepartmentBootstrap,
)
from src.autonomous.provisioning.fire import (
    FirePhase,
    FireSaga,
)


class _FakeChannel:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.stopped: list[str] = []

    def stop(self, agent_id):
        if self._fail:
            raise RuntimeError("channel stop failed")
        self.stopped.append(agent_id)


class _FakeContext:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.invalidated: list[str] = []

    def invalidate_employee_context(self, agent_id):
        if self._fail:
            raise RuntimeError("context drain failed")
        self.invalidated.append(agent_id)


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
            context=_FakeContext(),
            channel=channel,
            slash=slash,
            vault=vault,
            archive=archive,
            journal=journal,
        )
        state = saga.fire(agent_id="agt_alpha", app_id="app_1", credential_ref="cred_1")
        assert state.phase == FirePhase.COMPLETED
        assert state.is_terminal
        assert "agt_alpha" in channel.stopped
        assert "app_1" in slash.cleaned
        assert "cred_1" in vault.destroyed
        assert "agt_alpha" in archive.archived
        assert len(journal.events) == 5

    def test_channel_failure_stops_saga(self) -> None:
        saga = FireSaga(
            context=_FakeContext(),
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
            context=_FakeContext(),
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
            context=_FakeContext(),
            channel=_FakeChannel(),
            slash=_FakeSlash(),
            vault=_FakeVault(fail=True),
            archive=_FakeArchive(),
            journal=_FakeJournal(),
        )
        state = saga.fire(agent_id="agt_alpha", app_id="app_1", credential_ref="cred_1")
        assert state.phase == FirePhase.FAILED
        assert "vault" in state.error

    def test_context_failure_prevents_channel_and_vault_cleanup(self) -> None:
        channel = _FakeChannel()
        vault = _FakeVault()
        saga = FireSaga(
            context=_FakeContext(fail=True),
            channel=channel,
            slash=_FakeSlash(),
            vault=vault,
            archive=_FakeArchive(),
            journal=_FakeJournal(),
        )

        state = saga.fire(
            agent_id="agt_alpha",
            app_id="app_1",
            credential_ref="cred_1",
        )

        assert state.phase == FirePhase.FAILED
        assert "context" in state.error
        assert channel.stopped == []
        assert vault.destroyed == []


class TestDepartmentBootstrap:
    def test_dormant_mode_when_limit_zero(self) -> None:
        bootstrap = AgentDepartmentBootstrap(settings=None, visible_employee_limit=0)
        result = bootstrap.start()
        assert result.dormant is True
        assert result.healthy is False
        assert bootstrap.is_ready is False

    def test_nonzero_limit_without_component_probes_fails_closed(self) -> None:
        bootstrap = AgentDepartmentBootstrap(settings=object(), visible_employee_limit=1)
        result = bootstrap.start()
        assert result.healthy is False
        assert bootstrap.is_ready is False
        assert "missing_component_probes" in result.errors

    def test_all_component_probes_are_required_for_readiness(self) -> None:
        names = {
            "data_plane",
            "context",
            "provisioning",
            "channel",
            "router",
            "response",
        }
        probes = {name: (lambda: True) for name in names}
        probes["channel"] = lambda: False
        bootstrap = AgentDepartmentBootstrap(
            settings=object(),
            visible_employee_limit=1,
            component_probes=probes,
        )

        result = bootstrap.start()

        assert result.channel_ready is False
        assert result.healthy is False
        assert bootstrap.is_ready is False

    def test_all_component_probes_can_report_ready(self) -> None:
        probes = {
            name: (lambda: True)
            for name in (
                "data_plane",
                "context",
                "provisioning",
                "channel",
                "router",
                "response",
            )
        }
        bootstrap = AgentDepartmentBootstrap(
            settings=object(),
            visible_employee_limit=1,
            component_probes=probes,
        )
        result = bootstrap.start()
        assert result.healthy is True
        assert bootstrap.is_ready is True

    def test_shutdown(self) -> None:
        bootstrap = AgentDepartmentBootstrap(settings=None, visible_employee_limit=0)
        bootstrap.start()
        bootstrap.shutdown()
        assert not bootstrap.is_ready
