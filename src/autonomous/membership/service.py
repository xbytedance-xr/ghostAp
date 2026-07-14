"""Journal-backed membership mutation, reconciliation, and Router health."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from ..domain import EmployeeState, WorkerType
from ..journal.frame import JournalEvent
from ..journal.writer import JournalWriter
from ..workforce.projection import commit_workforce_events_unlocked
from .lark import MembershipRemoteRejected, MembershipRemoteUnknown
from .models import (
    MembershipEffect,
    MembershipEffectState,
    MembershipOperation,
    MembershipState,
    membership_effect_id,
)
from .projection import (
    EFFECT_ACTION_REQUIRED,
    EFFECT_COMMITTED,
    EFFECT_EXECUTING,
    EFFECT_PREPARED,
    MembershipProjectionState,
    MembershipRecord,
    reduce_membership_frame,
)


class MembershipServiceError(RuntimeError):
    pass


class MembershipAuthorizationError(MembershipServiceError):
    pass


class MembershipBindingError(MembershipServiceError):
    pass


@dataclass(frozen=True, slots=True)
class MembershipMutationRequest:
    tenant_key: str
    chat_id: str
    agent_id: str
    requester_principal_id: str
    operation: MembershipOperation

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value and value == value.strip()
            for value in (
                self.tenant_key,
                self.chat_id,
                self.agent_id,
                self.requester_principal_id,
            )
        ):
            raise ValueError("membership mutation coordinates are required")
        try:
            object.__setattr__(
                self,
                "operation",
                MembershipOperation(self.operation),
            )
        except (TypeError, ValueError):
            raise ValueError("invalid membership operation") from None


@dataclass(frozen=True, slots=True)
class MembershipMutationOutcome:
    state: MembershipState
    confirmed: bool
    changed: bool
    effect_id: str = ""
    error_code: str = ""


@dataclass(frozen=True, slots=True)
class _Authority:
    app_id: str
    credential_ref: str
    member_groups: tuple[str, ...]


class EmployeeMembershipService:
    """Own canonical employee membership; legacy registry is never a fallback."""

    def __init__(
        self,
        *,
        writer: JournalWriter,
        hire_service: Any,
        remote: Any,
        admin_principal_ids: frozenset[str],
        team_owner_resolver: Callable[[str], str],
        team_active_resolver: Callable[[str], bool],
    ) -> None:
        if not isinstance(writer, JournalWriter):
            raise TypeError("writer must be JournalWriter")
        if not callable(team_owner_resolver):
            raise TypeError("team_owner_resolver is required")
        if not callable(team_active_resolver):
            raise TypeError("team_active_resolver is required")
        self._writer = writer
        self._hire = hire_service
        self._remote = remote
        self._admins = frozenset(admin_principal_ids)
        self._team_owner_resolver = team_owner_resolver
        self._team_active_resolver = team_active_resolver
        self._state = MembershipProjectionState()
        self._mutex = threading.RLock()
        self._chat_locks: dict[str, threading.RLock] = {}
        self._chat_locks_guard = threading.Lock()
        self.rebuild_projection()

    @property
    def state(self) -> MembershipProjectionState:
        with self._mutex:
            return self._state.clone()

    def get(
        self,
        tenant_key: str,
        chat_id: str,
        agent_id: str,
    ) -> MembershipRecord | None:
        self.rebuild_projection()
        with self._mutex:
            return self._state.records.get((tenant_key, chat_id, agent_id))

    def get_employee(self, tenant_key: str, agent_id: str) -> Any | None:
        """Return one projected employee without exposing a writable registry."""

        projection = self._hire.synchronize_projection()
        employee = projection.employees.get(agent_id)
        if employee is None or employee.tenant_key != tenant_key:
            return None
        return employee

    def find_employee_by_name(self, tenant_key: str, name: str) -> Any | None:
        """Resolve an exact tenant-scoped visible employee name."""

        normalized = name.casefold()
        matches = [
            employee
            for employee in self.list_employees(tenant_key)
            if employee.name.casefold() == normalized
        ]
        if len(matches) > 1:
            raise MembershipBindingError("employee name is ambiguous")
        return matches[0] if matches else None

    def list_employees(self, tenant_key: str) -> list[Any]:
        projection = self._hire.synchronize_projection()
        return [
            employee
            for employee in projection.employees.values()
            if employee.tenant_key == tenant_key
            and employee.state is EmployeeState.ACTIVE
            and employee.worker_type is WorkerType.VISIBLE
        ]

    def is_degraded(self, agent_id: str, team_id: str) -> bool:
        """Deny unless Journal proves ACTIVE membership for this exact chat."""

        self.rebuild_projection()
        projection = self._hire.synchronize_projection()
        employee = projection.employees.get(agent_id)
        if employee is None or team_id not in employee.member_groups:
            return True
        with self._mutex:
            matches = tuple(
                record
                for key, record in self._state.records.items()
                if key[1] == team_id and key[2] == agent_id
            )
        return len(matches) != 1 or matches[0].state is not MembershipState.ACTIVE

    def mutate(
        self,
        request: MembershipMutationRequest,
    ) -> MembershipMutationOutcome:
        if not isinstance(request, MembershipMutationRequest):
            raise TypeError("request must be MembershipMutationRequest")
        with self._chat_lock(request.chat_id):
            authority = self._resolve_authority(request)
            desired = request.operation is MembershipOperation.ADD
            projected = request.chat_id in authority.member_groups
            if projected is desired:
                try:
                    observed = self._observe(request, authority)
                except MembershipRemoteUnknown:
                    effect = self._prepare(request, authority)
                    self._mark_executing(effect.effect_id)
                    return self._mark_action_required(
                        effect.effect_id,
                        "idempotency_observation_unknown",
                    )
                if observed is desired:
                    return MembershipMutationOutcome(
                        state=(MembershipState.ACTIVE if desired else MembershipState.ABSENT),
                        confirmed=True,
                        changed=False,
                    )

            effect = self._prepare(request, authority)
            self._mark_executing(effect.effect_id)
            mutation_error = ""
            try:
                self._remote.mutate(
                    request.operation,
                    chat_id=request.chat_id,
                    app_id=authority.app_id,
                )
            except MembershipRemoteRejected:
                mutation_error = "remote_rejected"
            except Exception:
                mutation_error = "remote_unknown"
            try:
                observed = self._observe(request, authority)
            except MembershipRemoteUnknown:
                return self._mark_action_required(
                    effect.effect_id,
                    mutation_error or "remote_unknown",
                )
            if observed is desired:
                return self._commit_confirmed(effect.effect_id, observed)
            return self._mark_action_required(
                effect.effect_id,
                mutation_error or "observation_mismatch",
            )

    def reconcile_event(
        self,
        *,
        tenant_key: str,
        chat_id: str,
        agent_id: str,
    ) -> MembershipMutationOutcome:
        """Use a durable membership event only to trigger employee observation."""

        with self._chat_lock(chat_id):
            if self._team_active_resolver(chat_id) is not True:
                raise MembershipBindingError("membership team is not active")
            projection = self._hire.synchronize_projection()
            employee = projection.employees.get(agent_id)
            if (
                employee is None
                or employee.tenant_key != tenant_key
                or employee.state is not EmployeeState.ACTIVE
                or employee.worker_type is not WorkerType.VISIBLE
                or not employee.bot_principal_id
            ):
                raise MembershipBindingError("employee membership authority unavailable")
            principal = projection.bot_principals.get(employee.bot_principal_id)
            if (
                principal is None
                or principal.tenant_key != tenant_key
                or principal.agent_id != agent_id
                or not principal.app_id
                or not principal.credential_ref
            ):
                raise MembershipBindingError("employee principal authority unavailable")
            authority = _Authority(
                app_id=principal.app_id,
                credential_ref=principal.credential_ref,
                member_groups=employee.member_groups,
            )
            probe = MembershipMutationRequest(
                tenant_key=tenant_key,
                chat_id=chat_id,
                agent_id=agent_id,
                requester_principal_id="system_membership_event",
                operation=(
                    MembershipOperation.ADD
                    if chat_id in employee.member_groups
                    else MembershipOperation.REMOVE
                ),
            )
            try:
                observed = self._observe(probe, authority)
            except MembershipRemoteUnknown:
                effect = self._prepare(probe, authority)
                self._mark_executing(effect.effect_id)
                return self._mark_action_required(
                    effect.effect_id,
                    "event_observation_unknown",
                )
            operation = MembershipOperation.ADD if observed else MembershipOperation.REMOVE
            request = replace(probe, operation=operation)
            record = self.get(tenant_key, chat_id, agent_id)
            projected = chat_id in employee.member_groups
            desired_state = MembershipState.ACTIVE if observed else MembershipState.ABSENT
            if projected is observed and record is not None and record.state is desired_state:
                return MembershipMutationOutcome(
                    state=desired_state,
                    confirmed=True,
                    changed=False,
                )
            effect = self._prepare(request, authority)
            self._mark_executing(effect.effect_id)
            return self._commit_confirmed(effect.effect_id, observed)

    def rebuild_projection(self) -> MembershipProjectionState:
        with self._mutex:
            fresh = MembershipProjectionState()
            for frame in self._writer.replay():
                reduce_membership_frame(fresh, frame)
            self._state = fresh
            return self._state

    def recover_pending(self) -> int:
        """Observe incomplete effects without replaying an external mutation."""

        self.rebuild_projection()
        pending = tuple(
            effect
            for effect in self.state.effects.values()
            if not effect.state.terminal
        )
        recovered = 0
        for snapshot in pending:
            with self._chat_lock(snapshot.chat_id):
                self.rebuild_projection()
                effect = self._state.effects.get(snapshot.effect_id)
                if effect is None or effect.state.terminal:
                    continue
                if effect.state is MembershipEffectState.PREPARED:
                    self._mark_action_required(
                        effect.effect_id,
                        "prepared_recovery_unknown",
                    )
                    recovered += 1
                    continue
                try:
                    authority = self._authority_for_effect(effect)
                    request = MembershipMutationRequest(
                        tenant_key=effect.tenant_key,
                        chat_id=effect.chat_id,
                        agent_id=effect.agent_id,
                        requester_principal_id=effect.requester_principal_id,
                        operation=effect.operation,
                    )
                    observed = self._observe(request, authority)
                except (MembershipBindingError, MembershipRemoteUnknown):
                    self._mark_action_required(
                        effect.effect_id,
                        "recovery_observation_unknown",
                    )
                else:
                    desired = effect.operation is MembershipOperation.ADD
                    if observed is desired:
                        self._commit_confirmed(effect.effect_id, observed)
                    else:
                        self._mark_action_required(
                            effect.effect_id,
                            "recovery_observation_mismatch",
                        )
                recovered += 1
        return recovered

    def _authority_for_effect(self, effect: MembershipEffect) -> _Authority:
        projection = self._hire.synchronize_projection()
        employee = projection.employees.get(effect.agent_id)
        if (
            employee is None
            or employee.tenant_key != effect.tenant_key
            or employee.state is not EmployeeState.ACTIVE
            or employee.worker_type is not WorkerType.VISIBLE
            or not employee.bot_principal_id
        ):
            raise MembershipBindingError("employee membership authority unavailable")
        principal = projection.bot_principals.get(employee.bot_principal_id)
        if (
            principal is None
            or principal.agent_id != effect.agent_id
            or principal.app_id != effect.app_id
            or not principal.credential_ref
        ):
            raise MembershipBindingError("employee principal authority unavailable")
        return _Authority(
            app_id=principal.app_id,
            credential_ref=principal.credential_ref,
            member_groups=employee.member_groups,
        )

    def _resolve_authority(
        self,
        request: MembershipMutationRequest,
    ) -> _Authority:
        projection = self._hire.synchronize_projection()
        employee = projection.employees.get(request.agent_id)
        if (
            employee is None
            or employee.tenant_key != request.tenant_key
            or employee.state is not EmployeeState.ACTIVE
            or employee.worker_type is not WorkerType.VISIBLE
            or not employee.bot_principal_id
        ):
            raise MembershipBindingError("employee membership authority unavailable")
        principal = projection.bot_principals.get(employee.bot_principal_id)
        if (
            principal is None
            or principal.tenant_key != request.tenant_key
            or principal.agent_id != request.agent_id
            or not principal.app_id
            or not principal.credential_ref
        ):
            raise MembershipBindingError("employee principal authority unavailable")
        owner = self._team_owner_resolver(request.chat_id)
        if self._team_active_resolver(request.chat_id) is not True:
            raise MembershipBindingError("membership team is not active")
        if request.requester_principal_id not in self._admins and (
            not owner or request.requester_principal_id != owner
        ):
            raise MembershipAuthorizationError("membership mutation is not authorized")
        return _Authority(
            app_id=principal.app_id,
            credential_ref=principal.credential_ref,
            member_groups=employee.member_groups,
        )

    def _observe(
        self,
        request: MembershipMutationRequest,
        authority: _Authority,
    ) -> bool:
        try:
            value = self._remote.is_member(
                chat_id=request.chat_id,
                agent_id=request.agent_id,
                app_id=authority.app_id,
                credential_ref=authority.credential_ref,
            )
        except MembershipRemoteUnknown:
            raise
        except Exception:
            raise MembershipRemoteUnknown("membership_observation_unknown") from None
        if type(value) is not bool:
            raise MembershipRemoteUnknown("membership_observation_unknown")
        return value

    def _prepare(
        self,
        request: MembershipMutationRequest,
        authority: _Authority,
    ) -> MembershipEffect:
        with self._hire.employee_dispatch_guard(), self._mutex, self._writer.transaction_guard():
            self._synchronize_unlocked()
            key = (request.tenant_key, request.chat_id, request.agent_id)
            current = self._state.records.get(key)
            epoch = 1 if current is None else current.membership_epoch + 1
            effect = MembershipEffect(
                schema_version=1,
                effect_id=membership_effect_id(
                    request.tenant_key,
                    request.chat_id,
                    request.agent_id,
                    request.operation,
                    epoch,
                ),
                tenant_key=request.tenant_key,
                chat_id=request.chat_id,
                agent_id=request.agent_id,
                app_id=authority.app_id,
                requester_principal_id=request.requester_principal_id,
                operation=request.operation,
                state=MembershipEffectState.PREPARED,
                membership_epoch=epoch,
                error_code="",
            )
            self._commit_unlocked(
                JournalEvent(
                    event_type=EFFECT_PREPARED,
                    aggregate_id=effect.effect_id,
                    payload={"effect": effect.to_dict()},
                ),
            )
            return effect

    def _mark_executing(self, effect_id: str) -> None:
        with self._hire.employee_dispatch_guard(), self._mutex, self._writer.transaction_guard():
            self._synchronize_unlocked()
            self._commit_unlocked(
                JournalEvent(
                    event_type=EFFECT_EXECUTING,
                    aggregate_id=effect_id,
                    payload={"effect_id": effect_id},
                ),
            )

    def _commit_confirmed(
        self,
        effect_id: str,
        observed: bool,
    ) -> MembershipMutationOutcome:
        with self._hire.employee_dispatch_guard(), self._mutex, self._writer.transaction_guard():
            self._synchronize_unlocked()
            effect = self._state.effects[effect_id]
            employee = self._hire.projection_state.employees[effect.agent_id]
            groups = [
                group
                for group in employee.member_groups
                if group != effect.chat_id
            ]
            if observed:
                groups.append(effect.chat_id)
            events = (
                JournalEvent(
                    event_type=EFFECT_COMMITTED,
                    aggregate_id=effect_id,
                    payload={
                        "effect_id": effect_id,
                        "observed_is_member": observed,
                    },
                ),
                JournalEvent(
                    event_type="employee.membership_changed",
                    aggregate_id=effect.agent_id,
                    payload={"member_groups": list(dict.fromkeys(groups))},
                ),
            )
            self._commit_unlocked(*events)
            record = self._state.records[
                (effect.tenant_key, effect.chat_id, effect.agent_id)
            ]
            return MembershipMutationOutcome(
                state=record.state,
                confirmed=True,
                changed=True,
                effect_id=effect_id,
            )

    def _mark_action_required(
        self,
        effect_id: str,
        error_code: str,
    ) -> MembershipMutationOutcome:
        with self._hire.employee_dispatch_guard(), self._mutex, self._writer.transaction_guard():
            self._synchronize_unlocked()
            self._commit_unlocked(
                JournalEvent(
                    event_type=EFFECT_ACTION_REQUIRED,
                    aggregate_id=effect_id,
                    payload={
                        "effect_id": effect_id,
                        "error_code": error_code,
                    },
                ),
            )
            effect = self._state.effects[effect_id]
            record = self._state.records[
                (effect.tenant_key, effect.chat_id, effect.agent_id)
            ]
            return MembershipMutationOutcome(
                state=record.state,
                confirmed=False,
                changed=False,
                effect_id=effect_id,
                error_code=error_code,
            )

    def _synchronize_unlocked(self) -> None:
        self._hire.synchronize_projection_unlocked()
        last = self._writer.get_last_frame()
        head = (0, "") if last is None else (last.sequence, last.frame_hash)
        if (self._state.cursor_sequence, self._state.cursor_hash) != head:
            fresh = MembershipProjectionState()
            for frame in self._writer.replay():
                reduce_membership_frame(fresh, frame)
            self._state = fresh

    def _commit_unlocked(self, *events: JournalEvent) -> None:
        result = commit_workforce_events_unlocked(
            self._writer,
            self._hire.projection_state,
            events,
        )
        reduce_membership_frame(self._state, result.frame)

    def _chat_lock(self, chat_id: str) -> threading.RLock:
        with self._chat_locks_guard:
            return self._chat_locks.setdefault(chat_id, threading.RLock())


__all__ = [
    "EmployeeMembershipService",
    "MembershipAuthorizationError",
    "MembershipBindingError",
    "MembershipMutationOutcome",
    "MembershipMutationRequest",
    "MembershipServiceError",
]
