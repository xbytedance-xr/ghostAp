"""Recoverable per-group TeamCoordinator actor."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Callable

from ..ingress.models import canonical_utc
from ..journal.blob_store import BlobRef, BlobStore
from ..journal.frame import JournalEvent
from ..journal.writer import CommitState, JournalWriter
from .models import (
    MAX_TEAM_ASSIGNMENTS,
    MAX_TEAM_TURNS,
    CoordinatorAction,
    CoordinatorDecision,
    TeamAssignmentStatus,
    TeamRunPhase,
    TeamRunV2,
)
from .projection import TeamProjectionError, rebuild_team_projection


class TeamCoordinatorError(RuntimeError):
    pass


DecisionProvider = Callable[[TeamRunV2, tuple[object, ...], str], CoordinatorDecision]


class SessionCoordinatorDecisionProvider:
    """Use one reusable configured tool/model session for bounded decisions."""

    def __init__(
        self,
        *,
        tool: str,
        model: str = "",
        profile: str = "",
        effort: str = "",
        cwd_resolver: Callable[[TeamRunV2], str],
        timeout_seconds: float = 120.0,
    ) -> None:
        self._tool = tool
        self._model = model
        self._profile = profile
        self._effort = effort
        self._cwd = cwd_resolver
        self._timeout = timeout_seconds
        self._lock = threading.RLock()
        self._sessions: dict[str, object] = {}

    def __call__(
        self,
        run: TeamRunV2,
        targets: tuple[object, ...],
        task: str,
    ) -> CoordinatorDecision:
        from src.agent_session import create_engine_session

        candidates = [
            {
                "agent_id": item.agent_id,
                "role": getattr(item, "role", ""),
                "capabilities": list(getattr(item, "capabilities", ())),
                "runtime_status": getattr(item, "runtime_status", "ready"),
                "mailbox_load": int(getattr(item, "mailbox_load", 0)),
            }
            for item in targets
        ]
        prompt = (
            "You are the GhostAP team coordinator. Select capable READY employees. "
            "Return JSON only with exactly: action, agent_ids, role, instruction, "
            "depends_on, done_checks, reason_code. action must be assign. Maximum "
            "fanout is 4. Never invent an agent_id.\n\n"
            f"Coordinator profile: {self._profile or 'provider-default'}; "
            f"effort: {self._effort or 'provider-default'}\n"
            f"Task: {task}\nCandidates: {json.dumps(candidates, ensure_ascii=False)}"
        )
        with self._lock:
            session = self._sessions.get(run.coordinator_session_key)
            if session is None:
                session = create_engine_session(
                    agent_type=self._tool,
                    cwd=self._cwd(run),
                    model_name=self._model or None,
                    thread_id=f"team-coordinator:{run.coordinator_session_key[:24]}",
                    auto_approve=True,
                )
                self._sessions[run.coordinator_session_key] = session
            result = session.send_prompt(prompt, timeout=self._timeout)
        raw = getattr(result, "text", "")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {
                "action",
                "agent_ids",
                "role",
                "instruction",
                "depends_on",
                "done_checks",
                "reason_code",
            }:
                raise ValueError
            return CoordinatorDecision(
                action=CoordinatorAction(str(value["action"])),
                agent_ids=tuple(value["agent_ids"]),
                role=str(value["role"]),
                instruction=str(value["instruction"]),
                depends_on=tuple(value["depends_on"]),
                done_checks=dict(value["done_checks"]),
                reason_code=str(value["reason_code"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TeamCoordinatorError("invalid coordinator model decision") from exc

    def close(self) -> None:
        from src.agent_session import close_session_safely

        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            close_session_safely(session)


class TeamCoordinatorActor:
    """Serialize durable team runs and recover them from task/contribution Blobs."""

    def __init__(
        self,
        *,
        writer: JournalWriter,
        blob_store: BlobStore,
        active_key_id: str,
        backend: object,
        coordinator_tool: str = "coco",
        coordinator_model: str = "",
        coordinator_profile: str = "",
        coordinator_effort: str = "",
        attempt_timeout_seconds: float = 600.0,
        poll_seconds: float = 0.1,
        clock: Callable[[], datetime] | None = None,
        decision_provider: DecisionProvider | None = None,
    ) -> None:
        if not coordinator_tool:
            raise ValueError("coordinator tool is required")
        self._writer = writer
        self._blobs = blob_store
        self._key = active_key_id
        self._backend = backend
        self._tool = coordinator_tool
        self._model = coordinator_model
        self._profile = coordinator_profile
        self._effort = coordinator_effort
        self._timeout = float(attempt_timeout_seconds)
        self._poll = float(poll_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._decide = decision_provider
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="team-coordinator")
        self._active: set[str] = set()
        self._closed = False

    def start_task(
        self,
        *,
        tenant_key: str,
        message_id: str,
        chat_id: str,
        requester_principal_id: str,
        task: str,
        project_id: str = "",
        done_criteria: tuple[str, ...] = ("deliverable_non_empty", "review_completed"),
    ) -> TeamRunV2:
        if not task.strip() or len(task) > 4_000:
            raise ValueError("invalid team task")
        run_id = "teamrun2_" + hashlib.sha256(
            f"{tenant_key}\0{chat_id}\0{project_id}\0{message_id}".encode()
        ).hexdigest()
        task_digest = hashlib.sha256(task.encode()).hexdigest()
        safe_goal = f"encrypted-team-task:{task_digest[:16]}"
        with self._lock:
            projection = self.projection()
            existing = projection.runs.get(run_id)
            if existing is not None:
                return existing
            task_ref = self._publish_json(
                {
                    "task": task,
                    "goal": task,
                    "done_criteria": list(done_criteria),
                },
                tenant_key=tenant_key,
                run_id=run_id,
                kind="team_task",
            )
            session_key = hashlib.sha256(
                "\0".join(
                    (tenant_key, chat_id, project_id, self._tool, self._model, self._profile, self._effort)
                ).encode()
            ).hexdigest()
            self._commit(
                JournalEvent(
                    event_type="team.v2.run.created",
                    aggregate_id=run_id,
                    payload={
                        "tenant_key": tenant_key,
                        "chat_id": chat_id,
                        "project_id": project_id,
                        "message_id": message_id,
                        "requester_principal_id": requester_principal_id,
                        "task_ref": task_ref.to_dict(),
                        "goal": safe_goal,
                        "done_criteria": list(done_criteria),
                        "coordinator_session_key": session_key,
                        "coordinator_tool": self._tool,
                        "coordinator_model": self._model,
                        "coordinator_profile": self._profile,
                        "coordinator_effort": self._effort,
                    },
                )
            )
            run = self.projection().runs[run_id]
            self._schedule(run_id)
            return run

    def projection(self):
        return rebuild_team_projection(self._writer.replay())

    def recover(self) -> int:
        projection = self.projection()
        pending = [
            run.run_id
            for run in projection.runs.values()
            if run.phase not in {
                TeamRunPhase.COMPLETED,
                TeamRunPhase.BLOCKED,
                TeamRunPhase.CANCELED,
            }
        ]
        for run_id in pending:
            self._schedule(run_id)
        return len(pending)

    def drain(self) -> None:
        while True:
            with self._lock:
                if not self._active:
                    return
            time.sleep(0.005)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        close = getattr(self._decide, "close", None)
        if callable(close):
            close()

    def claim(self, assignment_id: str, agent_id: str) -> bool:
        with self._lock:
            assignment = self.projection().assignments.get(assignment_id)
            if (
                assignment is None
                or assignment.agent_id != agent_id
                or assignment.status is not TeamAssignmentStatus.CREATED
            ):
                return False
            self._commit(
                JournalEvent(
                    event_type="team.v2.assignment.claimed",
                    aggregate_id=assignment_id,
                    payload={"run_id": assignment.run_id, "agent_id": agent_id},
                )
            )
            return True

    def record_collaboration_event(
        self,
        *,
        tenant_key: str,
        chat_id: str,
        agent_id: str,
        team_run_id: str,
        assignment_id: str,
        causal_event_id: str,
    ) -> bool:
        """Accept only a unique member contribution for one live assignment."""

        with self._lock:
            projection = self.projection()
            run = projection.runs.get(team_run_id)
            assignment = projection.assignments.get(assignment_id)
            if (
                run is None
                or run.tenant_key != tenant_key
                or run.chat_id != chat_id
                or run.phase
                in {
                    TeamRunPhase.COMPLETED,
                    TeamRunPhase.BLOCKED,
                    TeamRunPhase.CANCELED,
                }
                or run.handoff_count >= 8
                or assignment is None
                or assignment.run_id != team_run_id
                or assignment.agent_id != agent_id
                or assignment.status is not TeamAssignmentStatus.COMPLETED
                or causal_event_id in projection.collaboration_events
            ):
                return False
            self._commit(
                JournalEvent(
                    event_type="team.v2.collaboration.observed",
                    aggregate_id=team_run_id,
                    payload={
                        "run_id": team_run_id,
                        "assignment_id": assignment_id,
                        "agent_id": agent_id,
                        "causal_event_id": causal_event_id,
                    },
                )
            )
            return True

    def _schedule(self, run_id: str) -> None:
        with self._lock:
            if self._closed or run_id in self._active:
                return
            self._active.add(run_id)
            self._executor.submit(self._run, run_id)

    def _run(self, run_id: str) -> None:
        try:
            self._drive(run_id)
        except Exception:
            try:
                self._block(run_id, "team_coordinator_failed")
            except Exception:
                pass
        finally:
            with self._lock:
                self._active.discard(run_id)

    def _drive(self, run_id: str) -> None:
        while True:
            projection = self.projection()
            run = projection.runs[run_id]
            if run.phase in {TeamRunPhase.COMPLETED, TeamRunPhase.BLOCKED, TeamRunPhase.CANCELED}:
                return
            task = self._read_json(run.task_ref)["task"]
            targets = tuple(self._backend.list_active(run.tenant_key, run.chat_id))
            if not targets:
                self._block(run_id, "no_capable_team_employee")
                return
            if run.phase is TeamRunPhase.CREATED:
                self._phase(run, TeamRunPhase.PLANNING, turn=1)
                continue
            if run.phase is TeamRunPhase.PLANNING:
                lead = self._select_target(run, targets, str(task), role="execute")
                if lead is None:
                    self._block(run_id, "no_capable_team_employee")
                    return
                decision = CoordinatorDecision(
                    CoordinatorAction.ASSIGN,
                    (lead.agent_id,),
                    role="execute",
                    instruction="完成任务并给出证据、风险和可评审交付。\n\n" + str(task),
                )
                if self._decide is not None:
                    decision = self._decide(run, targets, str(task))
                self._validate_decision(decision, targets)
                for ordinal, agent_id in enumerate(decision.agent_ids, start=1):
                    self._create_assignment(
                        run, decision, ordinal=ordinal, agent_id=agent_id
                    )
                self._phase(run, TeamRunPhase.DISPATCHING, turn=2)
                continue
            if run.phase is TeamRunPhase.DISPATCHING:
                lead_assignments = self._assignments_by_role(run_id, "execute")
                for assignment in lead_assignments:
                    if not self._ensure_assignment(assignment, run):
                        return
                projection = self.projection()
                lead_assignments = tuple(
                    projection.assignments[item.assignment_id]
                    for item in lead_assignments
                )
                failed = next(
                    (
                        item
                        for item in lead_assignments
                        if item.status is not TeamAssignmentStatus.COMPLETED
                    ),
                    None,
                )
                if failed is not None:
                    self._block(run_id, failed.error_code or "team_execution_failed")
                    return
                reviewer = self._select_target(
                    run,
                    targets,
                    str(task),
                    role="review",
                    exclude={item.agent_id for item in lead_assignments},
                ) or self._select_target(run, targets, str(task), role="review")
                if reviewer is None:
                    self._block(run_id, "no_capable_team_reviewer")
                    return
                contribution = "\n\n".join(
                    self._read_text(item.contribution_ref)
                    for item in lead_assignments
                )
                decision = CoordinatorDecision(
                    CoordinatorAction.REVIEW,
                    (reviewer.agent_id,),
                    role="review",
                    instruction=(
                        "独立审查交付；列出阻塞缺陷并给出明确修订建议。\n\n"
                        f"任务：{task}\n\n交付：{contribution[:4_000]}"
                    ),
                    depends_on=tuple(item.assignment_id for item in lead_assignments),
                )
                self._create_assignment(
                    run,
                    decision,
                    ordinal=len(lead_assignments) + 1,
                    agent_id=reviewer.agent_id,
                )
                self._phase(run, TeamRunPhase.REVIEWING, turn=3)
                continue
            if run.phase is TeamRunPhase.REVIEWING:
                review = self._assignment_by_role(run_id, "review")
                if not self._ensure_assignment(review, run):
                    return
                review = self.projection().assignments[review.assignment_id]
                if review.status is not TeamAssignmentStatus.COMPLETED:
                    self._block(run_id, review.error_code or "team_review_failed")
                    return
                lead = self._assignment_by_role(run_id, "execute")
                deliverable = self._read_text(lead.contribution_ref)
                review_text = self._read_text(review.contribution_ref)
                decision = CoordinatorDecision(
                    CoordinatorAction.REVISE,
                    (lead.agent_id,),
                    role="finalize",
                    instruction=(
                        "根据独立评审修订并只输出最终交付。\n\n"
                        f"任务：{task}\n\n初稿：{deliverable[:4_000]}\n\n评审：{review_text[:4_000]}"
                    ),
                    depends_on=(lead.assignment_id, review.assignment_id),
                )
                self._create_assignment(
                    run,
                    decision,
                    ordinal=len(run.assignment_ids) + 1,
                    agent_id=lead.agent_id,
                )
                self._phase(run, TeamRunPhase.REVISING, turn=4, handoff=1)
                continue
            final = self._assignment_by_role(run_id, "finalize")
            if not self._ensure_assignment(final, run):
                return
            final = self.projection().assignments[final.assignment_id]
            if final.status is not TeamAssignmentStatus.COMPLETED:
                self._block(run_id, final.error_code or "team_revision_failed")
                return
            output = self._read_text(final.contribution_ref)
            CoordinatorDecision(
                CoordinatorAction.COMPLETE,
                done_checks={
                    "deliverable_non_empty": bool(output.strip()),
                    "review_completed": self._assignment_by_role(run_id, "review").status
                    is TeamAssignmentStatus.COMPLETED,
                },
            )
            self._finalize(run, final.contribution_ref, output)
            return

    def _create_assignment(
        self,
        run: TeamRunV2,
        decision: CoordinatorDecision,
        *,
        ordinal: int,
        agent_id: str,
    ) -> str:
        projection = self.projection()
        if len(run.assignment_ids) >= MAX_TEAM_ASSIGNMENTS:
            raise TeamCoordinatorError("team assignment bound exceeded")
        assignment_id = f"{run.run_id}:assignment:{ordinal}"
        if assignment_id in projection.assignments:
            return assignment_id
        instruction_ref = self._publish_text(
            decision.instruction,
            tenant_key=run.tenant_key,
            run_id=run.run_id,
            kind="team_instruction",
        )
        self._commit(
            JournalEvent(
                event_type="team.v2.assignment.created",
                aggregate_id=assignment_id,
                payload={
                    "run_id": run.run_id,
                    "agent_id": agent_id,
                    "role": decision.role,
                    "instruction_ref": instruction_ref.to_dict(),
                    "depends_on": list(decision.depends_on),
                },
            )
        )
        return assignment_id

    def _ensure_assignment(self, assignment, run: TeamRunV2) -> bool:
        if assignment.status is TeamAssignmentStatus.CREATED:
            if not self.claim(assignment.assignment_id, assignment.agent_id):
                return False
            assignment = self.projection().assignments[assignment.assignment_id]
        aggregate = assignment.assignment_id
        if assignment.status is TeamAssignmentStatus.CLAIMED:
            instruction = self._read_text(assignment.instruction_ref)
            deadline_at = canonical_utc(
                self._clock() + timedelta(seconds=self._timeout), "team_deadline_at"
            )
            self._effect(aggregate, "employee_dispatch", "prepared")
            self._effect(aggregate, "employee_dispatch", "executing")
            target = next(
                item
                for item in self._backend.list_active(run.tenant_key, run.chat_id)
                if item.agent_id == assignment.agent_id
            )
            try:
                acceptance_id = self._backend.submit(
                    run_id=run.run_id,
                    step_id=assignment.assignment_id.rsplit(":", 1)[-1],
                    target=target,
                    tenant_key=run.tenant_key,
                    chat_id=run.chat_id,
                    message_id=run.message_id,
                    requester_principal_id=run.requester_principal_id,
                    instruction=instruction,
                    deadline_at=deadline_at,
                )
            except Exception:
                self._effect(aggregate, "employee_dispatch", "action_required")
                self._assignment_failed(assignment, "team_dispatch_failed")
                return True
            self._commit(
                JournalEvent(
                    event_type="team.v2.assignment.submitted",
                    aggregate_id=aggregate,
                    payload={"run_id": run.run_id, "acceptance_id": acceptance_id},
                )
            )
            assignment = self.projection().assignments[aggregate]
        if assignment.status is not TeamAssignmentStatus.RUNNING:
            return True
        deadline = self._clock() + timedelta(seconds=self._timeout)
        result = None
        while self._clock() < deadline:
            result = self._backend.result(assignment.acceptance_id)
            if result is not None:
                break
            time.sleep(self._poll)
        if result is None:
            result = self._backend.cancel(
                assignment.acceptance_id,
                run_id=run.run_id,
                step_id=assignment.assignment_id,
            )
        if result.status != "completed" or not result.output.strip():
            self._effect(aggregate, "employee_dispatch", "action_required")
            self._assignment_failed(assignment, result.error_code or "team_assignment_failed")
            return True
        contribution_ref = self._publish_text(
            result.output,
            tenant_key=run.tenant_key,
            run_id=run.run_id,
            kind="team_contribution",
        )
        self._effect(aggregate, "employee_dispatch", "committed")
        self._commit(
            JournalEvent(
                event_type="team.v2.assignment.completed",
                aggregate_id=aggregate,
                payload={
                    "run_id": run.run_id,
                    "contribution_ref": contribution_ref.to_dict(),
                    "history_record_id": result.history_record_id,
                },
            )
        )
        return True

    def _assignment_failed(self, assignment, error_code: str) -> None:
        self._commit(
            JournalEvent(
                event_type="team.v2.assignment.failed",
                aggregate_id=assignment.assignment_id,
                payload={"run_id": assignment.run_id, "error_code": error_code},
            )
        )

    def _finalize(self, run: TeamRunV2, result_ref: BlobRef, output: str) -> None:
        aggregate = f"{run.run_id}:notify"
        projection = self.projection()
        notify_state = projection.effects.get((aggregate, "notify"))
        if notify_state is None:
            self._effect(aggregate, "notify", "prepared")
            self._effect(aggregate, "notify", "executing")
            self._notify(run, output)
            self._effect(aggregate, "notify", "committed")
        elif notify_state == "executing":
            self._notify(run, output)
            self._effect(aggregate, "notify", "committed")
        self._commit(
            JournalEvent(
                event_type="team.v2.run.completed",
                aggregate_id=run.run_id,
                payload={"run_id": run.run_id, "result_ref": result_ref.to_dict()},
            )
        )

    def _notify(self, run: TeamRunV2, output: str) -> None:
        idempotency_key = hashlib.sha256(
            f"team-final\0{run.run_id}".encode()
        ).hexdigest()[:50]
        try:
            self._backend.notify(
                run.message_id,
                run.chat_id,
                output,
                idempotency_key=idempotency_key,
            )
        except TypeError:
            # Compatibility-only backends predate the idempotent notify port.
            # Production coordinator composition always accepts this key.
            self._backend.notify(run.message_id, run.chat_id, output)

    def _block(self, run_id: str, error_code: str) -> None:
        with self._lock:
            projection = self.projection()
            run = projection.runs.get(run_id)
            if run is None or run.phase in {
                TeamRunPhase.COMPLETED,
                TeamRunPhase.BLOCKED,
                TeamRunPhase.CANCELED,
            }:
                return
            for (aggregate, effect_type), state in projection.effects.items():
                if (aggregate == run_id or aggregate.startswith(run_id + ":")) and state in {
                    "prepared",
                    "executing",
                }:
                    self._effect(aggregate, effect_type, "action_required")
            self._phase(run, TeamRunPhase.BLOCKED, error=error_code)

    def _phase(
        self,
        run: TeamRunV2,
        phase: TeamRunPhase,
        *,
        turn: int | None = None,
        handoff: int | None = None,
        error: str = "",
    ) -> None:
        next_turn = run.turn_count if turn is None else turn
        if next_turn > MAX_TEAM_TURNS:
            raise TeamCoordinatorError("team turn bound exceeded")
        self._commit(
            JournalEvent(
                event_type="team.v2.run.phase_changed",
                aggregate_id=run.run_id,
                payload={
                    "run_id": run.run_id,
                    "phase": phase.value,
                    "turn_count": next_turn,
                    "handoff_count": run.handoff_count if handoff is None else handoff,
                    "error_code": error,
                },
            )
        )

    def _assignment_by_role(self, run_id: str, role: str):
        return self._assignments_by_role(run_id, role)[0]

    def _assignments_by_role(self, run_id: str, role: str):
        projection = self.projection()
        return tuple(
            sorted(
                (
                    item
                    for item in projection.assignments.values()
                    if item.run_id == run_id and item.role == role
                ),
                key=lambda item: item.assignment_id,
            )
        )

    @staticmethod
    def _select_target(run, targets, task: str, *, role: str, exclude=frozenset()):
        del run
        task_words = set(task.casefold().replace("/", " ").split())
        explicit = {item.agent_id for item in targets if f"@{item.agent_id}" in task}
        eligible = []
        for item in targets:
            if item.agent_id in exclude:
                continue
            status = str(getattr(item, "runtime_status", "ready")).casefold()
            if status not in {"ready", "ready_cold", "ready_warm"}:
                continue
            capabilities = {str(value).casefold() for value in getattr(item, "capabilities", ())}
            haystack = capabilities | set(str(getattr(item, "role", "")).casefold().split())
            if not any(value.strip() for value in haystack):
                continue
            role_match = (
                role == "review"
                and any("review" in value for value in haystack)
            ) or (
                role == "execute"
                and any(
                    marker in value
                    for value in haystack
                    for marker in ("coder", "developer", "implementation")
                )
            )
            task_match = any(word in " ".join(haystack) for word in task_words if len(word) > 2)
            eligible.append(
                (
                    0 if item.agent_id in explicit else 1,
                    0 if role_match else 1,
                    0 if task_match else 1,
                    int(getattr(item, "mailbox_load", 0)),
                    item.agent_id,
                    item,
                )
            )
        if not eligible:
            return None
        eligible.sort(key=lambda value: value[:-1])
        return eligible[0][-1]

    @staticmethod
    def _validate_decision(decision: CoordinatorDecision, targets: tuple[object, ...]) -> None:
        active_ids = {item.agent_id for item in targets}
        if decision.action is not CoordinatorAction.ASSIGN or decision.role != "execute":
            raise TeamCoordinatorError("coordinator planning decision is invalid")
        if not set(decision.agent_ids) <= active_ids:
            raise TeamCoordinatorError("coordinator selected an unavailable employee")

    def _effect(self, aggregate: str, effect_type: str, state: str) -> None:
        self._commit(
            JournalEvent(
                event_type=f"team.v2.effect.{state}",
                aggregate_id=aggregate,
                payload={"effect_type": effect_type},
            )
        )

    def _publish_json(self, value: object, **labels: str) -> BlobRef:
        return self._blobs.stage_and_publish(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode(), labels, self._key
        )

    def _publish_text(self, value: str, **labels: str) -> BlobRef:
        return self._blobs.stage_and_publish(value.encode(), labels, self._key)

    def _read_json(self, ref: BlobRef):
        return json.loads(self._blobs.read(ref))

    def _read_text(self, ref: BlobRef | None) -> str:
        if ref is None:
            raise TeamCoordinatorError("team contribution is unavailable")
        return self._blobs.read(ref).decode()

    def _commit(self, event: JournalEvent) -> None:
        with self._lock, self._writer.transaction_guard():
            frames = list(self._writer.replay())
            try:
                rebuild_team_projection((*frames, SimpleNamespace(events=(event,))))
            except TeamProjectionError as exc:
                raise TeamCoordinatorError(str(exc)) from exc
            last = self._writer.get_last_frame()
            result = self._writer.commit(
                (event,),
                self._writer.get_aggregate_versions((event.aggregate_id,)),
                expected_head_sequence=0 if last is None else last.sequence,
                expected_head_hash="" if last is None else last.frame_hash,
            )
        if result.state is not CommitState.ANCHORED:
            raise TeamCoordinatorError("team coordinator event was not anchored")


__all__ = [
    "DecisionProvider",
    "SessionCoordinatorDecisionProvider",
    "TeamCoordinatorActor",
    "TeamCoordinatorError",
]
