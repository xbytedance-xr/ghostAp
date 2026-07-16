"""Journal-backed coordinator for visible employee collaboration runs."""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Protocol

from ..journal.frame import JournalEvent
from ..journal.writer import CommitState, JournalWriter

MAX_HANDOFFS = 8
MAX_DEPTH = 4
MAX_FANOUT = 4
_MAX_TASK_CHARS = 4_000
_RESULT_CONTEXT_CHARS = 4_000


class TeamServiceError(RuntimeError):
    """A team run could not safely progress."""


@dataclass(frozen=True, slots=True)
class TeamTarget:
    agent_id: str
    name: str
    role: str = ""


@dataclass(frozen=True, slots=True)
class TeamAttemptResult:
    status: str
    output: str = ""
    history_record_id: str = ""
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class TeamRunState:
    run_id: str
    tenant_key: str
    message_id: str
    chat_id: str
    requester_principal_id: str
    task_digest: str
    status: str = "running"
    result: str = ""


class TeamBackend(Protocol):
    def list_active(self, tenant_key: str, chat_id: str) -> tuple[TeamTarget, ...]: ...

    def submit(
        self,
        *,
        run_id: str,
        step_id: str,
        target: TeamTarget,
        tenant_key: str,
        chat_id: str,
        message_id: str,
        requester_principal_id: str,
        instruction: str,
    ) -> str: ...

    def result(self, acceptance_id: str) -> TeamAttemptResult | None: ...

    def notify(self, message_id: str, chat_id: str, result: str) -> None: ...


