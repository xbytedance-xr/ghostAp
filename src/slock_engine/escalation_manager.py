"""EscalationManager — Escalation Protocol logic extracted from SlockEngine.

Manages escalation lifecycle: create, resolve, query, and resume-after-escalation.
The manager does not own the lock — it receives a shared RLock from the engine.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import TYPE_CHECKING, Callable, Optional

from .card_templates import build_escalation_card
from .models import (
    ABORT_OPTIONS,
    RETRY_OPTIONS,
    SKIP_OPTIONS,
    AgentIdentity,
    AgentStatus,
    EscalationLevel,
    EscalationRequest,
    SlockTask,
    TaskStatus,
)

if TYPE_CHECKING:
    import threading

    from .task_router import TaskRouter

logger = logging.getLogger(__name__)


class EscalationManager:
    """Manages the escalation protocol for a SlockEngine instance.

    Lifecycle-bound to the engine. Receives shared state references via constructor.
    """

    _MAX_ESCALATION_RETRIES = 3
    _MAX_RESOLVED_ESCALATIONS = 100

    def __init__(
        self,
        *,
        lock: threading.RLock,
        escalations: list[EscalationRequest],
        retry_counts: dict[str, int],
        channel_getter: Callable[[], Optional[object]],
        chat_id_getter: Callable[[], str],
        task_list_getter: Callable[[], list[SlockTask]],
        dirty_setter: Callable[[bool], None],
        router: TaskRouter,
        transition_agent: Callable[[str, AgentStatus], None],
        flush_if_dirty: Callable[[list[SlockTask]], None],
        execute_task_fn: Optional[Callable[..., Optional[str]]] = None,
        rollback_task_fn: Optional[Callable[[str, str], None]] = None,
        force_complete_task_fn: Optional[Callable[[str], None]] = None,
        get_executor_fn: Optional[Callable[[], object]] = None,
    ) -> None:
        self._lock = lock
        self._escalations = escalations
        self._escalation_retry_counts = retry_counts
        self._channel_getter = channel_getter
        self._chat_id_getter = chat_id_getter
        self._task_list_getter = task_list_getter
        self._dirty_setter = dirty_setter
        self._router = router
        self._transition_agent = transition_agent
        self._flush_if_dirty = flush_if_dirty
        # These are set after construction to break circular dep with TaskBoardManager
        self._execute_task_fn = execute_task_fn
        self._rollback_task_fn = rollback_task_fn
        self._force_complete_task_fn = force_complete_task_fn
        self._get_executor_fn = get_executor_fn

    def set_task_callbacks(
        self,
        execute_task_fn: Callable[..., Optional[str]],
        rollback_task_fn: Callable[[str, str], None],
        force_complete_task_fn: Callable[[str], None],
    ) -> None:
        """Wire up task-related callbacks after both managers are constructed."""
        self._execute_task_fn = execute_task_fn
        self._rollback_task_fn = rollback_task_fn
        self._force_complete_task_fn = force_complete_task_fn

    def escalate(
        self,
        agent: AgentIdentity,
        reason: str,
        *,
        level: EscalationLevel = EscalationLevel.BLOCKED,
        task_id: Optional[str] = None,
        context: str = "",
        options: Optional[list[str]] = None,
        callbacks=None,
    ) -> EscalationRequest:
        """Raise an escalation request — pauses the agent and requests admin decision."""
        escalation = EscalationRequest(
            agent_id=agent.agent_id,
            agent_name=agent.name,
            task_id=task_id,
            level=level,
            reason=reason,
            context=context[:2000],
            options=options or ["重试", "跳过", "中止"],
        )

        with self._lock:
            self._escalations.append(escalation)

        # Pause the agent
        self._transition_agent(agent.agent_id, AgentStatus.IDLE)

        logger.warning(
            "Escalation raised: agent=%s level=%s reason=%s",
            agent.name, level.value, reason[:100],
        )

        if callbacks and callbacks.on_error:
            callbacks.on_error(f"Escalation [{level.value}] from {agent.name}: {reason}")

        return escalation

    def resolve_escalation(
        self,
        escalation_id: str,
        resolution: str,
    ) -> Optional[EscalationRequest]:
        """Resolve a pending escalation with the admin's decision."""
        with self._lock:
            for esc in self._escalations:
                if esc.escalation_id == escalation_id and not esc.resolved:
                    esc.resolved = True
                    esc.resolution = resolution
                    esc.resolved_at = time.time()
                    retry_key = f"esc_retry:{escalation_id}"
                    self._escalation_retry_counts.pop(retry_key, None)
                    logger.info(
                        "Escalation resolved: id=%s resolution=%s",
                        escalation_id, resolution,
                    )
                    self._trim_escalations()
                    return esc
        return None

    def get_escalation(self, escalation_id: str) -> Optional[EscalationRequest]:
        """Get an escalation by ID. Returns a shallow copy."""
        with self._lock:
            for esc in self._escalations:
                if esc.escalation_id == escalation_id:
                    return copy.copy(esc)
        return None

    def get_pending_escalations(self) -> list[EscalationRequest]:
        """Return all unresolved escalations."""
        with self._lock:
            return [e for e in self._escalations if not e.resolved]

    def get_escalation_card(self, escalation: EscalationRequest) -> dict:
        """Build the interactive card for an escalation request."""
        channel = self._channel_getter()
        channel_id = channel.channel_id if channel else self._chat_id_getter()
        return build_escalation_card(escalation, channel_id=channel_id)

    def resume_after_escalation(
        self,
        escalation: EscalationRequest,
        callbacks=None,
    ) -> Optional[str]:
        """Resume agent work after an escalation has been resolved.

        Behaviour depends on the resolution:
          - Retry: re-execute the associated task (up to _MAX_ESCALATION_RETRIES).
          - Skip: release the task back to TODO for reassignment.
          - Abort: mark the task as DONE (abandoned).
        """
        resolution = (escalation.resolution or "").strip()
        task_id = escalation.task_id
        agent_id = escalation.agent_id

        if resolution in RETRY_OPTIONS:
            retry_key = f"esc_retry:{escalation.escalation_id}"
            with self._lock:
                count = self._escalation_retry_counts.get(retry_key, 0)
                if count >= self._MAX_ESCALATION_RETRIES:
                    logger.warning(
                        "Escalation retry limit reached (%d) for %s — auto-aborting",
                        count, escalation.escalation_id,
                    )
                    if task_id and self._force_complete_task_fn:
                        self._force_complete_task_fn(task_id)
                    return None
                self._escalation_retry_counts[retry_key] = count + 1

            if task_id and self._execute_task_fn:
                # Submit retry asynchronously via BoundedExecutor (consistent with
                # handler async execution model). Falls back to synchronous if no
                # executor is available.
                if self._get_executor_fn:
                    try:
                        executor = self._get_executor_fn()
                        executor.submit(self._execute_task_fn, task_id, agent_id, callbacks)
                        logger.info(
                            "Escalation Retry: task %s submitted async for agent %s",
                            task_id, agent_id,
                        )
                        return None  # result delivered asynchronously
                    except Exception as e:
                        logger.warning(
                            "Escalation Retry async submit failed (%s), falling back to sync",
                            repr(e),
                        )
                        return self._execute_task_fn(task_id, agent_id, callbacks)
                else:
                    return self._execute_task_fn(task_id, agent_id, callbacks)
            else:
                logger.info("Escalation retry with no task_id, nothing to re-execute")
                return None

        elif resolution in SKIP_OPTIONS:
            if task_id and self._rollback_task_fn:
                self._rollback_task_fn(task_id, agent_id)
                logger.info("Escalation Skip: task %s released back to TODO", task_id)
            return None

        elif resolution in ABORT_OPTIONS:
            if task_id and self._force_complete_task_fn:
                self._force_complete_task_fn(task_id)
                logger.info("Escalation Abort: task %s marked DONE (abandoned)", task_id)
            return None

        else:
            logger.warning("Unknown escalation resolution '%s', no action taken", resolution)
            return None

    def _trim_escalations(self, max_resolved: int = _MAX_RESOLVED_ESCALATIONS) -> None:
        """Remove oldest resolved escalations when exceeding the cap. Must be called under lock."""
        resolved = [e for e in self._escalations if e.resolved]
        if len(resolved) <= max_resolved:
            return
        resolved.sort(key=lambda e: e.resolved_at or 0)
        to_remove = set(id(e) for e in resolved[: len(resolved) - max_resolved])
        for esc in self._escalations:
            if id(esc) in to_remove:
                retry_key = f"esc_retry:{esc.escalation_id}"
                self._escalation_retry_counts.pop(retry_key, None)
        self._escalations[:] = [e for e in self._escalations if id(e) not in to_remove]
