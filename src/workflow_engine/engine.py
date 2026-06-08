"""WorkflowEngine — orchestrates multi-step AI workflows via Node.js runtime."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..engine_base import BaseEngine, EngineRunState
from .bridge import RuntimeBridge
from .constants import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_MAX_CONCURRENT,
    MAX_TOTAL_AGENTS,
    NODE_MIN_VERSION,
    RESERVE_PER_AGENT_TOKENS,
    STATE_FILENAME,
)
from .errors import _strip_internal_details
from .executor import AgentExecutor
from .history import WorkflowHistory
from .journal import WorkflowJournal
from .models import (
    AgentCallParams,
    AgentCallResult,
    BudgetState,
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

        # Counters for safety fuse
        self._agent_call_count: int = 0

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
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
        selected_tools: Optional[list[str]] = None,
        initiator_user_id: Optional[str] = None,
    ) -> WorkflowProject:
        """Execute a workflow script end-to-end.

        Args:
            requirement: The user's original requirement text.
            script_path: Absolute path to the .js workflow script.
            callbacks: Optional event callbacks for progress/completion.
            budget_tokens: Token budget ceiling for this run.
            selected_tools: Optional tool whitelist; agents may only use these tools.

        Returns:
            The final WorkflowProject with status, metrics, and result.

        Raises:
            RuntimeError: If Node.js is unavailable or the bridge fails fatally.
        """
        self._callbacks = callbacks or WorkflowEngineCallbacks()

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
            budget=BudgetState(total=budget_tokens, used=0),
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
            on_token_usage=self._on_token_usage,
            max_workers=max_concurrent,
            budget_total=budget_tokens,
            on_budget_exceeded=self._on_budget_exceeded,
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
                budget_total=budget_tokens,
                on_agent_call=self._handle_agent_call,
                on_phase=self._handle_phase,
                on_log=self._handle_log,
                cancel_event=self._cancel_event,
                allowed_tools=selected_tools,
                initiator_user_id=project.initiator_user_id,
            )
            self._bridge.start()

            # Run the event loop (blocks until done/error/timeout)
            result_text = self._bridge.run()

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
                "[WorkflowEngine:%s] Completed — agents=%d, tokens=%d, duration=%.1fs",
                workflow_id,
                project.metrics.completed_agents,
                project.budget.used,
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
                self._state_manager.on_workflow_failed("cancelled")
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

    def _handle_agent_call(self, params: AgentCallParams) -> AgentCallResult:
        """Handle an agent() call from the JS runtime.

        Flow:
            1. Safety fuse check (MAX_TOTAL_AGENTS)
            2. Budget exhaustion check
            3. Journal cache lookup
            4. Execute via AgentExecutor (creates one-shot session)
            5. Store result in journal
            6. Fire progress callbacks
        """
        with self._lock:
            self._agent_call_count += 1
            count = self._agent_call_count
        label = params.label or f"agent-{count}"

        # Safety fuse
        if count > MAX_TOTAL_AGENTS:
            error_msg = f"Agent call limit exceeded ({MAX_TOTAL_AGENTS})"
            logger.warning("[WorkflowEngine] %s", error_msg)
            return AgentCallResult(error=error_msg, tool=params.tool, model=params.model)

        # Atomic budget reservation + agent state update (eliminates TOCTOU race)
        # Both operations happen under the same lock in try_reserve().
        # The ``try/finally`` below ensures settle() always runs so reserved
        # tokens are released on tool-rejection, cache-hit, exception, and
        # normal execution paths alike.
        reserved = False
        if self._state_manager:
            reserved = self._state_manager.try_reserve(
                RESERVE_PER_AGENT_TOKENS,
                label,
                tool=params.tool,
                phase=params.phase or "default",
            )
            if not reserved:
                error_msg = "Token budget exhausted"
                logger.warning("[WorkflowEngine] %s", error_msg)
                return AgentCallResult(error=error_msg, tool=params.tool, model=params.model)

        # Result is declared outside the try/finally so the finally block can
        # settle with its final token usage (0 for cache hits, real value for
        # real executions).
        result: AgentCallResult | None = None
        cache_key = WorkflowJournal.compute_key(params.prompt, params.tool, params.model)

        try:
            # Tool whitelist enforcement
            project = self._project
            if project and project.selected_tools and params.tool:
                if params.tool not in project.selected_tools:
                    error_msg = (
                        f"Tool '{params.tool}' not in allowed list: {project.selected_tools}"
                    )
                    logger.warning("[WorkflowEngine] %s", error_msg)
                    # Rollback: agent was already added to state in try_reserve
                    if self._state_manager:
                        self._state_manager.on_agent_failed(label, error_msg)
                    return AgentCallResult(
                        error=error_msg, tool=params.tool, model=params.model
                    )

            # Fire agent start callbacks (state already updated atomically above)
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
                        self._state_manager.on_agent_done(label, {
                            "token_usage": 0,
                            "duration_s": 0.0,
                            "cached": True,
                        })
                    self._fire_progress()
                    result = cached_result
                    return cached_result

            # Execute via AgentExecutor
            result = self._executor.execute(params)

            # Store in journal (only on success)
            if result.error is None and self._journal:
                self._journal.store(cache_key, result)

            # Update state
            if self._state_manager:
                if result.error:
                    self._state_manager.on_agent_failed(label, result.error)
                else:
                    self._state_manager.on_agent_done(label, {
                        "token_usage": result.token_usage,
                        "duration_s": result.duration_s,
                        "cached": False,
                    })

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
        finally:
            # Settle the budget reservation: cache hits return 0 tokens, tool
            # rejections and exceptions are settled with 0, real executions
            # with the actual token_usage. settle() only releases reserved
            # headroom; real usage is tracked incrementally by the executor.
            if reserved and self._state_manager:
                actual_tokens = 0
                if result is not None:
                    actual_tokens = int(result.token_usage or 0)
                self._state_manager.settle(label, actual_tokens)

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

    def _on_budget_exceeded(self, consumed: int, total: int) -> None:
        """Budget exhaustion callback from AgentExecutor.

        Logs the overrun, sets the project budget-exceeded flag and fires a final
        progress refresh so the card can prompt the user.  The budget-exceeded
        state is picked up downstream by rendering/UI to give the operator a chance to
        top-up or terminate.
        """
        logger.warning(
            "[WorkflowEngine] Budget exceeded — consumed=%d total=%d",
            consumed, total,
        )
        if self._state_manager:
            self._state_manager.mark_budget_exceeded(consumed)
        self._fire_progress()

    def _on_token_usage(self, tokens: int) -> None:
        """Callback from AgentExecutor when tokens are consumed.

        Thread-safe: called from ThreadPoolExecutor workers concurrently.
        """
        if self._state_manager:
            self._state_manager.add_token_usage(tokens)

    def _fire_progress(self) -> None:
        """Fire progress callback via coalescer (non-blocking, debounced)."""
        if not self._renderer_wf:
            return
        if not self._callbacks or not self._callbacks.on_progress:
            return
        try:
            card_data = self._renderer_wf.render_progress_card()
            if self._progress_coalescer:
                self._progress_coalescer.enqueue(card_data)
            else:
                self._callbacks.on_progress(card_data)
        except Exception:
            logger.debug("on_progress callback failed", exc_info=True)

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
            return self._renderer_wf.render_progress_card()
        return None

    def get_journal_stats(self) -> dict:
        """Return journal cache statistics."""
        if self._journal:
            return self._journal.stats()
        return {"total": 0, "hits": 0, "misses": 0}
