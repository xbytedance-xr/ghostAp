"""WorkflowEngine — orchestrates multi-step AI workflows via Node.js runtime."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..engine_base import BaseEngine, EngineRunState
from .bridge import RuntimeBridge
from .constants import (
    DEFAULT_MAX_CONCURRENT,
    MAX_TOTAL_AGENTS,
    NODE_MIN_VERSION,
    PROGRESS_HEARTBEAT_S,
    STATE_FILENAME,
)
from .errors import _strip_internal_details
from .executor import AgentExecutor
from .history import WorkflowHistory
from .journal import WorkflowJournal
from .models import (
    AgentCallParams,
    AgentCallResult,
    AgentStatus,
    WorkflowMetrics,
    WorkflowProject,
    WorkflowStatus,
)
from .progress_coalescer import ProgressCoalescer
from .renderer import WorkflowProgressRenderer
from .state_manager import WorkflowStateManager

logger = logging.getLogger(__name__)


def _node_version_required_text() -> str:
    """Return the user-visible Node.js version-gate message.

    The minimum version is derived from :data:`NODE_MIN_VERSION` so that all
    user-facing strings stay in sync when the requirement is bumped.
    """
    return (
        f"Node.js >= {NODE_MIN_VERSION[0]} is required for workflow mode. "
        f"Please install Node.js and ensure it's in PATH."
    )


def _decode_result_payload(result_text: str) -> Any:
    """Decode JSON result wrappers, including JSON strings that contain JSON."""
    parsed: Any = result_text
    for _ in range(3):
        if not isinstance(parsed, str):
            return parsed
        text = parsed.strip()
        if not text or not text.startswith(("{", "[", '"')):
            return parsed
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return parsed
    return parsed


def _terminal_failure_from_result(result_text: str) -> str | None:
    """Return a user-visible error when a normal JS result encodes failure.

    Only triggers when the *top-level* result is purely an error — not when
    the result contains partial agent errors alongside valid output (which is
    normal for scripts that try-catch individual agent failures).
    """
    parsed = _decode_result_payload(str(result_text or ""))
    if not isinstance(parsed, dict):
        return None

    error = parsed.get("error")
    error_text = str(error or "").strip()
    message_text = str(parsed.get("message") or "").strip()
    status = str(parsed.get("status") or "").strip().lower()

    # A result is only a terminal failure if it looks like a *pure* error
    # payload — i.e., it has no meaningful output alongside the error.
    # Scripts that catch sub-agent errors and include them in a report (e.g.,
    # {"final_report": "...", "error": "agent X failed"}) are NOT failures.
    has_meaningful_output = any(
        k not in ("error", "message", "status", "stage", "fallback")
        and bool(v)
        for k, v in parsed.items()
    )

    if has_meaningful_output:
        return None

    is_failure = (
        bool(parsed.get("fallback"))
        or status in {"error", "failed", "failure"}
        or ("error" in parsed and bool(error_text))
    )
    if not is_failure:
        return None

    reason = error_text or message_text
    if not reason:
        return None
    stage = str(parsed.get("stage") or "").strip()
    if stage:
        return f"{stage}: {reason}"
    return reason


def _terminal_failure_from_project(project: WorkflowProject) -> str | None:
    """Fail closed when the bridge returns while agent calls are unfinished."""
    for phase in project.phases:
        for agent in phase.agents:
            if agent.status in (AgentStatus.RUNNING, AgentStatus.PENDING):
                label = agent.label or "agent"
                status = agent.status.value if hasattr(agent.status, "value") else str(agent.status)
                return f"Workflow finished while agent {label} was still {status}"

    metrics = project.metrics
    if metrics.total_agents > metrics.completed_agents:
        return f"Workflow finished before all agent calls completed ({metrics.completed_agents}/{metrics.total_agents})"
    return None


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


@dataclass
class WorkflowEngineCallbacks:
    """Event callbacks for the Workflow Engine handler layer."""

    on_progress: Optional[Callable[[dict[str, Any]], None]] = None
    on_done: Optional[Callable[[WorkflowProject], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    on_log: Optional[Callable[[str], None]] = None
    on_phase: Optional[Callable[[str], None]] = None
    on_agent_start: Optional[Callable[[str, str], None]] = None  # (label, tool)
    # AC4: on_agent_done is a lightweight meta-info callback; the payload
    # deliberately excludes the agent output/parsed body so that handler-layer
    # subscribers cannot accidentally leak intermediate results into the main
    # agent chat context. Only final results are delivered via on_done.
    on_agent_done: Optional[Callable[[str, dict], None]] = None  # (label, meta_dict)


# ---------------------------------------------------------------------------
# WorkflowEngine
# ---------------------------------------------------------------------------


class WorkflowEngine(BaseEngine):
    """Orchestrates multi-step AI workflows using a Node.js runtime bridge.

    Lifecycle:
        1. Handler calls execute_workflow(requirement, script_path, callbacks)
        2. Engine creates journal, executor, bridge
        3. Bridge spawns Node.js subprocess running the workflow script
        4. Script issues agent() calls via JSON-RPC → bridge dispatches to executor
        5. Executor creates one-shot ACP/CLI sessions per agent call
        6. Results flow back through the bridge → script continues
        7. On done/error, engine updates project state and fires callbacks
    """

    _state_filename: str = STATE_FILENAME
    _gc_label: str = "Workflow"
    _gc_threshold_default: float = 85.0

    # Cache root for workflow state (mirrors project path under ~/.cache/ghostAp)
    _CACHE_ROOT: str = "~/.cache/ghostAp"

    def __init__(
        self,
        chat_id: str,
        root_path: str,
        agent_type: str = "coco",
        engine_name: str = "Coco",
        model_name: Optional[str] = None,
    ):
        super().__init__(chat_id, root_path, agent_type, engine_name, model_name)

        # Workflow-specific state — initialized to IDLE so handler code
        # can set pending state before execute_workflow() is called.
        self._project: Optional[WorkflowProject] = WorkflowProject()
        self._bridge: Optional[RuntimeBridge] = None
        self._journal: Optional[WorkflowJournal] = None
        self._executor: Optional[AgentExecutor] = None
        self._renderer_wf: Optional[WorkflowProgressRenderer] = None
        self._state_manager: Optional[WorkflowStateManager] = None
        self._progress_coalescer: Optional[ProgressCoalescer] = None
        self._cancel_event = threading.Event()
        self._callbacks: Optional[WorkflowEngineCallbacks] = None

        # Heartbeat: periodically re-renders the progress card while a run is
        # active so the live elapsed counters keep advancing even when no
        # agent start/done/phase event fires during a long blocking agent()
        # call. Plain Event (no lock needed — set/clear/wait are atomic).
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None

        # Counters for safety fuse
        self._agent_call_count: int = 0

        # Map JSON-RPC request_id → effective agent label. Used by
        # _handle_agent_aborted to look up agents by request_id instead of
        # raw label string, which avoids mismatches when state_manager
        # disambiguates duplicate labels (e.g. "agent-1" → "agent-1 #2").
        self._request_to_label: dict[Any, str] = {}
        self._request_to_label_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def workflow_project(self) -> Optional[WorkflowProject]:
        with self._lock:
            return self._project

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def _state_dir(self) -> str:
        """Return the cache directory for workflow state (outside project tree).

        Mirrors the project absolute path under ``~/.cache/ghostAp`` so that
        state files do not pollute the project directory or git status.
        Example: project at ``/data00/home/user/work/proj``
        → ``~/.cache/ghostAp/data00/home/user/work/proj/``
        """
        import os
        from pathlib import Path

        cache_root = os.path.abspath(os.path.expanduser(self._CACHE_ROOT))
        abs_project = os.path.abspath(self.root_path)
        _, tail = os.path.splitdrive(abs_project)
        parts = [part for part in Path(tail).parts if part not in (os.sep, "")]
        return os.path.join(cache_root, *parts)

    def save_state(self, filepath: Optional[str] = None) -> str:
        """Persist workflow state to ~/.cache/ghostAp/<project_path>/ instead of project root."""
        import os

        if not filepath:
            state_dir = self._state_dir()
            os.makedirs(state_dir, exist_ok=True)
            filepath = os.path.join(state_dir, self._state_filename)
        return super().save_state(filepath)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cleanup(self):
        """Override to remove orphaned pending script files and release
        thread-pool resources (executor + bridge). Safe to call more than
        once; shutdown is idempotent.
        """
        project = self._project
        if project and project.pending:
            pending_path = project.pending.script_path
            if pending_path:
                import os

                try:
                    os.remove(pending_path)
                except OSError:
                    pass

        # Ensure any lingering heartbeat thread is stopped (best-effort).
        try:
            self._stop_heartbeat()
        except Exception as e:
            logger.debug("Heartbeat stop during cleanup failed: %s", str(e))

        # Release AgentExecutor thread pool (prevents thread leak across runs).
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                logger.debug("WorkflowEngine executor shutdown failed")
            self._executor = None

        # Release bridge thread pools (agent calls + sub-workflow calls).
        if self._bridge is not None:
            try:
                self._bridge.stop()
            except Exception:
                logger.debug("WorkflowEngine bridge stop failed")
            self._bridge = None

        # Clear request_id → label mapping to prevent cross-run leaks
        with self._request_to_label_lock:
            self._request_to_label.clear()

        super().cleanup()

    # ------------------------------------------------------------------
    # Main execution entry point
    # ------------------------------------------------------------------

    def execute_workflow(
        self,
        requirement: str,
        script_path: str,
        callbacks: Optional[WorkflowEngineCallbacks] = None,
        *,
        selected_tools: Optional[list[str]] = None,
        initiator_user_id: Optional[str] = None,
    ) -> WorkflowProject:
        """Execute a workflow script end-to-end.

        Args:
            requirement: The user's original requirement text.
            script_path: Absolute path to the .js workflow script.
            callbacks: Optional event callbacks for progress/completion.
            selected_tools: Optional tool whitelist; agents may only use these tools.

        Returns:
            The final WorkflowProject with status, metrics, and result.

        Raises:
            RuntimeError: If Node.js is unavailable or the bridge fails fatally.
        """
        self._callbacks = callbacks or WorkflowEngineCallbacks()
        # WorkflowEngine instances are intentionally reused by the manager for
        # the same chat/root path. A previous stop() leaves the event set; every
        # new run must start with a clean cancellation boundary. Clear under
        # self._lock to establish happens-before with _on_stop() which also
        # acquires self._lock before setting the event.
        with self._lock:
            self._cancel_event.clear()
            self._agent_call_count = 0

        # Parse meta from the generated script so we can honor meta.maxConcurrent
        # before the bridge / executor thread pools are created.
        script_meta = None
        max_concurrent = DEFAULT_MAX_CONCURRENT
        try:
            if script_path:
                from .templates import parse_template_meta

                script_content = None
                try:
                    with open(script_path, "r", encoding="utf-8") as f:
                        script_content = f.read()
                except OSError:
                    script_content = None
                if script_content:
                    script_meta = parse_template_meta(script_content)
            if script_meta is not None and script_meta.max_concurrent:
                max_concurrent = int(script_meta.max_concurrent)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to parse workflow script meta: %s", repr(exc))
            script_meta = None
            max_concurrent = DEFAULT_MAX_CONCURRENT

        # Initialize project state
        workflow_id = uuid.uuid4().hex[:12]
        project = WorkflowProject(
            workflow_id=workflow_id,
            status=WorkflowStatus.RUNNING,
            requirement=requirement,
            script_path=script_path,
            meta=script_meta,
            metrics=WorkflowMetrics(),
            started_at=time.time(),
            selected_tools=selected_tools,
            initiator_user_id=initiator_user_id,
        )

        with self._lock:
            self._project = project
            self._run_state = EngineRunState.RUNNING

        # Initialize components
        self._journal = WorkflowJournal(self.root_path, workflow_id)
        self._executor = AgentExecutor(
            cwd=self.root_path,
            cancel_event=self._cancel_event,
            max_workers=max_concurrent,
            on_activity=self._handle_agent_activity,
        )
        self._state_manager = WorkflowStateManager(project)
        self._renderer_wf = WorkflowProgressRenderer(project)

        # Initialize progress coalescer (debounced card updates)
        if self._callbacks and self._callbacks.on_progress:
            self._progress_coalescer = ProgressCoalescer(
                on_progress=self._callbacks.on_progress,
            )

        try:
            # Check Node.js availability
            if not RuntimeBridge.check_node_available():
                raise RuntimeError(_node_version_required_text())

            # Create and start the bridge
            self._bridge = RuntimeBridge(
                script_path=script_path,
                cwd=self.root_path,
                max_concurrent=max_concurrent,
                on_agent_call=self._handle_agent_call,
                on_agent_aborted=self._handle_agent_aborted,
                on_phase=self._handle_phase,
                on_log=self._handle_log,
                cancel_event=self._cancel_event,
                allowed_tools=selected_tools,
                initiator_user_id=project.initiator_user_id,
            )
            self._bridge.start()

            # Start the progress heartbeat so the card (and the new elapsed
            # counters) keep refreshing while the bridge blocks on a long
            # agent() call. Stopped in the finally: block below.
            self._start_heartbeat()

            # Run the event loop (blocks until done/error/timeout)
            result_text = self._bridge.run()

            terminal_failure = _terminal_failure_from_result(result_text)
            if terminal_failure is None and self._state_manager is not None:
                terminal_failure = _terminal_failure_from_project(self._state_manager.snapshot())
            if terminal_failure:
                sanitized_error = _strip_internal_details(terminal_failure)
                project.result = result_text
                project.status = WorkflowStatus.FAILED
                project.error = sanitized_error
                project.finished_at = time.time()
                self._state_manager.on_workflow_failed(terminal_failure)

                logger.error("[WorkflowEngine:%s] Failed: %s", workflow_id, terminal_failure)

                self._fire_progress()
                if self._callbacks.on_error:
                    self._callbacks.on_error(sanitized_error)
                return project

            # Success path
            project.result = result_text
            project.status = WorkflowStatus.COMPLETED
            project.finished_at = time.time()
            # AC4: 仅最终汇总结果计入主 context 增量（字符估算（以字符数作为 token 的近似）。
            # 中间 agent 输出不得通过其他路径进入主 context。
            if self._state_manager:
                self._state_manager.add_context_tokens(len(result_text or ""))
            self._state_manager.on_workflow_done(result_text)

            logger.info(
                "[WorkflowEngine:%s] Completed — agents=%d, duration=%.1fs",
                workflow_id,
                project.metrics.completed_agents,
                time.time() - (project.started_at or 0),
            )

            self._fire_progress()
            if self._callbacks.on_done:
                self._callbacks.on_done(project)

        except RuntimeError as e:
            error_msg = str(e)
            sanitized_error = _strip_internal_details(error_msg)
            if self._cancel_event.is_set():
                project.status = WorkflowStatus.CANCELLED
                project.error = "Workflow cancelled"
                project.finished_at = time.time()
                self._state_manager.on_workflow_cancelled("Workflow cancelled")
            else:
                project.status = WorkflowStatus.FAILED
                project.error = sanitized_error
                project.finished_at = time.time()
                self._state_manager.on_workflow_failed(error_msg)

            logger.error("[WorkflowEngine:%s] Failed: %s", workflow_id, error_msg)

            self._fire_progress()
            if self._callbacks.on_error:
                self._callbacks.on_error(sanitized_error)

        except Exception as e:
            error_msg = f"{type(e).__name__}: {e}"
            sanitized_error = _strip_internal_details(error_msg)
            project.status = WorkflowStatus.FAILED
            project.error = sanitized_error
            project.finished_at = time.time()
            if self._state_manager:
                self._state_manager.on_workflow_failed(error_msg)

            logger.exception("[WorkflowEngine:%s] Unexpected error", workflow_id)

            self._fire_progress()
            if self._callbacks and self._callbacks.on_error:
                self._callbacks.on_error(sanitized_error)

        finally:
            # Stop the progress heartbeat before flushing the final card so no
            # stray re-render races the terminal render below.
            self._stop_heartbeat()

            # Flush any pending progress update (stop() forces final flush
            if self._progress_coalescer:
                self._progress_coalescer.stop()

            # Cleanup bridge
            if self._bridge:
                try:
                    self._bridge.stop()
                except Exception:
                    pass
                self._bridge = None

            # Release AgentExecutor thread pool (prevents thread leak across runs)
            if self._executor:
                try:
                    self._executor.shutdown(wait=False)
                except Exception:
                    logger.debug("WorkflowEngine executor shutdown (finally) failed")
                self._executor = None

            # Persist state
            try:
                self.save_state()
            except Exception as save_err:
                logger.debug("Failed to save workflow state: %s", save_err)

            # Record in execution history
            try:
                history = WorkflowHistory(self.root_path)
                history.record(project)
            except Exception as hist_err:
                logger.debug("Failed to record workflow history: %s", hist_err)

            with self._lock:
                self._run_state = EngineRunState.IDLE

        return project

    # ------------------------------------------------------------------
    # BaseEngine hooks
    # ------------------------------------------------------------------

    def _on_stop(self) -> None:
        """Cancel workflow execution when stop() is called.

        Also shuts down the AgentExecutor's shared thread pool to avoid
        lingering threads after a cancelled run.
        """
        self._cancel_event.set()
        # Best-effort: ensure the heartbeat cannot outlive a stop() call.
        self._heartbeat_stop.set()
        if self._bridge:
            try:
                self._bridge.stop()
            except Exception:
                pass
        if self._executor:
            try:
                self._executor.shutdown(wait=False)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Bridge callbacks
    # ------------------------------------------------------------------

    def _handle_agent_call(
        self,
        params: AgentCallParams,
        *,
        cancel_event=None,
        request_id=None,
        deadline_monotonic: float | None = None,
    ) -> AgentCallResult:
        """Handle an agent() call from the JS runtime.

        Flow:
            1. Safety fuse check (MAX_TOTAL_AGENTS)
            2. Resolve missing model from user's tool-model bindings
            3. Journal cache lookup
            4. Execute via AgentExecutor (creates one-shot session)
            5. Store result in journal
            6. Fire progress callbacks
        """
        with self._lock:
            self._agent_call_count += 1
            count = self._agent_call_count
        label = params.label or f"agent-{count}"

        # Resolve missing model from user's orchestrator/review bindings
        if not params.model and params.tool and self._project:
            params.model = self._resolve_model_for_tool(params.tool)

        # Safety fuse
        if count > MAX_TOTAL_AGENTS:
            error_msg = f"Agent call limit exceeded ({MAX_TOTAL_AGENTS})"
            logger.warning("[WorkflowEngine] %s", error_msg)
            return AgentCallResult(error=error_msg, tool=params.tool, model=params.model)

        # Extract a short task summary from the prompt (first meaningful line, max 60 chars)
        task_summary = ""
        if params.prompt:
            for line in params.prompt.strip().splitlines():
                line = line.strip()
                if line and not line.startswith(("Role:", "#", "---", "**Subagent")):
                    task_summary = line[:80]
                    if len(line) > 80:
                        task_summary += "..."
                    break

        # Register agent in state manager
        if self._state_manager:
            label = self._state_manager.on_agent_started(
                label,
                tool=params.tool,
                phase=params.phase or "default",
                task_summary=task_summary,
            )

        # Track request_id → effective label for abort-by-request-id lookup
        if request_id is not None:
            with self._request_to_label_lock:
                self._request_to_label[request_id] = label

        cache_key = WorkflowJournal.compute_key(
            params.prompt,
            params.tool,
            params.model,
            role=params.role,
            output_schema=params.output_schema,
        )

        # Tool whitelist enforcement
        project = self._project
        if project and project.selected_tools and params.tool:
            if params.tool not in project.selected_tools:
                error_msg = f"Tool '{params.tool}' not in allowed list: {project.selected_tools}"
                logger.warning("[WorkflowEngine] %s", error_msg)
                if self._state_manager:
                    self._state_manager.on_agent_failed(label, error_msg)
                return AgentCallResult(error=error_msg, tool=params.tool, model=params.model)

        # Fire agent start callbacks
        if self._callbacks and self._callbacks.on_agent_start:
            self._callbacks.on_agent_start(label, params.tool)
        self._fire_progress()

        # Journal cache lookup
        if self._journal:
            cached = self._journal.get_cached(cache_key)
            if cached is not None:
                logger.debug("[WorkflowEngine] Cache hit for %s", label)
                cached_result = AgentCallResult(
                    output=cached.output,
                    parsed=cached.parsed,
                    token_usage=0,  # No tokens consumed on cache hit
                    duration_s=0.0,
                    cached=True,
                    tool=params.tool,
                    model=params.model,
                )
                if self._state_manager:
                    self._state_manager.on_agent_done(
                        label,
                        {
                            "token_usage": 0,
                            "duration_s": 0.0,
                            "cached": True,
                        },
                    )
                self._fire_progress()
                return cached_result

        # Execute via AgentExecutor (pass per-call cancel event for race/tournament abort)
        result = self._executor.execute(
            params,
            cancel_event=cancel_event,
            deadline_monotonic=deadline_monotonic,
        )

        # Store in journal (only on success)
        if result.error is None and self._journal:
            self._journal.store(cache_key, result)

        # Update state
        if self._state_manager:
            if result.error:
                self._state_manager.on_agent_failed(label, result.error)
            else:
                self._state_manager.on_agent_done(
                    label,
                    {
                        "token_usage": result.token_usage,
                        "duration_s": result.duration_s,
                        "cached": False,
                    },
                )

        # Fire agent done callback — AC4: only meta info, no output body.
        if self._callbacks and self._callbacks.on_agent_done:
            # Hand-rolled payload: deliberately excludes output/parsed
            # so callers cannot leak intermediate results into the main
            # agent context.
            payload = {
                "label": label,
                "tool": params.tool,
                "model": result.model if result else None,
                "token_usage": result.token_usage if result else 0,
                "duration_s": result.duration_s if result else 0.0,
                "cached": bool(result.cached) if result else False,
                "error": result.error if result else None,
            }
            self._callbacks.on_agent_done(label, payload)

        self._fire_progress()
        return result

    def _handle_agent_activity(self, label: str, activity: str) -> None:
        """Update live activity hint for a running agent from ACP events."""
        if self._state_manager:
            self._state_manager.update_agent_activity(label, activity)

    def _handle_agent_aborted(self, label: str, reason: str, *, request_id=None) -> None:
        """Handle an agent_aborted notification from the JS runtime.

        Called when a race() loser (or tournament elimination) agent is
        aborted. Updates the progress card so the agent no longer shows as
        '执行中'. The ACP session is interrupted via the per-call cancel_event
        set by the bridge's _handle_abort_request.

        Uses request_id for authoritative lookup (avoids label mismatch when
        state_manager disambiguates duplicate labels), falling back to raw
        label for backward compatibility.
        """
        effective_label = label
        if request_id is not None:
            with self._request_to_label_lock:
                mapped = self._request_to_label.get(request_id)
            if mapped:
                effective_label = mapped
        logger.info(
            "[WorkflowEngine] Agent aborted: %s (reason=%s, request_id=%s)",
            effective_label,
            reason,
            request_id,
        )
        if self._state_manager:
            self._state_manager.on_agent_aborted(effective_label, reason)
        self._fire_progress(immediate=True)

    def _handle_phase(self, title: str) -> None:
        """Handle a phase() notification from the JS runtime."""
        logger.info("[WorkflowEngine] Phase: %s", title)

        if self._state_manager:
            self._state_manager.on_phase_changed(title)

        if self._callbacks and self._callbacks.on_phase:
            self._callbacks.on_phase(title)

        self._fire_progress()

    def _handle_log(self, message: str) -> None:
        """Handle a log() notification from the JS runtime."""
        logger.debug("[WorkflowEngine] Log: %s", message)

        if self._callbacks and self._callbacks.on_log:
            self._callbacks.on_log(message)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fire_progress(self, immediate: bool = False) -> None:
        """Fire progress callback via coalescer (non-blocking, debounced).

        Args:
            immediate: If True, bypass the coalescer debounce and flush
                immediately. Used for abort events where the user needs to
                see cancelled agents disappear from '执行中' quickly.
        """
        if not self._renderer_wf:
            return
        if not self._callbacks or not self._callbacks.on_progress:
            return
        try:
            if self._state_manager:
                snapshot = self._state_manager.snapshot()
                card_data = self._renderer_wf.render_progress_card(snapshot)
            else:
                card_data = self._renderer_wf.render_progress_card()
            if self._progress_coalescer:
                if immediate:
                    self._progress_coalescer.flush_immediate(card_data)
                else:
                    self._progress_coalescer.enqueue(card_data)
            else:
                self._callbacks.on_progress(card_data)
        except Exception:
            logger.debug("on_progress callback failed", exc_info=True)

    # ------------------------------------------------------------------
    # Progress heartbeat
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        """Start the daemon heartbeat thread that re-renders the progress card.

        Idempotent: only one heartbeat thread runs per workflow. The thread
        exits when ``_heartbeat_stop`` is set (see :meth:`_stop_heartbeat`).
        """
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        # Fresh cancellation boundary for this run (the event may be set from a
        # previous run when the engine instance is reused by the manager).
        self._heartbeat_stop.clear()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name="WorkflowHeartbeat",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        """Signal the heartbeat thread to stop and join it (best-effort)."""
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None:
            try:
                thread.join(timeout=2.0)
            except Exception as e:
                logger.debug("Heartbeat join failed: %s", str(e))
            self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        """Re-render the progress card every ``PROGRESS_HEARTBEAT_S`` seconds.

        Goes through the coalescer (debounced) so it never spams Feishu. Any
        error is swallowed at debug level — a failed heartbeat must never
        terminate the run.
        """
        while not self._heartbeat_stop.wait(PROGRESS_HEARTBEAT_S):
            try:
                self._fire_progress()
            except Exception as e:
                logger.debug("Heartbeat progress fire failed: %s", str(e))

    def _resolve_model_for_tool(self, tool: str) -> str | None:
        """Resolve the model for a tool from user's selection bindings.

        Uses the tool_model_map populated by start_execution() from the
        user's orchestrator/review agent selections.
        """
        project = self._project
        if not project:
            return None
        return project.tool_model_map.get(tool) or None

    # ------------------------------------------------------------------
    # Status / snapshot
    # ------------------------------------------------------------------

    def get_status_text(self) -> str:
        """Return a compact one-line status string."""
        if self._renderer_wf:
            return self._renderer_wf.render_compact_status()
        return "Workflow: idle"

    def get_progress_card(self) -> Optional[dict[str, Any]]:
        """Return current Feishu card JSON for the workflow progress."""
        if self._renderer_wf:
            if self._state_manager:
                return self._renderer_wf.render_progress_card(self._state_manager.snapshot())
            return self._renderer_wf.render_progress_card()
        return None

    def get_journal_stats(self) -> dict:
        """Return journal cache statistics."""
        if self._journal:
            return self._journal.stats()
        return {"total": 0, "hits": 0, "misses": 0}
