"""Legacy in-memory employee router retained for scaffold-only tests.

Production employee ingress must use ``autonomous.ingress``'s durable Router;
this module accepts caller-supplied identity/tool fields and is not an
authority or recovery boundary.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

logger = logging.getLogger(__name__)


class RouterError(RuntimeError):
    """Routing failure."""


class RouteDecision(str, Enum):
    EXECUTE = "execute"
    QUEUE = "queue"
    REJECT = "reject"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InboundMessage:
    """Normalized message from employee Channel SDK."""

    agent_id: str
    tenant_key: str
    chat_id: str
    thread_root_id: str
    message_id: str
    sender_id: str
    text: str
    command: str = ""
    args: str = ""
    timestamp: float = 0.0


@dataclass
class RouteResult:
    """Outcome of routing one message."""

    decision: RouteDecision
    agent_id: str
    task_id: str = ""
    error: str = ""
    queued_position: int = 0


class EmployeeMembershipPort(Protocol):
    """Check employee team membership."""

    def is_member(self, agent_id: str, chat_id: str) -> bool: ...

    def get_tenant(self, agent_id: str) -> str: ...


class TaskExecutionPort(Protocol):
    """Port wrapping existing _run_acp_session for one employee task."""

    def execute(
        self,
        *,
        agent_id: str,
        tenant_key: str,
        chat_id: str,
        thread_root_id: str,
        message_id: str,
        sender_id: str,
        text: str,
        tool: str,
        model: str,
        effort: str,
    ) -> str: ...


class EmployeeMessageRouter:
    """Deprecated scaffold; never compose this into production ingress."""

    def __init__(
        self,
        *,
        membership: EmployeeMembershipPort,
        execution: TaskExecutionPort,
        max_queue_per_employee: int = 10,
    ) -> None:
        self._membership = membership
        self._execution = execution
        self._max_queue = max_queue_per_employee
        self._queues: dict[str, list[InboundMessage]] = {}
        self._active: dict[str, str] = {}
        self._lock = threading.Lock()

    def route(
        self,
        message: InboundMessage,
        *,
        tool: str,
        model: str,
        effort: str,
    ) -> RouteResult:
        """Route one inbound message with tenant/membership check."""
        if not message.agent_id or not message.tenant_key:
            return RouteResult(decision=RouteDecision.REJECT, agent_id=message.agent_id, error="missing identity")
        expected_tenant = self._membership.get_tenant(message.agent_id)
        if expected_tenant != message.tenant_key:
            return RouteResult(
                decision=RouteDecision.REJECT,
                agent_id=message.agent_id,
                error="cross-tenant rejected",
            )
        if not self._membership.is_member(message.agent_id, message.chat_id):
            return RouteResult(
                decision=RouteDecision.REJECT,
                agent_id=message.agent_id,
                error="not a member of this chat",
            )
        with self._lock:
            if message.agent_id in self._active:
                queue = self._queues.setdefault(message.agent_id, [])
                if len(queue) >= self._max_queue:
                    return RouteResult(
                        decision=RouteDecision.REJECT,
                        agent_id=message.agent_id,
                        error="queue full",
                    )
                queue.append(message)
                return RouteResult(
                    decision=RouteDecision.QUEUE,
                    agent_id=message.agent_id,
                    queued_position=len(queue),
                )
            self._active[message.agent_id] = message.message_id
        try:
            task_id = self._execution.execute(
                agent_id=message.agent_id,
                tenant_key=message.tenant_key,
                chat_id=message.chat_id,
                thread_root_id=message.thread_root_id,
                message_id=message.message_id,
                sender_id=message.sender_id,
                text=message.text,
                tool=tool,
                model=model,
                effort=effort,
            )
            return RouteResult(
                decision=RouteDecision.EXECUTE,
                agent_id=message.agent_id,
                task_id=task_id,
            )
        except Exception as exc:
            return RouteResult(
                decision=RouteDecision.UNKNOWN,
                agent_id=message.agent_id,
                error=str(exc)[:500],
            )
        finally:
            with self._lock:
                self._active.pop(message.agent_id, None)
                next_msg = self._queues.get(message.agent_id, [])
                if next_msg:
                    queued = next_msg.pop(0)
                else:
                    queued = None
            if queued is not None:
                self._dispatch_queued(queued, tool=tool, model=model, effort=effort)

    def drain_queue(self, agent_id: str) -> InboundMessage | None:
        """Pop next queued message for processing."""
        with self._lock:
            queue = self._queues.get(agent_id, [])
            return queue.pop(0) if queue else None

    def _dispatch_queued(
        self, message: InboundMessage, *, tool: str, model: str, effort: str
    ) -> None:
        """Execute a queued message (best-effort, errors logged)."""
        try:
            self.route(message, tool=tool, model=model, effort=effort)
        except Exception as exc:
            logger.warning("queued dispatch failed for %s: %s", message.agent_id, exc)

    def queue_depth(self, agent_id: str) -> int:
        with self._lock:
            return len(self._queues.get(agent_id, []))

    def is_busy(self, agent_id: str) -> bool:
        with self._lock:
            return agent_id in self._active
