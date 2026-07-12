"""Channel Connection Manager: per-employee WebSocket lifecycle."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)


class ChannelError(RuntimeError):
    """Channel connection failure."""


class ChannelState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STOPPED = "stopped"


@dataclass
class ChannelStatus:
    """Status of one employee channel connection."""

    agent_id: str
    app_id: str
    state: ChannelState = ChannelState.DISCONNECTED
    connected_at: float = 0.0
    last_message_at: float = 0.0
    reconnect_count: int = 0
    generation: int = 0
    error: str = ""


class ChannelSDKPort(Protocol):
    """Port for channel-sdk-python operations."""

    def connect(
        self,
        *,
        app_id: str,
        app_secret: str,
        on_message: Callable[[dict[str, Any]], None],
        on_disconnect: Callable[[], None],
    ) -> Any: ...

    def disconnect(self, connection: Any) -> None: ...


class ChannelConnectionManager:
    """Manages per-employee Channel SDK WebSocket connections."""

    def __init__(
        self,
        *,
        channel_sdk: ChannelSDKPort,
        secret_resolver: Callable[[str, str], str],
        max_reconnects: int = 10,
    ) -> None:
        self._sdk = channel_sdk
        self._secret_resolver = secret_resolver
        self._max_reconnects = max_reconnects
        self._channels: dict[str, ChannelStatus] = {}
        self._connections: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._generation = 0

    def start(
        self,
        *,
        agent_id: str,
        app_id: str,
        credential_ref: str,
        on_message: Callable[[str, dict[str, Any]], None],
    ) -> ChannelStatus:
        """Start a channel connection for one employee."""
        with self._lock:
            existing = self._channels.get(agent_id)
            if existing and existing.state in (ChannelState.CONNECTED, ChannelState.CONNECTING):
                return existing
            self._generation += 1
            current_gen = self._generation
            status = ChannelStatus(
                agent_id=agent_id,
                app_id=app_id,
                state=ChannelState.CONNECTING,
                generation=current_gen,
            )
            self._channels[agent_id] = status
        try:
            secret = self._secret_resolver(agent_id, credential_ref)
            connection = self._sdk.connect(
                app_id=app_id,
                app_secret=secret,
                on_message=lambda msg: on_message(agent_id, msg),
                on_disconnect=lambda gen=current_gen: self._on_disconnect(agent_id, gen),
            )
            with self._lock:
                if status.generation != current_gen or status.state == ChannelState.STOPPED:
                    try:
                        self._sdk.disconnect(connection)
                    except Exception:
                        pass
                    return status
                status.state = ChannelState.CONNECTED
                status.connected_at = time.time()
                self._connections[agent_id] = connection
        except Exception as exc:
            with self._lock:
                if status.generation == current_gen:
                    status.state = ChannelState.DISCONNECTED
                    status.error = str(exc)[:500]
        return status

    def stop(self, agent_id: str) -> ChannelStatus | None:
        """Stop and disconnect one employee channel."""
        with self._lock:
            status = self._channels.get(agent_id)
            if status is None:
                return None
            connection = self._connections.pop(agent_id, None)
            status.state = ChannelState.STOPPED
        if connection:
            try:
                self._sdk.disconnect(connection)
            except Exception:
                pass
        return status

    def stop_all(self) -> int:
        """Stop all employee channels. Returns count stopped."""
        agents = list(self._channels.keys())
        count = 0
        for agent_id in agents:
            if self.stop(agent_id):
                count += 1
        return count

    def get_status(self, agent_id: str) -> ChannelStatus | None:
        return self._channels.get(agent_id)

    def list_connected(self) -> list[ChannelStatus]:
        return [
            s for s in self._channels.values()
            if s.state == ChannelState.CONNECTED
        ]

    def _on_disconnect(self, agent_id: str, generation: int) -> None:
        with self._lock:
            status = self._channels.get(agent_id)
            if status is None or status.state == ChannelState.STOPPED:
                return
            if status.generation != generation:
                return
            status.state = ChannelState.DISCONNECTED
            status.reconnect_count += 1
            if status.reconnect_count > self._max_reconnects:
                status.state = ChannelState.STOPPED
                status.error = "max reconnects exceeded"