class EmployeeTeamService:
    """Coordinate a bounded analyst -> reviewer -> synthesizer team run."""

    def __init__(
        self,
        *,
        writer: JournalWriter,
        backend: TeamBackend,
        attempt_timeout_seconds: float = 600.0,
        poll_seconds: float = 0.1,
    ) -> None:
        self._writer = writer
        self._backend = backend
        self._timeout = float(attempt_timeout_seconds)
        self._poll = float(poll_seconds)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="employee-team")
        self._lock = threading.RLock()
        self._closed = False
        self._stop = threading.Event()

    def start_task(
        self,
        *,
        tenant_key: str,
        message_id: str,
        chat_id: str,
        requester_principal_id: str,
        task: str,
    ) -> TeamRunState:
        values = (tenant_key, message_id, chat_id, requester_principal_id, task)
        if not all(isinstance(value, str) and value.strip() == value and value for value in values):
            raise ValueError("team task coordinates are required")
        if len(task) > _MAX_TASK_CHARS:
            raise ValueError("team task exceeds maximum length")
        run_id = "teamrun_" + hashlib.sha256(
            f"{tenant_key}\0{message_id}".encode()
        ).hexdigest()
        existing = self.get_run(run_id)
        if existing is not None:
            return existing
        state = TeamRunState(
            run_id=run_id,
            tenant_key=tenant_key,
            message_id=message_id,
            chat_id=chat_id,
            requester_principal_id=requester_principal_id,
            task_digest=hashlib.sha256(task.encode()).hexdigest(),
        )
        self._commit(
            JournalEvent(
                event_type="team.run.created",
                aggregate_id=run_id,
                payload={
                    "tenant_key": tenant_key,
                    "message_id": message_id,
                    "chat_id": chat_id,
                    "requester_principal_id": requester_principal_id,
                    "task_digest": state.task_digest,
                    "max_handoffs": MAX_HANDOFFS,
                    "max_depth": MAX_DEPTH,
                    "max_fanout": MAX_FANOUT,
                },
            )
        )
        with self._lock:
            if self._closed:
                raise TeamServiceError("team service is closed")
            self._executor.submit(self._execute, state, task)
        return state

    def get_run(self, run_id: str) -> TeamRunState | None:
        state: TeamRunState | None = None
        for frame in self._writer.replay():
            for event in frame.events:
                if event.aggregate_id != run_id:
                    continue
                if event.event_type == "team.run.created":
                    state = TeamRunState(
                        run_id=run_id,
                        tenant_key=str(event.payload["tenant_key"]),
                        message_id=str(event.payload["message_id"]),
                        chat_id=str(event.payload["chat_id"]),
                        requester_principal_id=str(event.payload["requester_principal_id"]),
                        task_digest=str(event.payload["task_digest"]),
                    )
                elif state is not None and event.event_type == "team.run.completed":
                    state = replace(state, status="completed")
                elif state is not None and event.event_type == "team.run.action_required":
                    state = replace(state, status="action_required")
        return state

    def recover(self) -> int:
        """Terminalize runs whose in-memory instruction was lost on restart."""
        latest: dict[str, TeamRunState] = {}
        for frame in self._writer.replay():
            for event in frame.events:
                if event.event_type == "team.run.created":
                    state = self.get_run(event.aggregate_id)
                    if state is not None:
                        latest[event.aggregate_id] = state
        pending = [state for state in latest.values() if state.status == "running"]
        for state in pending:
            self._action_required(state, "restart_instruction_unavailable")
        return len(pending)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _execute(self, state: TeamRunState, task: str) -> None:
        try:
            targets = self._backend.list_active(state.tenant_key, state.chat_id)
            if not targets:
                self._action_required(state, "no_active_team_employee")
                return
            lead = targets[0]
            reviewer = targets[1] if len(targets) > 1 else lead
            alternate = targets[2] if len(targets) > 2 else None

            analysis = self._run_step(
                state,
                step_id="analysis",
                depth=1,
                target=lead,
                instruction=(
                    "你是团队主执行者。分析并完成下面任务，输出可交接的方案、证据和风险。\n\n"
                    + task
                ),
            )
            if analysis.status != "completed":
                analysis = self._retry_step(state, "analysis", 1, alternate or reviewer, task)
            if analysis.status != "completed":
                self._action_required(state, analysis.error_code or "analysis_failed")
                return

            review_instruction = (
                "你是独立评审员工。审查上一位员工的交付，指出缺陷并给出修订版。\n\n"
                f"原始任务：\n{task}\n\n上一位员工交付：\n"
                f"{analysis.output[:_RESULT_CONTEXT_CHARS]}"
            )
            review = self._run_step(
                state,
                step_id="review",
                depth=2,
                target=reviewer,
                instruction=review_instruction,
            )
            if review.status != "completed" and alternate is not None:
                review = self._retry_step(state, "review", 2, alternate, review_instruction)
            if review.status != "completed":
                self._action_required(state, review.error_code or "review_failed")
                return

            synthesis_instruction = (
                "你是团队负责人。综合执行稿和评审稿，直接输出最终可交付结果；不要描述协作过程。\n\n"
                f"原始任务：\n{task}\n\n执行稿：\n{analysis.output[:_RESULT_CONTEXT_CHARS]}"
                f"\n\n评审稿：\n{review.output[:_RESULT_CONTEXT_CHARS]}"
            )
            synthesis = self._run_step(
                state,
                step_id="synthesis",
                depth=3,
                target=lead,
                instruction=synthesis_instruction,
            )
            if synthesis.status != "completed" and alternate is not None:
                synthesis = self._retry_step(state, "synthesis", 3, alternate, synthesis_instruction)
            if synthesis.status != "completed":
                self._action_required(state, synthesis.error_code or "synthesis_failed")
                return

            self._commit_effect(state.run_id, "notify", "prepared")
            self._commit_effect(state.run_id, "notify", "executing")
            self._backend.notify(state.message_id, state.chat_id, synthesis.output)
            self._commit_effect(state.run_id, "notify", "committed")
            self._commit(
                JournalEvent(
                    event_type="team.run.completed",
                    aggregate_id=state.run_id,
                    payload={
                        "result_digest": hashlib.sha256(synthesis.output.encode()).hexdigest(),
                        "history_record_id": synthesis.history_record_id,
                    },
                )
            )
        except Exception:
            self._action_required(state, "team_coordinator_failed")

    def _retry_step(
        self,
        state: TeamRunState,
        base_step: str,
        depth: int,
        target: TeamTarget,
        instruction: str,
    ) -> TeamAttemptResult:
        return self._run_step(
            state,
            step_id=f"{base_step}-retry",
            depth=min(depth + 1, MAX_DEPTH),
            target=target,
            instruction=instruction,
        )

    def _run_step(
        self,
        state: TeamRunState,
        *,
        step_id: str,
        depth: int,
        target: TeamTarget,
        instruction: str,
    ) -> TeamAttemptResult:
        if depth > MAX_DEPTH:
            return TeamAttemptResult("action_required", error_code="team_depth_exceeded")
        aggregate = f"{state.run_id}:{step_id}"
        digest = hashlib.sha256(instruction.encode()).hexdigest()
        self._commit(
            JournalEvent(
                event_type="team.step.prepared",
                aggregate_id=aggregate,
                payload={
                    "run_id": state.run_id,
                    "step_id": step_id,
                    "agent_id": target.agent_id,
                    "depth": depth,
                    "instruction_digest": digest,
                },
            )
        )
        self._commit_effect(aggregate, "employee_dispatch", "prepared")
        self._commit_effect(aggregate, "employee_dispatch", "executing")
        try:
            acceptance_id = self._backend.submit(
                run_id=state.run_id,
                step_id=step_id,
                target=target,
                tenant_key=state.tenant_key,
                chat_id=state.chat_id,
                message_id=state.message_id,
                requester_principal_id=state.requester_principal_id,
                instruction=instruction,
            )
        except Exception:
            self._commit_effect(aggregate, "employee_dispatch", "action_required")
            return TeamAttemptResult("action_required", error_code="team_dispatch_failed")
        self._commit(
            JournalEvent(
                event_type="team.step.submitted",
                aggregate_id=aggregate,
                payload={"run_id": state.run_id, "step_id": step_id, "acceptance_id": acceptance_id},
            )
        )
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            result = self._backend.result(acceptance_id)
            if result is not None:
                return self._terminalize_step_result(state, step_id, aggregate, result)
            time.sleep(self._poll)
        if self._stop.is_set():
            self._commit_effect(aggregate, "employee_dispatch", "action_required")
            return TeamAttemptResult("action_required", error_code="team_service_stopping")
        result = self._backend.result(acceptance_id)
        if result is not None:
            return self._terminalize_step_result(state, step_id, aggregate, result)
        self._commit_effect(aggregate, "employee_dispatch", "action_required")
        return TeamAttemptResult("timeout", error_code="team_step_timeout")

    def _terminalize_step_result(
        self,
        state: TeamRunState,
        step_id: str,
        aggregate: str,
        result: TeamAttemptResult,
    ) -> TeamAttemptResult:
        self._commit_effect(
            aggregate,
            "employee_dispatch",
            "committed" if result.status == "completed" else "action_required",
        )
        event_type = "team.step.completed" if result.status == "completed" else "team.step.failed"
        self._commit(
            JournalEvent(
                event_type=event_type,
                aggregate_id=aggregate,
                payload={
                    "run_id": state.run_id,
                    "step_id": step_id,
                    "status": result.status,
                    "history_record_id": result.history_record_id or "none",
                    "result_digest": hashlib.sha256(result.output.encode()).hexdigest(),
                    "error_code": result.error_code or "none",
                },
            )
        )
        return result

    def _action_required(self, state: TeamRunState, error_code: str) -> None:
        current = self.get_run(state.run_id)
        if current is not None and current.status != "running":
            return
        effect_states: dict[tuple[str, str], str] = {}
        for frame in self._writer.replay():
            for event in frame.events:
                if not event.event_type.startswith("team.effect."):
                    continue
                if event.aggregate_id != state.run_id and not event.aggregate_id.startswith(
                    state.run_id + ":"
                ):
                    continue
                effect_type = event.payload.get("effect_type")
                if isinstance(effect_type, str) and effect_type:
                    effect_states[(event.aggregate_id, effect_type)] = event.event_type.rsplit(".", 1)[-1]
        for (aggregate_id, effect_type), effect_state in effect_states.items():
            if effect_state in {"prepared", "executing"}:
                self._commit_effect(aggregate_id, effect_type, "action_required")
        failure_notice = (
            "⚠️ 团队任务未能自动收敛，已安全停止并转为人工处理。"
            f"错误码：`{error_code}`"
        )
        notify_aggregate = f"{state.run_id}:failure-notify"
        try:
            self._commit_effect(notify_aggregate, "notify", "prepared")
            self._commit_effect(notify_aggregate, "notify", "executing")
            self._backend.notify(state.message_id, state.chat_id, failure_notice)
            self._commit_effect(notify_aggregate, "notify", "committed")
        except Exception:
            self._commit_effect(notify_aggregate, "notify", "action_required")
        self._commit(
            JournalEvent(
                event_type="team.run.action_required",
                aggregate_id=state.run_id,
                payload={"error_code": error_code},
            )
        )

    def _commit_effect(self, aggregate_id: str, effect_type: str, state: str) -> None:
        self._commit(
            JournalEvent(
                event_type=f"team.effect.{state}",
                aggregate_id=aggregate_id,
                payload={"effect_type": effect_type},
            )
        )

    def _commit(self, event: JournalEvent) -> None:
        with self._writer.transaction_guard():
            last = self._writer.get_last_frame()
            result = self._writer.commit(
                (event,),
                self._writer.get_aggregate_versions((event.aggregate_id,)),
                expected_head_sequence=0 if last is None else last.sequence,
                expected_head_hash="" if last is None else last.frame_hash,
            )
        if result.state is not CommitState.ANCHORED:
            raise TeamServiceError("team event was not anchored")
