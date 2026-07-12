"""Recoverable /fire saga: disconnect, cleanup, archive employee."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class FireSagaError(RuntimeError):
    """Fire saga failure."""


class FirePhase(str, Enum):
    INITIATED = "initiated"
    CHANNEL_DISCONNECTED = "channel_disconnected"
    SLASH_CLEANED = "slash_cleaned"
    VAULT_DESTROYED = "vault_destroyed"
    ARCHIVED = "archived"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class FireState:
    """Mutable progress of one /fire saga."""

    agent_id: str
    phase: FirePhase = FirePhase.INITIATED
    app_id: str = ""
    credential_ref: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: float = 0.0

    @property
    def is_terminal(self) -> bool:
        return self.phase in (FirePhase.COMPLETED, FirePhase.FAILED)


class ChannelDisconnectPort(Protocol):
    def stop(self, agent_id: str) -> Any: ...


class SlashCleanupPort(Protocol):
    def cleanup_all(self, app_id: str) -> Any: ...


class VaultDestroyPort(Protocol):
    def destroy(self, credential_ref: str) -> bool: ...


class ArchivePort(Protocol):
    def archive_employee(self, agent_id: str) -> None: ...


class JournalFirePort(Protocol):
    def record_fire_event(self, *, agent_id: str, phase: str, payload: dict[str, Any]) -> None: ...


class FireSaga:
    """Recoverable /fire saga with ordered cleanup phases."""

    def __init__(
        self,
        *,
        channel: ChannelDisconnectPort,
        slash: SlashCleanupPort,
        vault: VaultDestroyPort,
        archive: ArchivePort,
        journal: JournalFirePort,
    ) -> None:
        self._channel = channel
        self._slash = slash
        self._vault = vault
        self._archive = archive
        self._journal = journal

    def fire(
        self,
        *,
        agent_id: str,
        app_id: str,
        credential_ref: str,
    ) -> FireState:
        """Execute the full /fire saga. Recoverable on crash."""
        state = FireState(agent_id=agent_id, app_id=app_id, credential_ref=credential_ref)
        try:
            self._channel.stop(agent_id)
            state.phase = FirePhase.CHANNEL_DISCONNECTED
            self._journal.record_fire_event(
                agent_id=agent_id, phase="channel_disconnected", payload={}
            )
        except Exception as exc:
            state.error = f"channel disconnect: {exc}"
            state.phase = FirePhase.FAILED
            return state
        try:
            self._slash.cleanup_all(app_id)
            state.phase = FirePhase.SLASH_CLEANED
            self._journal.record_fire_event(
                agent_id=agent_id, phase="slash_cleaned", payload={}
            )
        except Exception as exc:
            state.error = f"slash cleanup: {exc}"
            state.phase = FirePhase.FAILED
            return state
        try:
            self._vault.destroy(credential_ref)
            state.phase = FirePhase.VAULT_DESTROYED
            self._journal.record_fire_event(
                agent_id=agent_id, phase="vault_destroyed", payload={}
            )
        except Exception as exc:
            state.error = f"vault destroy: {exc}"
            state.phase = FirePhase.FAILED
            return state
        try:
            self._archive.archive_employee(agent_id)
            state.phase = FirePhase.ARCHIVED
        except Exception as exc:
            state.error = f"archive: {exc}"
            state.phase = FirePhase.FAILED
            return state
        state.phase = FirePhase.COMPLETED
        state.completed_at = time.time()
        self._journal.record_fire_event(
            agent_id=agent_id, phase="completed", payload={}
        )
        return state
