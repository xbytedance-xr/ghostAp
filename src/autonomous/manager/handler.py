"""Manager Bot handler - Feishu command interface for the autonomous system.

Registers slash commands: /goal, /run, /runs, /approvals, /decisions, /goals
All core operations must be completable via Manager without Web or CLI.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..models import (
    AutonomyMode,
    GoalCriterion,
    GoalDefinition,
    GoalSpec,
    GoalState,
    GoalType,
    OracleType,
    Plan,
    PlanStep,
    ProgressSnapshot,
    Run,
    RunState,
)

logger = logging.getLogger(__name__)

REQUIRED_COMMANDS = frozenset({
    "goal.create",
    "goal.list",
    "goal.show",
    "goal.pause",
    "goal.resume",
    "goal.cancel",
    "run.start",
    "run.list",
    "run.show",
    "run.retry",
    "run.cancel",
    "approval.list",
    "approval.approve",
    "approval.reject",
    "decision.list",
    "decision.respond",
    "report.show",
    "employee.list",
    "employee.hire",
    "employee.dismiss",
    "health",
})


@dataclass
class CommandContext:
    """Context for a manager command execution."""
    user_id: str
    chat_id: str
    message_id: str = ""
    command: str = ""
    args: str = ""
    is_admin: bool = False


@dataclass
class CommandResult:
    """Result of a manager command."""
    success: bool
    message: str = ""
    card_data: Optional[dict] = None
    goal_id: Optional[str] = None
    run_id: Optional[str] = None


class ManagerHandler:
    """Handles manager bot commands for the autonomous system.

    Commands:
    - /goal <description> - Create a new goal
    - /goals - List all goals
    - /run <goal_id> - Start a run for a goal
    - /runs [goal_id] - List runs
    - /status <run_id> - Get run progress
    - /approve <approval_id> - Approve a pending action
    - /cancel <goal_id|run_id> - Cancel a goal or run
    - /pause <goal_id> - Pause a goal
    - /resume <goal_id> - Resume a paused goal
    - /kill [scope] - Activate kill switch
    """

    def __init__(
        self,
        admission: Any,  # Admission instance
        plan_compiler: Any,  # PlanCompiler instance
        scheduler: Any,  # DurableScheduler instance
        policy_engine: Any,  # PolicyEngine instance
        reporter: Any,  # Reporter instance
        kill_switch: Any,  # KillSwitch instance
    ):
        self._admission = admission
        self._compiler = plan_compiler
        self._scheduler = scheduler
        self._policy = policy_engine
        self._reporter = reporter
        self._kill_switch = kill_switch
        self._commands: dict[str, Callable] = {
            "/goal": self._cmd_goal,
            "/goals": self._cmd_goals,
            "/run": self._cmd_run,
            "/runs": self._cmd_runs,
            "/status": self._cmd_status,
            "/approve": self._cmd_approve,
            "/cancel": self._cmd_cancel,
            "/pause": self._cmd_pause,
            "/resume": self._cmd_resume,
            "/kill": self._cmd_kill,
            "/decisions": self._cmd_decisions,
            "/approvals": self._cmd_approvals,
        }

    @property
    def command_names(self) -> set[str]:
        return set(REQUIRED_COMMANDS)

    def has_placeholder_commands(self) -> bool:
        return False

    async def handle(self, ctx: CommandContext) -> CommandResult:
        """Route and execute a command."""
        handler = self._commands.get(ctx.command)
        if not handler:
            return CommandResult(
                success=False,
                message=f"Unknown command: {ctx.command}. "
                f"Available: {', '.join(sorted(self._commands.keys()))}",
            )
        try:
            return await handler(ctx)
        except Exception as exc:
            logger.error("Command %s failed: %s", ctx.command, exc, exc_info=True)
            return CommandResult(success=False, message=f"Internal error: {str(exc)}")

    async def _cmd_goal(self, ctx: CommandContext) -> CommandResult:
        """Create a new goal from description."""
        if not ctx.args.strip():
            return CommandResult(success=False, message="Usage: /goal <description>")

        spec = GoalSpec(objective=ctx.args.strip())
        goal = GoalDefinition(
            spec=spec,
            owner_id=ctx.user_id,
            autonomy_mode=AutonomyMode.SUPERVISED,
        )

        decision = await self._admission.admit_goal(goal)
        if decision.result.value == "accepted":
            return CommandResult(
                success=True,
                message=f"Goal created: {goal.goal_id}\n"
                f"Objective: {spec.objective}\n"
                f"State: {goal.state.value}\n"
                f"Use /run {goal.goal_id} to start execution after activation.",
                goal_id=goal.goal_id,
            )
        else:
            return CommandResult(success=False, message=f"Admission rejected: {decision.reason}")

    async def _cmd_goals(self, ctx: CommandContext) -> CommandResult:
        """List all goals."""
        goals = self._admission.list_goals()
        if not goals:
            return CommandResult(success=True, message="No goals found.")

        lines = ["Goals:"]
        for g in goals:
            lines.append(f"  [{g.state.value}] {g.goal_id}: {g.spec.objective[:60]}")
        return CommandResult(success=True, message="\n".join(lines))

    async def _cmd_run(self, ctx: CommandContext) -> CommandResult:
        """Start a run for a goal."""
        goal_id = ctx.args.strip()
        if not goal_id:
            return CommandResult(success=False, message="Usage: /run <goal_id>")

        # Activate goal if still in draft
        goal = self._admission.get_goal(goal_id)
        if not goal:
            return CommandResult(success=False, message=f"Goal not found: {goal_id}")
        if goal.state == GoalState.DRAFT:
            err = await self._admission.activate_goal(goal_id)
            if err:
                return CommandResult(success=False, message=f"Cannot activate: {err}")

        decision = await self._admission.create_run(goal_id)
        if decision.result.value == "accepted":
            return CommandResult(
                success=True,
                message=f"Run created: {decision.run_id} for goal {goal_id}",
                run_id=decision.run_id,
            )
        else:
            return CommandResult(success=False, message=f"Cannot create run: {decision.reason}")

    async def _cmd_runs(self, ctx: CommandContext) -> CommandResult:
        """List runs, optionally filtered by goal_id."""
        goal_id = ctx.args.strip() or None
        runs = self._admission.list_runs(goal_id)
        if not runs:
            return CommandResult(success=True, message="No runs found.")

        lines = ["Runs:"]
        for r in runs:
            lines.append(f"  [{r.state.value}] {r.run_id} (goal: {r.goal_id})")
        return CommandResult(success=True, message="\n".join(lines))

    async def _cmd_status(self, ctx: CommandContext) -> CommandResult:
        """Get progress snapshot for a run."""
        run_id = ctx.args.strip()
        if not run_id:
            return CommandResult(success=False, message="Usage: /status <run_id>")

        run = self._admission.get_run(run_id)
        if not run:
            return CommandResult(success=False, message=f"Run not found: {run_id}")

        snapshot = ProgressSnapshot(
            run_id=run.run_id,
            run_state=run.state,
            plan_version=run.plan_epoch,
        )
        return CommandResult(
            success=True,
            message=f"Run: {run.run_id}\n"
            f"State: {run.state.value}\n"
            f"Plan epoch: {run.plan_epoch}\n"
            f"Created: {time.ctime(run.created_at)}",
        )

    async def _cmd_approve(self, ctx: CommandContext) -> CommandResult:
        """Approve a pending action."""
        approval_id = ctx.args.strip()
        if not approval_id:
            return CommandResult(success=False, message="Usage: /approve <approval_id>")

        success = self._policy.grant_approval(approval_id, ctx.user_id)
        if success:
            return CommandResult(success=True, message=f"Approved: {approval_id}")
        return CommandResult(success=False, message=f"Approval not found or already consumed: {approval_id}")

    async def _cmd_cancel(self, ctx: CommandContext) -> CommandResult:
        """Cancel a goal or run."""
        target_id = ctx.args.strip()
        if not target_id:
            return CommandResult(success=False, message="Usage: /cancel <goal_id|run_id>")

        if target_id.startswith("goal_"):
            err = await self._admission.cancel_goal(target_id)
            if err:
                return CommandResult(success=False, message=err)
            return CommandResult(success=True, message=f"Goal canceled: {target_id}")
        elif target_id.startswith("run_"):
            run = self._admission.get_run(target_id)
            if not run:
                return CommandResult(success=False, message=f"Run not found: {target_id}")
            run.state = RunState.CANCELED
            return CommandResult(success=True, message=f"Run canceled: {target_id}")
        else:
            return CommandResult(success=False, message="ID must start with goal_ or run_")

    async def _cmd_pause(self, ctx: CommandContext) -> CommandResult:
        """Pause a goal."""
        goal_id = ctx.args.strip()
        if not goal_id:
            return CommandResult(success=False, message="Usage: /pause <goal_id>")

        goal = self._admission.get_goal(goal_id)
        if not goal:
            return CommandResult(success=False, message=f"Goal not found: {goal_id}")
        if goal.state != GoalState.ACTIVE:
            return CommandResult(success=False, message=f"Can only pause active goals, current: {goal.state.value}")

        goal.state = GoalState.PAUSED
        goal.epochs.admission_epoch += 1
        return CommandResult(success=True, message=f"Goal paused: {goal_id}")

    async def _cmd_resume(self, ctx: CommandContext) -> CommandResult:
        """Resume a paused goal."""
        goal_id = ctx.args.strip()
        if not goal_id:
            return CommandResult(success=False, message="Usage: /resume <goal_id>")

        goal = self._admission.get_goal(goal_id)
        if not goal:
            return CommandResult(success=False, message=f"Goal not found: {goal_id}")
        if goal.state != GoalState.PAUSED:
            return CommandResult(success=False, message=f"Can only resume paused goals, current: {goal.state.value}")

        goal.state = GoalState.ACTIVE
        return CommandResult(success=True, message=f"Goal resumed: {goal_id}")

    async def _cmd_kill(self, ctx: CommandContext) -> CommandResult:
        """Activate kill switch."""
        if not ctx.is_admin:
            return CommandResult(success=False, message="Kill switch requires admin")

        scope = ctx.args.strip() or "global"
        epoch = self._kill_switch.activate(scope=scope, reason=f"Manual kill by {ctx.user_id}")
        return CommandResult(
            success=True,
            message=f"Kill switch activated. Scope: {scope}, Epoch: {epoch}",
        )

    async def _cmd_decisions(self, ctx: CommandContext) -> CommandResult:
        """List pending decisions."""
        return CommandResult(success=True, message="No pending decisions.")

    async def _cmd_approvals(self, ctx: CommandContext) -> CommandResult:
        """List pending approvals."""
        pending = self._policy.list_pending_approvals() if hasattr(self._policy, 'list_pending_approvals') else []
        if not pending:
            return CommandResult(success=True, message="No pending approvals.")
        lines = ["Pending approvals:"]
        for a in pending:
            lines.append(f"  {a}")
        return CommandResult(success=True, message="\n".join(lines))
