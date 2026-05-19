"""EscalationManager — Escalation Protocol logic extracted from SlockEngine.

Manages escalation lifecycle: create, resolve, query, and resume-after-escalation.
The manager does not own the lock — it receives a shared RLock from the engine.
"""

from __future__ import annotations

import copy
import logging
import queue
import threading as _threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
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

from ..utils.redact import redact_sensitive

if TYPE_CHECKING:
    import threading

    from .task_router import TaskRouter

logger = logging.getLogger(__name__)

_BOUNDED_IO_MAX_QUEUE = 16


class _BoundedIOExecutor:
    """Single-thread executor with a bounded task queue.

    When the queue is full, the oldest pending task is discarded (with a
    WARNING log) so that the submitter is never blocked.
    """

    def __init__(self, max_queue_size: int = _BOUNDED_IO_MAX_QUEUE) -> None:
        self._max_queue_size = max_queue_size
        self._queue: queue.Queue[tuple[Callable, tuple]] = queue.Queue(
            maxsize=max_queue_size
        )
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="slock-esc-io"
        )
        self._shutdown = False
        self._lock = _threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        # Start the consumer loop
        self._consumer_future = self._executor.submit(self._consume_loop)

    def submit(self, fn: Callable, *args) -> None:
        """Enqueue a task. If full, discard the oldest and log a warning."""
        with self._lock:
            if self._shutdown:
                raise RuntimeError("_BoundedIOExecutor is shut down")
            if self._queue.full():
                try:
                    discarded = self._queue.get_nowait()
                    logger.warning(
                        "BoundedIOExecutor queue full (%d), discarding oldest task: %s",
                        self._max_queue_size,
                        getattr(discarded[0], "__name__", repr(discarded[0])),
                    )
                except queue.Empty:
                    pass  # race — another consumer took it
            self._queue.put_nowait((fn, args))

    def _consume_loop(self) -> None:
        """Drain the queue and execute tasks sequentially."""
        while True:
            try:
                fn, args = self._queue.get(timeout=0.5)
            except queue.Empty:
                with self._lock:
                    if self._shutdown and self._queue.empty():
                        return
                continue
            try:
                fn(*args)
            except Exception as exc:
                logger.error("BoundedIOExecutor task failed: %s", repr(exc))

    def shutdown(self, wait: bool = False) -> None:
        """Signal shutdown. If wait=True, block until the consumer finishes."""
        with self._lock:
            self._shutdown = True
        if wait:
            self._consumer_future.result(timeout=5)
        self._executor.shutdown(wait=wait)


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
        force_complete_task_fn: Optional[Callable[..., None]] = None,
        get_executor_fn: Optional[Callable[[], object]] = None,
        update_card_fn: Optional[Callable[[str, str], bool]] = None,
        send_text_fn: Optional[Callable[[str, str], None]] = None,
        escalation_timeout_s: int = 30 * 60,
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
        self._update_card_fn = update_card_fn
        self._send_text_fn = send_text_fn
        self._escalation_timeout_s = escalation_timeout_s
        self._timeout_timers: dict[str, _threading.Timer] = {}
        self._half_timers: dict[str, _threading.Timer] = {}
        self._io_executor = _BoundedIOExecutor()
        self._timeout_call_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="slock-esc-call"
        )

    def set_task_callbacks(
        self,
        execute_task_fn: Callable[..., Optional[str]],
        rollback_task_fn: Callable[[str, str], None],
        force_complete_task_fn: Callable[..., None],
    ) -> None:
        """Wire up task-related callbacks after both managers are constructed."""
        self._execute_task_fn = execute_task_fn
        self._rollback_task_fn = rollback_task_fn
        self._force_complete_task_fn = force_complete_task_fn

    def set_ui_callbacks(
        self,
        update_card_fn: Callable[[str, str], bool],
        send_text_fn: Callable[[str, str], None],
    ) -> None:
        """Wire up UI notification callbacks for timeout auto-abort."""
        self._update_card_fn = update_card_fn
        self._send_text_fn = send_text_fn

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

        # Trigger on_escalation callback (outside lock to avoid deadlock).
        # Timer starts AFTER callback so that card_message_id is written before
        # the timeout can fire and copy the escalation object.
        try:
            if callbacks and callbacks.on_escalation:
                try:
                    callbacks.on_escalation(escalation)
                except Exception as exc:
                    logger.warning("on_escalation callback failed: %s", repr(exc))
        finally:
            # Ensure timeout timer always starts even if callback raises
            self._start_timeout_timer(escalation)

        return escalation

    def resolve_escalation(
        self,
        escalation_id: str,
        resolution: str,
    ) -> Optional[EscalationRequest]:
        """Resolve a pending escalation with the admin's decision."""
        self._cancel_timeout_timer(escalation_id)
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
        timeout_minutes = self._escalation_timeout_s // 60
        return build_escalation_card(
            escalation, channel_id=channel_id, timeout_minutes=timeout_minutes,
        )

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
                        self._force_complete_task_fn(task_id, reason="重试次数超限")
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
                self._force_complete_task_fn(task_id, reason="超时中止")
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

    # ------------------------------------------------------------------
    # Timeout auto-abort
    # ------------------------------------------------------------------

    def _start_timeout_timer(self, escalation: EscalationRequest) -> None:
        """Start a daemon timer that auto-aborts the escalation after timeout.

        Also starts a half-time reminder timer at T/2.
        """
        # Full timeout timer
        timer = _threading.Timer(
            self._escalation_timeout_s,
            self._timeout_auto_abort,
            args=(escalation.escalation_id,),
        )
        timer.daemon = True
        timer.start()

        # Half-time reminder timer (T/2)
        half_time = self._escalation_timeout_s / 2
        half_timer = _threading.Timer(
            half_time,
            self._half_time_reminder,
            args=(escalation.escalation_id,),
        )
        half_timer.daemon = True
        half_timer.start()

        with self._lock:
            self._timeout_timers[escalation.escalation_id] = timer
            self._half_timers[escalation.escalation_id] = half_timer

    def _half_time_reminder(self, escalation_id: str) -> None:
        """Send a half-time reminder notification when 50% of timeout has elapsed."""
        with self._lock:
            self._half_timers.pop(escalation_id, None)
            # Check if already resolved — skip if so
            esc = None
            for e in self._escalations:
                if e.escalation_id == escalation_id and not e.resolved:
                    esc = e
                    break
            if esc is None:
                return
            agent_name = esc.agent_name
            remaining_min = self._escalation_timeout_s // 120  # half remaining in minutes

        chat_id = self._chat_id_getter()
        if chat_id and self._send_text_fn:
            try:
                text = (
                    f"⏳ 升级请求即将超时\n"
                    f"Agent: {agent_name}\n"
                    f"剩余: 约 {remaining_min} 分钟\n"
                    f"请尽快处理，否则将自动中止。"
                )
                self._send_text_fn(chat_id, text)
            except Exception as e:
                logger.warning("Failed to send half-time reminder: %s", repr(e))

    def _cancel_timeout_timer(self, escalation_id: str) -> None:
        """Cancel the timeout timer and half-time reminder for a resolved escalation."""
        with self._lock:
            timer = self._timeout_timers.pop(escalation_id, None)
            half_timer = self._half_timers.pop(escalation_id, None)
        if timer is not None:
            timer.cancel()
        if half_timer is not None:
            half_timer.cancel()

    def _timeout_auto_abort(self, escalation_id: str) -> None:
        """Auto-resolve an escalation as 'Abort' after timeout expiry.

        Thread-safety: lock is released before calling external callbacks
        to prevent deadlock with resume_after_escalation.

        I/O operations are offloaded to a dedicated single-thread executor
        so that the Timer thread is never blocked by slow Feishu API calls.
        """
        # Phase 1: state mutation under lock, collect data for callbacks
        esc_copy = None

        with self._lock:
            self._timeout_timers.pop(escalation_id, None)
            for esc in self._escalations:
                if esc.escalation_id == escalation_id and not esc.resolved:
                    esc.resolved = True
                    esc.resolution = "中止"
                    esc.resolved_at = time.time()
                    esc_copy = copy.copy(esc)
                    break

        if esc_copy is None:
            return  # already resolved or not found

        # Phase 2: offload all I/O to the dedicated executor (non-blocking)
        logger.warning(
            "Escalation auto-aborted after %ds timeout: id=%s agent=%s",
            self._escalation_timeout_s,
            escalation_id,
            esc_copy.agent_name,
        )

        # Mark state dirty for status panel refresh (cheap, no I/O)
        self._dirty_setter(True)

        self._io_executor.submit(self._do_timeout_io, esc_copy)

    _IO_CALL_TIMEOUT_S = 10  # Max seconds to wait for a single Feishu API call

    def _do_timeout_io(self, esc_copy: EscalationRequest) -> None:
        """Execute timeout I/O side-effects on the dedicated IO thread.

        Runs serially (single-thread executor guarantees order):
          1. Update escalation card to resolved state
          2. Send text notification to chat
          3. Resume agent (abort branch → force_complete_task)

        Each step is isolated with try/except so one failure doesn't block the next.
        update_card_fn and send_text_fn are individually wrapped with a 10s deadline.
        """
        import json

        from .card_templates import build_resolved_escalation_card

        chat_id = self._chat_id_getter()
        timeout_min = self._escalation_timeout_s // 60

        # FS-1: Update card to resolved state
        if esc_copy.card_message_id and self._update_card_fn:
            try:
                resolved_card = build_resolved_escalation_card(
                    esc_copy,
                    resolved_by="系统超时",
                    resolution="中止（超时）",
                    resolved_at=esc_copy.resolved_at,
                    channel_id=chat_id,
                )
                card_json = json.dumps(resolved_card, ensure_ascii=False)
                self._call_with_timeout(
                    self._update_card_fn, esc_copy.card_message_id, card_json,
                    label="update_card",
                )
            except Exception as e:
                logger.error("Failed to update escalation card on timeout: %s", repr(e))

        # FS-2: Send text notification to chat (redacted)
        if chat_id and self._send_text_fn:
            try:
                text = (
                    f"⏰ 升级请求已超时自动中止\n"
                    f"Agent: {esc_copy.agent_name}\n"
                    f"原因: {redact_sensitive(esc_copy.reason[:100])}\n"
                    f"超时: {timeout_min} 分钟无人处理"
                )
                self._call_with_timeout(
                    self._send_text_fn, chat_id, text,
                    label="send_text",
                )
            except Exception as e:
                logger.error("Failed to send timeout text notification: %s", repr(e))

        # FS-3: Resume agent after escalation (Abort branch handles force_complete)
        try:
            self.resume_after_escalation(esc_copy)
        except Exception as e:
            logger.error("Failed to resume agent after timeout abort: %s", repr(e))
            # Fallback: force-complete the task to prevent inconsistent state
            if esc_copy.task_id and self._force_complete_task_fn:
                try:
                    self._force_complete_task_fn(esc_copy.task_id, reason="系统错误:需人工介入")
                except Exception:
                    logger.error("Fallback force_complete also failed for task %s", esc_copy.task_id)
            # Send alert to chat for admin manual intervention
            if chat_id and self._send_text_fn:
                try:
                    alert_text = (
                        f"🚨 系统告警: 升级恢复失败\n"
                        f"Agent: {esc_copy.agent_name}\n"
                        f"任务: {esc_copy.task_id or 'N/A'}\n"
                        f"请管理员手动介入处理。"
                    )
                    self._send_text_fn(chat_id, alert_text)
                except Exception:
                    logger.error("Failed to send system alert after resume failure")

    def _call_with_timeout(
        self, fn: Callable, *args, label: str = "io_call"
    ) -> None:
        """Call *fn(*args)* with a deadline of _IO_CALL_TIMEOUT_S seconds.

        Submits the call to a bounded ThreadPoolExecutor (max_workers=4) and
        waits on the future with a timeout.  If the deadline expires, a
        TimeoutError is raised so the caller's except block can skip gracefully.
        The underlying lark_oapi socket timeout (30s) serves as the hard upper
        bound for abandoned calls.
        """
        future = self._timeout_call_executor.submit(fn, *args)
        try:
            future.result(timeout=self._IO_CALL_TIMEOUT_S)
        except TimeoutError:
            logger.error(
                "Timeout (%ds) calling %s in escalation auto-abort, skipping",
                self._IO_CALL_TIMEOUT_S,
                label,
            )
            raise TimeoutError(f"{label} exceeded {self._IO_CALL_TIMEOUT_S}s deadline")
        except Exception:
            raise

    def shutdown_timers(self) -> None:
        """Cancel all active timeout timers — called during engine cleanup."""
        with self._lock:
            timers = list(self._timeout_timers.values())
            self._timeout_timers.clear()
            half_timers = list(self._half_timers.values())
            self._half_timers.clear()
        for timer in timers:
            timer.cancel()
        for timer in half_timers:
            timer.cancel()
        self._io_executor.shutdown(wait=False)
        self._timeout_call_executor.shutdown(wait=False)
