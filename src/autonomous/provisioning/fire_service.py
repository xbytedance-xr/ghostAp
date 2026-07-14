"""Journal-backed fail-closed employee retirement orchestration."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from ..journal.frame import JournalEvent
from ..journal.writer import CommitState, JournalWriter
from .fire_state import (
    FIRE_EFFECT_ORDER,
    DurableFireState,
    FireEffectState,
    FirePhase,
    rebuild_fire_projection,
)


class FireServiceError(RuntimeError):
    """Retirement could not safely progress."""


@dataclass(frozen=True, slots=True)
class EmployeeFireRequest:
    employee: str
    tenant_key: str
    message_id: str
    chat_id: str
    requester_principal_id: str
    drain: bool = False

    def __post_init__(self) -> None:
        for name in (
            "employee", "tenant_key", "message_id", "chat_id", "requester_principal_id"
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{name} is required")
        if type(self.drain) is not bool:
            raise ValueError("drain must be bool")


@dataclass(frozen=True, slots=True)
class EmployeeFireTarget:
    tenant_key: str
    agent_id: str
    employee_name: str
    bot_principal_id: str
    app_id: str
    credential_ref: str


class FireAuthority(Protocol):
    def resolve(self, request: EmployeeFireRequest) -> EmployeeFireTarget: ...

    def admit(
        self,
        request: EmployeeFireRequest,
        target: EmployeeFireTarget,
        intent_id: str,
    ) -> None: ...

    def mark_action_required(self, agent_id: str) -> None: ...

    def mark_credential_destroyed(self, target: EmployeeFireTarget) -> None: ...

    def mark_archived(self, agent_id: str, intent_id: str) -> None: ...


class FireEffectPort(Protocol):
    def execute(self, state: DurableFireState) -> None: ...

    def observe(self, state: DurableFireState) -> bool | None: ...


class EmployeeFireService:
    """Run one-way cleanup with anchored effect transitions and safe recovery."""

    def __init__(
        self,
        *,
        writer: JournalWriter,
        authority: FireAuthority,
        effects: dict[str, FireEffectPort],
    ) -> None:
        if set(effects) != set(FIRE_EFFECT_ORDER):
            raise ValueError("all fire effects must be configured exactly once")
        self._writer = writer
        self._authority = authority
        self._effects = dict(effects)

    def start_fire(self, request: EmployeeFireRequest) -> DurableFireState:
        if not isinstance(request, EmployeeFireRequest):
            raise TypeError("request must be EmployeeFireRequest")
        intent_id = self._intent_id(request)
        state = self._states().get(intent_id)
        if state is not None:
            if not self._matches_existing(state, request):
                raise FireServiceError("fire idempotency conflict")
            return self.resume(intent_id)
        target = self._authority.resolve(request)
        self._authority.admit(request, target, intent_id)
        state = self._require(intent_id)
        if not self._matches(state, request, target):
            raise FireServiceError("fire idempotency conflict")
        return self.resume(intent_id)

    def resume(self, intent_id: str) -> DurableFireState:
        state = self._require(intent_id)
        if state.phase is FirePhase.ARCHIVED:
            return state
        if state.phase is FirePhase.ACTION_REQUIRED:
            return state
        target = EmployeeFireTarget(
            tenant_key=state.tenant_key,
            agent_id=state.agent_id,
            employee_name=state.employee_name,
            bot_principal_id=state.bot_principal_id,
            app_id=state.app_id,
            credential_ref=state.credential_ref,
        )
        for effect_type in FIRE_EFFECT_ORDER:
            state = self._require(intent_id)
            effect_state = state.effect_state(effect_type)
            if effect_state is FireEffectState.COMMITTED:
                if effect_type == "credential_destroy":
                    self._authority.mark_credential_destroyed(target)
                continue
            if effect_state is FireEffectState.EXECUTING:
                return self._reconcile_executing(state, effect_type)
            if effect_state is FireEffectState.ACTION_REQUIRED:
                return state
            if effect_state is None:
                self._transition(intent_id, effect_type, FireEffectState.PREPARED)
            self._transition(intent_id, effect_type, FireEffectState.EXECUTING)
            state = self._require(intent_id)
            try:
                self._effects[effect_type].execute(state)
            except Exception:
                pass
            observed = self._observe(state, effect_type)
            if observed is not True:
                return self._action_required(state, effect_type, "outcome_unknown")
            self._transition(intent_id, effect_type, FireEffectState.COMMITTED)
            if effect_type == "credential_destroy":
                self._authority.mark_credential_destroyed(target)
        self._authority.mark_archived(state.agent_id, intent_id)
        return self._require(intent_id)

    def recover(self) -> tuple[DurableFireState, ...]:
        recovered: list[DurableFireState] = []
        for intent_id, state in self._states().items():
            if state.phase is FirePhase.RETIRING:
                recovered.append(self.resume(intent_id))
            elif state.phase is FirePhase.ACTION_REQUIRED:
                self._authority.mark_action_required(state.agent_id)
        return tuple(recovered)

    def _reconcile_executing(
        self,
        state: DurableFireState,
        effect_type: str,
    ) -> DurableFireState:
        observed = self._observe(state, effect_type)
        if observed is not True:
            return self._action_required(state, effect_type, "recovery_outcome_unknown")
        self._transition(state.intent_id, effect_type, FireEffectState.COMMITTED)
        return self.resume(state.intent_id)

    def _observe(self, state: DurableFireState, effect_type: str) -> bool | None:
        try:
            value = self._effects[effect_type].observe(state)
        except Exception:
            return None
        return value if type(value) is bool else None

    def _action_required(
        self,
        state: DurableFireState,
        effect_type: str,
        error_code: str,
    ) -> DurableFireState:
        self._transition(
            state.intent_id,
            effect_type,
            FireEffectState.ACTION_REQUIRED,
            error_code=error_code,
        )
        self._authority.mark_action_required(state.agent_id)
        return self._require(state.intent_id)

    def _transition(
        self,
        intent_id: str,
        effect_type: str,
        state: FireEffectState,
        *,
        error_code: str = "",
    ) -> None:
        payload = {"effect_type": effect_type}
        if error_code:
            payload["error_code"] = error_code
        self._commit(
            JournalEvent(
                event_type=f"fire.effect.{state.value}",
                aggregate_id=intent_id,
                payload=payload,
            )
        )

    def _commit(self, event: JournalEvent) -> None:
        with self._writer.transaction_guard():
            last = self._writer.get_last_frame()
            sequence = 0 if last is None else last.sequence
            frame_hash = "" if last is None else last.frame_hash
            result = self._writer.commit(
                (event,),
                self._writer.get_aggregate_versions((event.aggregate_id,)),
                expected_head_sequence=sequence,
                expected_head_hash=frame_hash,
            )
        if result.state is not CommitState.ANCHORED:
            raise FireServiceError("fire transition was not anchored")
        self._states()

    def _states(self):
        return rebuild_fire_projection(tuple(self._writer.replay()))

    def _require(self, intent_id: str) -> DurableFireState:
        state = self._states().get(intent_id)
        if state is None:
            raise FireServiceError("unknown fire request")
        return state

    @staticmethod
    def _intent_id(request: EmployeeFireRequest) -> str:
        raw = "\x00".join((request.tenant_key, request.message_id))
        return f"fire_{hashlib.sha256(raw.encode()).hexdigest()}"

    @staticmethod
    def _matches(
        state: DurableFireState,
        request: EmployeeFireRequest,
        target: EmployeeFireTarget,
    ) -> bool:
        return (
            state.tenant_key == request.tenant_key
            and state.message_id == request.message_id
            and state.chat_id == request.chat_id
            and state.requester_principal_id == request.requester_principal_id
            and state.drain is request.drain
            and state.agent_id == target.agent_id
            and state.bot_principal_id == target.bot_principal_id
            and state.app_id == target.app_id
            and state.credential_ref == target.credential_ref
        )

    @staticmethod
    def _matches_existing(
        state: DurableFireState,
        request: EmployeeFireRequest,
    ) -> bool:
        return (
            state.tenant_key == request.tenant_key
            and state.message_id == request.message_id
            and state.chat_id == request.chat_id
            and state.requester_principal_id == request.requester_principal_id
            and state.drain is request.drain
            and request.employee in {state.agent_id, state.employee_name}
        )


__all__ = [
    "EmployeeFireRequest",
    "EmployeeFireService",
    "EmployeeFireTarget",
    "FireAuthority",
    "FireEffectPort",
    "FireServiceError",
]
