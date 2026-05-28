"""CollaborationOrchestrator — multi-role task decomposition and execution.

Orchestrates collaboration plans by:
1. Decomposing tasks into multi-step plans using chain templates
2. Auto-starting plans after timeout (configurable, default 30s)
3. Driving event-driven role participation via TaskStatusObserver
4. Managing timeout protection for unresponsive roles
5. Graceful degradation when orchestrator/coordinator is unavailable
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from ..config import get_settings
from .models import (
    AgentIdentity,
    CollaborationPlan,
    CollaborationPlanStatus,
    PlanStep,
    PlanStepStatus,
    SlockTask,
    TaskStatus,
    TaskTimelineEvent,
)
from .observer_queue import TaskStatusNotifier, TaskStatusObserver
from .task_chain_manager import TaskChainManager

logger = logging.getLogger(__name__)


class CollaborationOrchestrator(TaskStatusObserver):
    """Orchestrates multi-role collaboration plans with event-driven execution.

    Implements TaskStatusObserver to react to task completions and automatically
    activate the next role in the chain. Manages plan lifecycle from creation
    through execution with timeout protection.

    Usage:
        orchestrator = CollaborationOrchestrator(
            chain_manager=chain_manager,
            notifier=notifier,
            resolve_agent=lambda role, channel: agent_identity_or_none,
            dispatch_task=lambda task, agent: None,
        )
        notifier.subscribe(orchestrator)
        plan = orchestrator.create_plan(task, channel_id="ch1")
        # Plan auto-starts after slock_auto_plan_timeout seconds
    """

    def __init__(
        self,
        chain_manager: TaskChainManager,
        notifier: TaskStatusNotifier,
        resolve_agent: Callable[[str, str], Optional[AgentIdentity]],
        dispatch_task: Callable[[SlockTask, AgentIdentity], None],
        *,
        register_task: Callable[[SlockTask], None] | None = None,
        add_task_fn: Callable[[str], Optional[SlockTask]] | None = None,
        claim_task_fn: Callable[[str, str], bool] | None = None,
        persist_fn: Callable[[], None] | None = None,
        auto_plan_timeout: int | None = None,
        role_response_timeout: int | None = None,
    ) -> None:
        """
        Args:
            chain_manager: Manages chain templates and progression.
            notifier: Dispatches task status events to observers.
            resolve_agent: Callable(role, channel_id) -> AgentIdentity or None.
                           Finds the best agent for a given role in the channel.
            dispatch_task: Callable(task, agent) -> None.
                           Dispatches a task to an agent for execution.
            register_task: Optional callback to register tasks in the board for persistence.
            add_task_fn: Callable(content) -> SlockTask or None. Standard board task creation.
            claim_task_fn: Callable(task_id, agent_id) -> bool. Standard board task claim.
            persist_fn: Optional callback to persist plans to disk after state changes.
            auto_plan_timeout: Seconds to wait before auto-starting plan.
            role_response_timeout: Seconds to wait for a role to respond.
        """
        settings = get_settings()
        self._chain_manager = chain_manager
        self._notifier = notifier
        self._resolve_agent = resolve_agent
        self._dispatch_task = dispatch_task
        self._register_task = register_task
        self._add_task_fn = add_task_fn
        self._claim_task_fn = claim_task_fn
        self._persist_fn = persist_fn
        self._auto_plan_timeout = auto_plan_timeout or settings.slock_auto_plan_timeout
        self._role_response_timeout = role_response_timeout or settings.slock_role_response_timeout

        self._plans: dict[str, CollaborationPlan] = {}  # plan_id -> plan
        self._task_to_plan: dict[str, str] = {}  # task_id -> plan_id (for reverse lookup)
        self._step_timers: dict[str, threading.Timer] = {}  # step_id -> timeout timer
        self._plan_timers: dict[str, threading.Timer] = {}  # plan_id -> auto-start timer
        self._retry_counts: dict[str, int] = {}  # "{plan_id}:{step_id}" -> retry count
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._channel_map: dict[str, str] = {}  # plan_id -> channel_id
        self._progress_tracker: Any = None
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="slock-dispatch")

    def set_progress_tracker(self, tracker: Any) -> None:
        """Inject progress tracker after construction (created after orchestrator)."""
        self._progress_tracker = tracker

    def get_all_plans(self) -> list[CollaborationPlan]:
        """Return a snapshot copy of all plans (thread-safe).

        Used by engine for persistence — avoids direct access to _plans.
        """
        with self._lock:
            return list(self._plans.values())

    def get_parallel_plan_tasks(self) -> list[tuple[str, str]]:
        """Find plan tasks that are ready to run in parallel (for idle scan).

        Returns list of (task_id, agent_id) for TODO steps whose dependencies
        are met in EXECUTING plans but haven't been started yet.
        """
        result: list[tuple[str, str]] = []
        with self._lock:
            for plan in self._plans.values():
                if plan.status != CollaborationPlanStatus.EXECUTING:
                    continue
                for step in plan.steps:
                    if step.status != PlanStepStatus.TODO:
                        continue
                    # Check all dependencies are resolved
                    deps_met = all(
                        any(
                            s.step_id == dep_id
                            and s.status in (PlanStepStatus.DONE, PlanStepStatus.SKIPPED, PlanStepStatus.TIMED_OUT)
                            for s in plan.steps
                        )
                        for dep_id in step.depends_on
                    )
                    if deps_met and step.task_id and step.agent_id:
                        result.append((step.task_id, step.agent_id))
        return result

    def _notify_overview(self, plan_id: str) -> None:
        """Trigger a rate-limited overview card update if tracker is wired."""
        if self._progress_tracker is not None:
            self._progress_tracker.schedule_overview_update(plan_id)

    def _persist(self) -> None:
        """Persist plans to disk if callback is configured."""
        if self._persist_fn:
            try:
                self._persist_fn()
            except Exception:
                logger.warning("Failed to persist plans")

    def restore_plans(self, plans: list[CollaborationPlan], channel_id: str) -> None:
        """Restore plans from persistence (called on engine startup)."""
        plans_to_start: list[CollaborationPlan] = []
        with self._lock:
            for plan in plans:
                self._plans[plan.plan_id] = plan
                self._channel_map[plan.plan_id] = channel_id
                # Rebuild task -> plan reverse mapping
                for step in plan.steps:
                    if step.task_id:
                        self._task_to_plan[step.task_id] = plan.plan_id
                # Re-schedule auto-start for pending plans if still within timeout
                if plan.status == CollaborationPlanStatus.PENDING_APPROVAL:
                    if plan.auto_start_at and plan.auto_start_at > time.time():
                        remaining = plan.auto_start_at - time.time()
                        timer = threading.Timer(remaining, self._auto_start_plan, args=(plan.plan_id,))
                        timer.daemon = True
                        timer.start()
                        self._plan_timers[plan.plan_id] = timer
                    else:
                        # Already past auto-start time, approve immediately
                        plan.status = CollaborationPlanStatus.EXECUTING
                # Collect executing plans to start outside lock
                if plan.status == CollaborationPlanStatus.EXECUTING:
                    plans_to_start.append(plan)

        # Start next steps outside lock to avoid deadlock (_start_next_step acquires _lock)
        for plan in plans_to_start:
            self._start_next_step(plan)

    # ------------------------------------------------------------------
    # Plan Lifecycle
    # ------------------------------------------------------------------

    def create_plan(
        self,
        task: SlockTask,
        channel_id: str,
        *,
        chain_template_name: str = "",
    ) -> Optional[CollaborationPlan]:
        """Create a collaboration plan for a task.

        Selects a chain template (explicit or auto-detected from task content),
        builds a plan with steps for each role, and starts the auto-approval timer.

        Returns the plan or None if no suitable chain found.
        """
        # Select chain template
        if chain_template_name:
            template = self._chain_manager.get_template_by_name(chain_template_name)
        else:
            template = self._chain_manager.find_chain_for_task(task.content)

        if template is None:
            logger.warning("No chain template found for task %s", task.task_id)
            return None

        # Build plan steps from template
        steps = []
        for chain_step in template.steps:
            agent = self._resolve_agent(chain_step.role, channel_id)
            step = PlanStep(
                step_id=str(uuid.uuid4()),
                role=chain_step.role,
                agent_id=agent.agent_id if agent else "",
                description=f"{chain_step.role} \u5904\u7406: {task.content[:80]}",
                order=chain_step.order,
                status=PlanStepStatus.TODO,
            )
            steps.append(step)

        # Set up dependencies: DAG-aware — steps depend on ALL steps of the
        # immediately preceding order group (supports parallel role groups).
        # Group steps by order to identify parallel groups.
        from collections import defaultdict
        order_groups: dict[int, list[PlanStep]] = defaultdict(list)
        for step in steps:
            order_groups[step.order].append(step)

        sorted_orders = sorted(order_groups.keys())
        for idx, order in enumerate(sorted_orders):
            if idx == 0:
                # First group has no dependencies
                continue
            prev_order = sorted_orders[idx - 1]
            prev_step_ids = [s.step_id for s in order_groups[prev_order]]
            for step in order_groups[order]:
                step.depends_on = list(prev_step_ids)

        plan = CollaborationPlan(
            plan_id=str(uuid.uuid4()),
            task_id=task.task_id,
            task_content=task.content[:200] if task.content else "",
            steps=steps,
            status=CollaborationPlanStatus.PENDING_APPROVAL,
            chain_template=template.name,
            planner_agent_id="",  # System-generated plan
            auto_start_at=time.time() + self._auto_plan_timeout,
        )

        with self._lock:
            self._plans[plan.plan_id] = plan
            self._task_to_plan[task.task_id] = plan.plan_id
            self._channel_map[plan.plan_id] = channel_id

        # Start auto-approval timer
        self._schedule_auto_start(plan)

        logger.info(
            "Plan created: plan_id=%s task=%s chain=%s steps=%d auto_start_in=%ds",
            plan.plan_id, task.task_id, template.name, len(steps), self._auto_plan_timeout,
        )
        self._persist()
        return plan

    def approve_plan(self, plan_id: str) -> bool:
        """User approves a plan — start execution immediately."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.PENDING_APPROVAL:
                return False
            plan.status = CollaborationPlanStatus.EXECUTING

        self._cancel_plan_timer(plan_id)
        self._start_next_step(plan)
        logger.info("Plan approved and started: %s", plan_id)
        self._persist()
        return True

    def cancel_plan(self, plan_id: str) -> bool:
        """User cancels a plan."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or plan.status in (
                CollaborationPlanStatus.COMPLETED,
                CollaborationPlanStatus.CANCELLED,
            ):
                return False
            plan.status = CollaborationPlanStatus.CANCELLED

        self._cancel_plan_timer(plan_id)
        self._cancel_all_step_timers(plan_id)
        logger.info("Plan cancelled: %s", plan_id)
        self._persist()
        self._notify_overview(plan_id)
        return True

    def pause_plan(self, plan_id: str) -> bool:
        """Pause a plan: prevent new steps from starting (does not interrupt running step)."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.EXECUTING:
                return False
            plan.status = CollaborationPlanStatus.PAUSED

        self._cancel_plan_timer(plan_id)
        logger.info("Plan paused: %s", plan_id)
        self._persist()
        self._notify_overview(plan_id)
        return True

    def resume_plan(self, plan_id: str) -> bool:
        """Resume a paused plan: allow next steps to proceed."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.PAUSED:
                return False
            plan.status = CollaborationPlanStatus.EXECUTING

        self._start_next_step(plan)
        logger.info("Plan resumed: %s", plan_id)
        self._persist()
        return True

    def get_plan(self, plan_id: str) -> Optional[CollaborationPlan]:
        """Get a plan by ID."""
        with self._lock:
            return self._plans.get(plan_id)

    def get_plan_for_task(self, task_id: str) -> Optional[CollaborationPlan]:
        """Get the plan associated with a task."""
        with self._lock:
            plan_id = self._task_to_plan.get(task_id)
            if plan_id:
                return self._plans.get(plan_id)
        return None

    def list_active_plans(self, channel_id: str = "") -> list[CollaborationPlan]:
        """List plans that are currently active (executing, pending, or paused)."""
        with self._lock:
            plans = [
                p for p in self._plans.values()
                if p.status in (CollaborationPlanStatus.EXECUTING, CollaborationPlanStatus.PENDING_APPROVAL, CollaborationPlanStatus.PAUSED)
            ]
            if channel_id:
                plans = [p for p in plans if self._channel_map.get(p.plan_id) == channel_id]
        return plans

    # ------------------------------------------------------------------
    # TaskStatusObserver Implementation (Event-Driven Participation)
    # ------------------------------------------------------------------

    def on_task_status_changed(
        self,
        task_id: str,
        old_status: str,
        new_status: str,
        agent_id: str,
        channel_id: str,
    ) -> None:
        """React to task status changes to drive plan execution."""
        if new_status != TaskStatus.DONE.value:
            return

        with self._lock:
            plan_id = self._task_to_plan.get(task_id)
            if not plan_id:
                return
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.EXECUTING:
                return

            # Find the step that just completed (inside lock for consistency)
            completed_step = None
            for step in plan.steps:
                if step.task_id == task_id:
                    step.status = PlanStepStatus.DONE
                    completed_step = step
                    break

            if completed_step is None:
                return

            # Check if plan is complete
            all_done = all(
                s.status in (PlanStepStatus.DONE, PlanStepStatus.SKIPPED, PlanStepStatus.TIMED_OUT)
                for s in plan.steps
            )
            if all_done:
                plan.status = CollaborationPlanStatus.COMPLETED

        # Cancel timeout timer for this step
        self._cancel_step_timer(completed_step.step_id)

        # Update progress tracker with step completion
        if self._progress_tracker:
            self._progress_tracker.update(
                task_id, entity_type="task", status="done",
                detail=f"{completed_step.role} 完成",
            )

        # Notify observers
        self._notifier.notify_plan_step_completed(
            plan_id=plan.plan_id,
            step_id=completed_step.step_id,
            role=completed_step.role,
            agent_id=agent_id,
        )

        if all_done:
            logger.info("Plan completed: %s", plan.plan_id)
            self._persist()
            self._notify_overview(plan.plan_id)
            return

        # Start next step(s)
        self._start_next_step(plan)
        self._persist()
        self._notify_overview(plan.plan_id)

    def on_plan_step_completed(
        self,
        plan_id: str,
        step_id: str,
        role: str,
        agent_id: str,
    ) -> None:
        """No-op — we are the source of these events, not a consumer."""
        pass

    def on_task_created(
        self,
        task_id: str,
        content: str,
        channel_id: str,
    ) -> None:
        """Auto-plan trigger: when a new task is created, schedule automatic planning.

        After slock_auto_plan_delay seconds, if no plan exists for this task,
        auto-create a collaboration plan.
        """
        # Skip if task already has a plan
        with self._lock:
            if task_id in self._task_to_plan:
                return

        settings = get_settings()
        delay = settings.slock_auto_plan_delay

        timer = threading.Timer(
            delay,
            self._auto_plan_for_task,
            args=(task_id, content, channel_id),
        )
        timer.daemon = True
        timer.name = f"slock-auto-plan-{task_id[:8]}"
        timer.start()
        with self._lock:
            self._plan_timers[f"autoplan-{task_id}"] = timer

    def _auto_plan_for_task(self, task_id: str, content: str, channel_id: str) -> None:
        """Execute auto-plan creation for a task after delay."""
        # Check again if a plan was already created (user may have intervened)
        with self._lock:
            if task_id in self._task_to_plan:
                return
            self._plan_timers.pop(f"autoplan-{task_id}", None)

        # Find if a multi-role chain applies
        template = self._chain_manager.find_chain_for_task(content)
        if template is None or len(template.roles) < 2:
            return

        # Guard: verify at least the first role in the chain can be resolved
        # to an existing agent; otherwise skip to avoid creating an unexecutable plan
        first_role = template.roles[0] if template.roles else None
        if first_role and self._resolve_agent:
            agent = self._resolve_agent(first_role, channel_id)
            if agent is None:
                logger.debug(
                    "Auto-plan skipped for task %s: role '%s' has no available agent",
                    task_id[:8], first_role,
                )
                return

        # Create a synthetic task reference for planning
        task = SlockTask(
            task_id=task_id,
            content=content,
            created_in=channel_id,
        )
        plan = self.create_plan(task, channel_id)
        if plan:
            logger.info("Auto-plan triggered for task %s: plan=%s", task_id, plan.plan_id)

    # ------------------------------------------------------------------
    # Task Rejection / Retry Logic
    # ------------------------------------------------------------------

    def on_task_rejected(self, task_id: str, rejector_role: str = "") -> bool:
        """Handle a task being rejected (e.g., reviewer rejects code quality).

        Rolls the plan back to the preceding step (e.g., coder) for retry.
        After 3 retries on the same step, escalates to the user.

        Args:
            task_id: The task that was rejected.
            rejector_role: The role that rejected (e.g., "reviewer").

        Returns:
            True if rollback was initiated, False if escalation triggered or not found.
        """
        with self._lock:
            plan_id = self._task_to_plan.get(task_id)
            if not plan_id:
                return False
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.EXECUTING:
                return False

            # Find the step associated with this task
            rejected_step = None
            for step in plan.steps:
                if step.task_id == task_id:
                    rejected_step = step
                    break

            if rejected_step is None:
                return False

            # Find predecessor step to retry
            predecessor_step = None
            for step in plan.steps:
                if step.step_id in rejected_step.depends_on:
                    predecessor_step = step
                    break

            if predecessor_step is None:
                logger.warning("No predecessor to retry for task %s", task_id)
                return False

            # Track retry count
            retry_key = f"{plan_id}:{predecessor_step.step_id}"
            self._retry_counts[retry_key] = self._retry_counts.get(retry_key, 0) + 1

            if self._retry_counts[retry_key] > 3:
                # Escalate: too many retries
                logger.warning(
                    "Plan %s: step %s exceeded 3 retries, escalating",
                    plan_id, predecessor_step.step_id,
                )
                plan.status = CollaborationPlanStatus.PAUSED
                self._persist()
                return False

            # Reset predecessor step to TODO for re-execution
            predecessor_step.status = PlanStepStatus.TODO
            predecessor_step.task_id = ""
            # Reset the rejected step too
            rejected_step.status = PlanStepStatus.TODO
            rejected_step.task_id = ""

        logger.info(
            "Plan %s: rolling back to %s (retry %d/3) after rejection by %s",
            plan_id, predecessor_step.role,
            self._retry_counts.get(retry_key, 0), rejector_role,
        )

        # Restart from the predecessor step
        self._start_next_step(plan)
        self._persist()
        return True

    # ------------------------------------------------------------------
    # Timeout Protection
    # ------------------------------------------------------------------

    def _handle_step_timeout(self, plan_id: str, step_id: str) -> None:
        """Handle a step that has timed out waiting for role response.

        When degradation is enabled, attempts to let the previous step's agent
        self-advance to maintain progress even without the timed-out coordinator.
        """
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.EXECUTING:
                return

            timed_out_step = None
            for step in plan.steps:
                if step.step_id == step_id and step.status == PlanStepStatus.IN_PROGRESS:
                    timed_out_step = step
                    break

            if timed_out_step is None:
                return

            # Mark step as timed out (NOT done)
            timed_out_step.status = PlanStepStatus.TIMED_OUT

            # Check if plan is complete
            all_done = all(
                s.status in (PlanStepStatus.DONE, PlanStepStatus.SKIPPED, PlanStepStatus.TIMED_OUT)
                for s in plan.steps
            )
            if all_done:
                plan.status = CollaborationPlanStatus.COMPLETED

        logger.warning(
            "Step timed out: plan=%s step=%s role=%s (timeout=%ds)",
            plan_id, step_id, timed_out_step.role, self._role_response_timeout,
        )

        # Update progress tracker with timeout
        if self._progress_tracker and timed_out_step.task_id:
            self._progress_tracker.update(
                timed_out_step.task_id, entity_type="task", status="timed_out",
                detail=f"{timed_out_step.role} 超时",
            )

        if all_done:
            self._notify_overview(plan_id)
            return

        # Degradation: if the timed-out step had a task_id, try self-advance
        # using the most recently completed agent as the driver
        settings = get_settings()
        if settings.slock_orchestrator_degradation_enabled and timed_out_step.task_id:
            channel_id = self._channel_map.get(plan_id, "")
            # Find the last completed step's agent to drive continuation
            last_completed_agent = ""
            with self._lock:
                for step in sorted(plan.steps, key=lambda s: s.order, reverse=True):
                    if step.status == PlanStepStatus.DONE and step.agent_id:
                        last_completed_agent = step.agent_id
                        break
            if last_completed_agent:
                advanced = self.attempt_self_advance(
                    timed_out_step.task_id, last_completed_agent, channel_id,
                )
                if advanced:
                    logger.info(
                        "Degradation: agent %s self-advanced after timeout of step %s",
                        last_completed_agent, step_id,
                    )
                    return

        # Normal fallback: try next step
        self._start_next_step(plan)
        self._notify_overview(plan_id)

    # ------------------------------------------------------------------
    # Internal Helpers
    # ------------------------------------------------------------------

    def _start_next_step(self, plan: CollaborationPlan) -> None:
        """Find and start ALL ready steps in the plan (DAG-aware parallel execution).

        Steps whose depends_on are all DONE/SKIPPED/TIMED_OUT are started concurrently.
        This enables parallel role groups (e.g. coder+tester running simultaneously).
        """
        if plan.status == CollaborationPlanStatus.PAUSED:
            return
        channel_id = self._channel_map.get(plan.plan_id, "")

        # Collect all steps ready to start under lock to prevent race conditions
        with self._lock:
            ready_steps: list[PlanStep] = []
            for step in sorted(plan.steps, key=lambda s: s.order):
                if step.status != PlanStepStatus.TODO:
                    continue

                # Check dependencies — all must be resolved
                deps_met = all(
                    any(
                        s.step_id == dep_id
                        and s.status in (PlanStepStatus.DONE, PlanStepStatus.SKIPPED, PlanStepStatus.TIMED_OUT)
                        for s in plan.steps
                    )
                    for dep_id in step.depends_on
                )
                if not deps_met:
                    continue

                ready_steps.append(step)

        # Start all ready steps (outside main lock to avoid holding it during dispatch)
        any_skipped = False
        for step in ready_steps:
            # CAS guard: re-check under lock that step is still TODO before proceeding
            with self._lock:
                if step.status != PlanStepStatus.TODO:
                    continue  # Another thread already claimed this step

            # Resolve agent
            agent = None
            if not step.agent_id:
                agent = self._resolve_agent(step.role, channel_id)
                if agent:
                    step.agent_id = agent.agent_id
                else:
                    logger.warning("No agent for role %s in channel %s", step.role, channel_id)
                    step.status = PlanStepStatus.SKIPPED
                    any_skipped = True
                    continue
            else:
                agent = self._resolve_agent(step.role, channel_id)

            # Create task for this step via standard board flow
            task: Optional[SlockTask] = None
            if self._add_task_fn:
                task = self._add_task_fn(step.description)
                if not task:
                    logger.warning("Board rejected task for step %s (limit reached)", step.step_id)
                    step.status = PlanStepStatus.SKIPPED
                    any_skipped = True
                    continue
                step.task_id = task.task_id
                # Claim task for the resolved agent
                if self._claim_task_fn and agent:
                    claimed = self._claim_task_fn(task.task_id, agent.agent_id)
                    if not claimed:
                        logger.warning("Claim failed for task %s agent %s", task.task_id, agent.agent_id)
                        step.status = PlanStepStatus.SKIPPED
                        any_skipped = True
                        continue
                # CAS: mark IN_PROGRESS under lock
                with self._lock:
                    if step.status != PlanStepStatus.TODO:
                        continue  # Race: another thread beat us
                    step.status = PlanStepStatus.IN_PROGRESS
            else:
                # Legacy fallback: direct construction
                task = SlockTask(
                    task_id=str(uuid.uuid4()),
                    content=step.description,
                    status=TaskStatus.IN_PROGRESS,
                    claimed_by=step.agent_id,
                    claimed_at=time.time(),
                    created_in=channel_id,
                )
                step.task_id = task.task_id
                # CAS: mark IN_PROGRESS under lock
                with self._lock:
                    if step.status != PlanStepStatus.TODO:
                        continue
                    step.status = PlanStepStatus.IN_PROGRESS
                # Register task in the board for persistence/tracking
                if self._register_task:
                    try:
                        self._register_task(task)
                    except Exception:
                        logger.warning("Failed to register task %s in board", task.task_id)

            # Record timeline event
            task.timeline.append(TaskTimelineEvent(
                event_type="started",
                agent_id=step.agent_id,
                timestamp=time.time(),
                detail=f"Plan step started: {step.role} — {step.description[:50]}",
            ))

            # Register task -> plan mapping
            with self._lock:
                self._task_to_plan[task.task_id] = plan.plan_id

            # Dispatch to agent asynchronously via bounded executor
            if agent:
                try:
                    self._executor.submit(self._dispatch_task, task, agent)
                except Exception:
                    logger.exception("Failed to submit dispatch for task %s", task.task_id)
                    step.status = PlanStepStatus.SKIPPED
                    any_skipped = True
                    continue

            # Start timeout timer
            self._start_step_timer(plan.plan_id, step.step_id)

        # If any steps were skipped, re-check for newly unblocked steps
        if any_skipped:
            # Check if plan is now complete (all steps resolved)
            all_resolved = all(
                s.status in (PlanStepStatus.DONE, PlanStepStatus.SKIPPED, PlanStepStatus.TIMED_OUT)
                for s in plan.steps
            )
            if all_resolved:
                plan.status = CollaborationPlanStatus.COMPLETED
                return
            self._start_next_step(plan)

        self._notify_overview(plan.plan_id)

    def confirm_plan_delivery(self, plan_id: str) -> None:
        """Confirm the plan card was delivered to the user.

        Resets the auto-start timer so the 30s countdown begins from
        delivery confirmation rather than plan creation time.
        """
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.PENDING_APPROVAL:
                return
            # Cancel existing timer and restart
            self._cancel_plan_timer(plan_id)
            plan.auto_start_at = time.time() + self._auto_plan_timeout
        self._schedule_auto_start(plan)

    def _schedule_auto_start(self, plan: CollaborationPlan) -> None:
        """Schedule auto-start timer for a pending plan."""
        timer = threading.Timer(
            self._auto_plan_timeout,
            self._auto_start_plan,
            args=(plan.plan_id,),
        )
        timer.daemon = True
        timer.name = f"slock-plan-autostart-{plan.plan_id[:8]}"
        timer.start()
        with self._lock:
            self._plan_timers[plan.plan_id] = timer

    def _auto_start_plan(self, plan_id: str) -> None:
        """Auto-start a plan after timeout (user didn't intervene)."""
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.PENDING_APPROVAL:
                return
            plan.status = CollaborationPlanStatus.EXECUTING

        logger.info("Plan auto-started after %ds: %s", self._auto_plan_timeout, plan_id)
        self._start_next_step(plan)

    def _start_step_timer(self, plan_id: str, step_id: str) -> None:
        """Start a timeout timer for a step."""
        timer = threading.Timer(
            self._role_response_timeout,
            self._handle_step_timeout,
            args=(plan_id, step_id),
        )
        timer.daemon = True
        timer.name = f"slock-step-timeout-{step_id[:8]}"
        timer.start()
        with self._lock:
            self._step_timers[step_id] = timer

    def _cancel_step_timer(self, step_id: str) -> None:
        """Cancel a step timeout timer."""
        with self._lock:
            timer = self._step_timers.pop(step_id, None)
        if timer:
            timer.cancel()

    def _cancel_plan_timer(self, plan_id: str) -> None:
        """Cancel the auto-start timer for a plan."""
        with self._lock:
            timer = self._plan_timers.pop(plan_id, None)
        if timer:
            timer.cancel()

    def _cancel_all_step_timers(self, plan_id: str) -> None:
        """Cancel all step timers for a plan."""
        with self._lock:
            plan = self._plans.get(plan_id)
        if not plan:
            return
        for step in plan.steps:
            self._cancel_step_timer(step.step_id)

    def start_cleanup_thread(self) -> None:
        """Start a background daemon thread to clean up expired plans."""
        self._cleanup_stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="slock-plan-ttl-cleanup"
        )
        self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """Periodically remove completed/cancelled plans older than TTL."""
        ttl_seconds = 3600  # 1 hour
        interval = 300  # 5 minutes
        while not self._cleanup_stop.is_set():
            self._cleanup_stop.wait(interval)
            if self._cleanup_stop.is_set():
                break
            self._cleanup_expired_plans(ttl_seconds)

    def _cleanup_expired_plans(self, ttl_seconds: float) -> None:
        """Remove plans that have been completed/cancelled longer than TTL."""
        now = time.time()
        to_remove = []
        with self._lock:
            for plan_id, plan in self._plans.items():
                if plan.status in (CollaborationPlanStatus.COMPLETED, CollaborationPlanStatus.CANCELLED):
                    # Use created_at + generous TTL as proxy (plans don't store completion time)
                    if now - plan.created_at > ttl_seconds:
                        to_remove.append(plan_id)
            for plan_id in to_remove:
                del self._plans[plan_id]
                self._channel_map.pop(plan_id, None)
        if to_remove:
            logger.info("TTL cleanup: removed %d expired plans", len(to_remove))
            self._persist()

    def shutdown(self) -> None:
        """Cancel all timers and clean up."""
        # Stop cleanup thread if running
        if hasattr(self, '_cleanup_stop'):
            self._cleanup_stop.set()
            if hasattr(self, '_cleanup_thread'):
                self._cleanup_thread.join(timeout=2)

        with self._lock:
            plan_ids = list(self._plan_timers.keys())
            step_ids = list(self._step_timers.keys())

        for plan_id in plan_ids:
            self._cancel_plan_timer(plan_id)
        for step_id in step_ids:
            self._cancel_step_timer(step_id)

        logger.info("CollaborationOrchestrator shutdown complete")

    # ------------------------------------------------------------------
    # Orchestrator Degradation Protocol (Slock Insight #3)
    # ------------------------------------------------------------------

    def attempt_self_advance(
        self,
        task_id: str,
        completing_agent_id: str,
        channel_id: str,
    ) -> bool:
        """Allow the completing agent to self-advance the plan when the orchestrator is unavailable.

        Called when a task completes but the normal on_task_status_changed path
        cannot resolve the next step's agent (coordinator role is missing or timed out).
        The completing agent takes over and drives the next step directly.

        Returns True if self-advance was successful, False otherwise.
        """
        settings = get_settings()
        if not settings.slock_orchestrator_degradation_enabled:
            return False

        with self._lock:
            plan_id = self._task_to_plan.get(task_id)
            if not plan_id:
                return False
            plan = self._plans.get(plan_id)
            if not plan or plan.status != CollaborationPlanStatus.EXECUTING:
                return False

            # Find next TODO steps whose dependencies are met
            ready_steps: list[PlanStep] = []
            for step in sorted(plan.steps, key=lambda s: s.order):
                if step.status != PlanStepStatus.TODO:
                    continue
                deps_met = all(
                    any(
                        s.step_id == dep_id
                        and s.status in (PlanStepStatus.DONE, PlanStepStatus.SKIPPED, PlanStepStatus.TIMED_OUT)
                        for s in plan.steps
                    )
                    for dep_id in step.depends_on
                )
                if deps_met:
                    ready_steps.append(step)

        if not ready_steps:
            return False

        advanced_any = False
        for step in ready_steps:
            # Try to resolve the designated agent
            agent = self._resolve_agent(step.role, channel_id) if self._resolve_agent else None

            if agent is None:
                # Degradation: assign to the completing agent itself
                step.agent_id = completing_agent_id
                agent = self._resolve_agent(step.role, channel_id) if self._resolve_agent else None
                if agent is None:
                    # Create a minimal identity for the completing agent to take over
                    logger.warning(
                        "Orchestrator degradation: agent %s self-advancing plan %s step %s (role=%s)",
                        completing_agent_id, plan_id, step.step_id, step.role,
                    )
                    step.status = PlanStepStatus.SKIPPED
                    continue

            # Create task and dispatch
            task: Optional[SlockTask] = None
            if self._add_task_fn:
                task = self._add_task_fn(step.description)
                if not task:
                    step.status = PlanStepStatus.SKIPPED
                    continue
                step.task_id = task.task_id
                if self._claim_task_fn and agent:
                    self._claim_task_fn(task.task_id, agent.agent_id)
            else:
                task = SlockTask(
                    task_id=str(uuid.uuid4()),
                    content=step.description,
                    status=TaskStatus.IN_PROGRESS,
                    claimed_by=agent.agent_id if agent else completing_agent_id,
                    claimed_at=time.time(),
                    created_in=channel_id,
                )
                step.task_id = task.task_id

            with self._lock:
                step.status = PlanStepStatus.IN_PROGRESS
                self._task_to_plan[task.task_id] = plan.plan_id

            task.timeline.append(TaskTimelineEvent(
                event_type="self_advanced",
                agent_id=completing_agent_id,
                timestamp=time.time(),
                detail=f"Degradation: agent self-advanced to step {step.role}",
            ))

            if agent:
                try:
                    self._executor.submit(self._dispatch_task, task, agent)
                    advanced_any = True
                except Exception:
                    logger.exception("Failed to dispatch self-advanced task %s", task.task_id)
                    step.status = PlanStepStatus.SKIPPED

            self._start_step_timer(plan.plan_id, step.step_id)

        if advanced_any:
            self._persist()
            self._notify_overview(plan.plan_id)
        return advanced_any
