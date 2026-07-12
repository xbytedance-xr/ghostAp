"""Employee Response Channel: durable outbox ensuring employee-owned delivery."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    """Employee response delivery failed."""


class DeliveryState(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass
class OutboxEntry:
    """One pending employee response."""

    entry_id: str
    agent_id: str
    chat_id: str
    thread_root_id: str
    content_type: str
    content: str
    card_json: dict[str, Any] | None = None
    state: DeliveryState = DeliveryState.PENDING
    attempts: int = 0
    created_at: float = 0.0
    delivered_at: float = 0.0
    error: str = ""


class EmployeeDeliveryPort(Protocol):
    """Port for sending messages as the employee bot (not main bot)."""

    def send_message(
        self,
        *,
        agent_id: str,
        chat_id: str,
        thread_root_id: str,
        content_type: str,
        content: str,
    ) -> str: ...

    def send_card(
        self,
        *,
        agent_id: str,
        chat_id: str,
        thread_root_id: str,
        card_json: dict[str, Any],
    ) -> str: ...


class EmployeeResponseChannel:
    """In-memory outbox ensuring employees respond with their own bot identity.

    Contract: main bot delivery count for employee responses must be 0.
    Note: this outbox is NOT durable across restarts. Journal-backed
    persistence is deferred until Channel SDK integration is live.
    """

    def __init__(
        self,
        *,
        delivery: EmployeeDeliveryPort,
        max_retry: int = 3,
    ) -> None:
        self._delivery = delivery
        self._max_retry = max_retry
        self._outbox: dict[str, OutboxEntry] = {}
        self._lock = threading.Lock()

    def enqueue_text(
        self,
        *,
        agent_id: str,
        chat_id: str,
        thread_root_id: str = "",
        text: str,
    ) -> OutboxEntry:
        """Enqueue a text response for delivery via employee bot."""
        entry = OutboxEntry(
            entry_id=f"resp_{uuid.uuid4().hex[:16]}",
            agent_id=agent_id,
            chat_id=chat_id,
            thread_root_id=thread_root_id,
            content_type="text",
            content=text,
            created_at=time.time(),
        )
        with self._lock:
            self._outbox[entry.entry_id] = entry
        self._try_deliver(entry)
        return entry

    def enqueue_card(
        self,
        *,
        agent_id: str,
        chat_id: str,
        thread_root_id: str = "",
        card_json: dict[str, Any],
    ) -> OutboxEntry:
        """Enqueue a card response for delivery via employee bot."""
        entry = OutboxEntry(
            entry_id=f"resp_{uuid.uuid4().hex[:16]}",
            agent_id=agent_id,
            chat_id=chat_id,
            thread_root_id=thread_root_id,
            content_type="interactive",
            content="",
            card_json=card_json,
            created_at=time.time(),
        )
        with self._lock:
            self._outbox[entry.entry_id] = entry
        self._try_deliver(entry)
        return entry

    def retry_pending(self) -> int:
        """Retry all pending entries. Returns count delivered."""
        delivered = 0
        with self._lock:
            pending = [e for e in self._outbox.values() if e.state == DeliveryState.PENDING]
        for entry in pending:
            if self._try_deliver(entry):
                delivered += 1
        return delivered

    def get_entry(self, entry_id: str) -> OutboxEntry | None:
        return self._outbox.get(entry_id)

    def pending_count(self, agent_id: str | None = None) -> int:
        with self._lock:
            entries = self._outbox.values()
            if agent_id:
                entries = [e for e in entries if e.agent_id == agent_id]
            return sum(1 for e in entries if e.state == DeliveryState.PENDING)

    def _try_deliver(self, entry: OutboxEntry) -> bool:
        entry.attempts += 1
        try:
            if entry.content_type == "interactive" and entry.card_json:
                self._delivery.send_card(
                    agent_id=entry.agent_id,
                    chat_id=entry.chat_id,
                    thread_root_id=entry.thread_root_id,
                    card_json=entry.card_json,
                )
            else:
                self._delivery.send_message(
                    agent_id=entry.agent_id,
                    chat_id=entry.chat_id,
                    thread_root_id=entry.thread_root_id,
                    content_type=entry.content_type,
                    content=entry.content,
                )
            entry.state = DeliveryState.DELIVERED
            entry.delivered_at = time.time()
            return True
        except Exception as exc:
            if entry.attempts >= self._max_retry:
                entry.state = DeliveryState.FAILED
                entry.error = str(exc)[:500]
            return False
