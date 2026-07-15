"""Journal/workforce authority for employee retirement admission and terminal facts."""

from __future__ import annotations

from datetime import UTC, datetime

from ..domain import EmployeeState, WorkerType
from ..ingress.service import EmployeeIngressService
from ..journal.frame import JournalEvent
from ..journal.writer import CommitState, JournalWriter
from ..workforce.projection import validate_workforce_events
from .fire_service import (
    EmployeeFireRequest,
    EmployeeFireTarget,
    FireServiceError,
)


class JournalFireAuthority:
    """Authorize admins and commit retirement facts into the shared Journal."""

    def __init__(
        self,
        *,
        writer: JournalWriter,
        hire_service: object,
        ingress_service: EmployeeIngressService,
        admin_principal_ids: frozenset[str],
    ) -> None:
        self._writer = writer
        self._hire = hire_service
        self._ingress = ingress_service
        self._admins = frozenset(admin_principal_ids)

    def resolve(self, request: EmployeeFireRequest) -> EmployeeFireTarget:
        if request.requester_principal_id not in self._admins:
            raise FireServiceError("fire is not authorized")
        projection = self._hire.synchronize_projection()
        matches = tuple(
            employee
            for employee in projection.employees.values()
            if employee.tenant_key == request.tenant_key
            and employee.worker_type is WorkerType.VISIBLE
            and employee.state
            in {
                EmployeeState.PROVISIONING_APP,
                EmployeeState.STORING_CREDENTIAL,
                EmployeeState.CONFIGURING,
                EmployeeState.VALIDATING,
                EmployeeState.READY_PENDING_VERIFICATION,
                EmployeeState.ACTIVE,
                EmployeeState.ACTION_REQUIRED,
            }
            and request.employee in {employee.agent_id, employee.name}
        )
        if len(matches) != 1:
            raise FireServiceError("retirable employee was not uniquely resolved")
        employee = matches[0]
        if not employee.bot_principal_id:
            raise FireServiceError("employee bot principal is unavailable")
        principal = projection.bot_principals.get(employee.bot_principal_id)
        if (
            principal is None
            or principal.tenant_key != employee.tenant_key
            or principal.agent_id != employee.agent_id
            or not principal.app_id
            or not principal.credential_ref
        ):
            raise FireServiceError("employee credential authority is unavailable")
        return EmployeeFireTarget(
            tenant_key=employee.tenant_key,
            agent_id=employee.agent_id,
            employee_name=employee.name,
            bot_principal_id=employee.bot_principal_id,
            app_id=principal.app_id,
            credential_ref=principal.credential_ref,
        )

    def admit(
        self,
        request: EmployeeFireRequest,
        target: EmployeeFireTarget,
        intent_id: str,
    ) -> None:
        ingress_aggregate = f"employee-ingress:{target.tenant_key}:{target.agent_id}"
        requested = JournalEvent(
            event_type="fire.requested",
            aggregate_id=intent_id,
            payload={
                "intent_id": intent_id,
                "tenant_key": request.tenant_key,
                "message_id": request.message_id,
                "chat_id": request.chat_id,
                "requester_principal_id": request.requester_principal_id,
                "agent_id": target.agent_id,
                "employee_name": target.employee_name,
                "bot_principal_id": target.bot_principal_id,
                "app_id": target.app_id,
                "credential_ref": target.credential_ref,
                "drain": request.drain,
            },
        )
        retiring = JournalEvent(
            event_type="employee.state_changed",
            aggregate_id=target.agent_id,
            payload={"state": EmployeeState.RETIRING.value},
        )
        ingress_closed = JournalEvent(
            event_type="employee.ingress.closed",
            aggregate_id=ingress_aggregate,
            payload={
                "tenant_key": target.tenant_key,
                "agent_id": target.agent_id,
                "reason_code": "retiring",
                "closed_at": datetime.now(UTC).isoformat(),
            },
        )
        with self._hire.employee_dispatch_guard(), self._ingress.employee_dispatch_guard(), self._writer.transaction_guard():
            self._ingress.synchronize_projection_unlocked()
            projection = self._hire.synchronize_projection_unlocked()
            current = projection.employees.get(target.agent_id)
            if current is None or current.state not in {
                EmployeeState.PROVISIONING_APP,
                EmployeeState.STORING_CREDENTIAL,
                EmployeeState.CONFIGURING,
                EmployeeState.VALIDATING,
                EmployeeState.READY_PENDING_VERIFICATION,
                EmployeeState.ACTIVE,
                EmployeeState.ACTION_REQUIRED,
            }:
                raise FireServiceError("employee is not retirable")
            hire_effect_dispositions = self._hire_effect_dispositions(target.agent_id)
            validate_workforce_events(projection, (retiring,))
            frame = self._commit_unlocked(
                (
                    requested,
                    *hire_effect_dispositions,
                    retiring,
                    ingress_closed,
                )
            )
            self._hire.apply_committed_frame_unlocked(frame)
            self._ingress.apply_committed_frame_unlocked(frame)

    def _hire_effect_dispositions(self, agent_id: str) -> tuple[JournalEvent, ...]:
        list_states = getattr(self._hire, "list_states", None)
        if not callable(list_states):
            return ()
        matches = [state for state in list_states() if state.agent_id == agent_id]
        if len(matches) > 1:
            raise FireServiceError("employee hire authority is ambiguous")
        if not matches:
            return ()
        state = matches[0]
        effect_types = dict(state.effect_types)
        events: list[JournalEvent] = []
        for effect_id, effect_state in state.effects:
            if effect_state.value not in {"prepared", "executing"}:
                continue
            effect_type = effect_types.get(effect_id)
            if not effect_type:
                raise FireServiceError("employee hire effect type is unavailable")
            metadata = {
                **dict(state.metadata_for(effect_id)),
                "error_code": "retirement_requested",
            }
            events.append(
                JournalEvent(
                    event_type="hire.effect.action_required",
                    aggregate_id=state.intent_id,
                    payload={
                        "effect_id": effect_id,
                        "effect_type": effect_type,
                        "metadata": metadata,
                    },
                )
            )
        return tuple(events)

    def mark_action_required(self, agent_id: str) -> None:
        self._commit_employee_state(agent_id, EmployeeState.ACTION_REQUIRED)

    def mark_credential_destroyed(self, target: EmployeeFireTarget) -> None:
        event = JournalEvent(
            event_type="credential.destroyed",
            aggregate_id=target.bot_principal_id,
            payload={"credential_ref": target.credential_ref},
        )
        with self._hire.employee_dispatch_guard(), self._writer.transaction_guard():
            projection = self._hire.synchronize_projection_unlocked()
            principal = projection.bot_principals.get(target.bot_principal_id)
            if principal is None:
                raise FireServiceError("employee bot principal is unavailable")
            if principal.credential_ref == "":
                return
            validate_workforce_events(projection, (event,))
            frame = self._commit_unlocked((event,))
            self._hire.apply_committed_frame_unlocked(frame)

    def mark_archived(self, agent_id: str, intent_id: str) -> None:
        archived = JournalEvent(
            event_type="employee.state_changed",
            aggregate_id=agent_id,
            payload={"state": EmployeeState.ARCHIVED.value},
        )
        completed = JournalEvent(
            event_type="fire.completed",
            aggregate_id=intent_id,
            payload={"external_app_disposition": "manual_deletion_required"},
        )
        with self._hire.employee_dispatch_guard(), self._writer.transaction_guard():
            projection = self._hire.synchronize_projection_unlocked()
            validate_workforce_events(projection, (archived,))
            frame = self._commit_unlocked((archived, completed))
            self._hire.apply_committed_frame_unlocked(frame)

    def _commit_employee_state(self, agent_id: str, state: EmployeeState) -> None:
        event = JournalEvent(
            event_type="employee.state_changed",
            aggregate_id=agent_id,
            payload={"state": state.value},
        )
        with self._hire.employee_dispatch_guard(), self._writer.transaction_guard():
            projection = self._hire.synchronize_projection_unlocked()
            current = projection.employees.get(agent_id)
            if current is None or current.state is state:
                return
            validate_workforce_events(projection, (event,))
            frame = self._commit_unlocked((event,))
            self._hire.apply_committed_frame_unlocked(frame)

    def _commit_unlocked(self, events: tuple[JournalEvent, ...]):
        last = self._writer.get_last_frame()
        sequence = 0 if last is None else last.sequence
        frame_hash = "" if last is None else last.frame_hash
        aggregate_ids = {event.aggregate_id for event in events}
        result = self._writer.commit(
            events,
            self._writer.get_aggregate_versions(aggregate_ids),
            expected_head_sequence=sequence,
            expected_head_hash=frame_hash,
        )
        if result.state is not CommitState.ANCHORED:
            raise FireServiceError("fire authority fact was not anchored")
        return result.frame


__all__ = ["JournalFireAuthority"]
