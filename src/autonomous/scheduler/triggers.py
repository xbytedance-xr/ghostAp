"""Schedule and standing trigger service for autonomous goals."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from ..domain.ids import new_id


class TriggerType(str, Enum):
    SCHEDULE = "schedule"
    STANDING = "standing"
    EVENT = "event"


class TriggerState(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    EXPIRED = "expired"
    CANCELED = "canceled"


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


@dataclass
class TriggerDefinition:
    trigger_id: str = field(default_factory=lambda: new_id("trg"))
    trigger_type: TriggerType = TriggerType.SCHEDULE
    goal_template_id: str = ""
    tenant_key: str = ""
    owner_principal_id: str = ""
    state: TriggerState = TriggerState.ACTIVE
    schedule_cron: str = ""
    event_filter: str = ""
    max_occurrences: int = 0
    occurrence_count: int = 0
    last_fired_at: float = 0.0
    next_fire_at: float = 0.0
    created_at: float = field(default_factory=time.time)


@dataclass
class TriggerOccurrence:
    occurrence_id: str = field(default_factory=lambda: new_id("occ"))
    trigger_id: str = ""
    goal_id: str = ""
    run_id: str = ""
    fired_at: float = field(default_factory=time.time)


class TriggerService:
    """Journal-backed trigger scheduling and firing."""

    def __init__(self, journal: JournalWriter) -> None:
        self._journal = journal
        self._triggers: dict[str, TriggerDefinition] = {}
        self._occurrences: list[TriggerOccurrence] = []

    async def register(
        self,
        *,
        trigger_type: TriggerType = TriggerType.SCHEDULE,
        goal_template_id: str = "",
        tenant_key: str = "",
        owner_principal_id: str = "",
        schedule_cron: str = "",
        event_filter: str = "",
        max_occurrences: int = 0,
    ) -> TriggerDefinition:
        trigger = TriggerDefinition(
            trigger_type=trigger_type,
            goal_template_id=goal_template_id,
            tenant_key=tenant_key,
            owner_principal_id=owner_principal_id,
            schedule_cron=schedule_cron,
            event_filter=event_filter,
            max_occurrences=max_occurrences,
        )
        self._triggers[trigger.trigger_id] = trigger

        await self._journal.write_event("trigger.registered", {
            "trigger_id": trigger.trigger_id,
            "trigger_type": trigger_type.value,
            "goal_template_id": goal_template_id,
            "tenant_key": tenant_key,
        })
        return trigger

    async def fire(self, trigger_id: str, *, goal_id: str = "", run_id: str = "") -> TriggerOccurrence | None:
        trigger = self._triggers.get(trigger_id)
        if trigger is None:
            raise ValueError(f"trigger not found: {trigger_id}")
        if trigger.state is not TriggerState.ACTIVE:
            return None
        if trigger.max_occurrences and trigger.occurrence_count >= trigger.max_occurrences:
            trigger.state = TriggerState.EXPIRED
            return None

        occurrence = TriggerOccurrence(
            trigger_id=trigger_id,
            goal_id=goal_id,
            run_id=run_id,
        )
        trigger.occurrence_count += 1
        trigger.last_fired_at = occurrence.fired_at
        self._occurrences.append(occurrence)

        await self._journal.write_event("trigger.fired", {
            "trigger_id": trigger_id,
            "occurrence_id": occurrence.occurrence_id,
            "occurrence_count": trigger.occurrence_count,
        })
        return occurrence

    async def pause(self, trigger_id: str) -> None:
        trigger = self._get(trigger_id)
        trigger.state = TriggerState.PAUSED
        await self._journal.write_event("trigger.paused", {"trigger_id": trigger_id})

    async def resume(self, trigger_id: str) -> None:
        trigger = self._get(trigger_id)
        if trigger.state is not TriggerState.PAUSED:
            raise ValueError("can only resume paused triggers")
        trigger.state = TriggerState.ACTIVE
        await self._journal.write_event("trigger.resumed", {"trigger_id": trigger_id})

    async def cancel(self, trigger_id: str) -> None:
        trigger = self._get(trigger_id)
        trigger.state = TriggerState.CANCELED
        await self._journal.write_event("trigger.canceled", {"trigger_id": trigger_id})

    def get_due_triggers(self, now: float | None = None) -> list[TriggerDefinition]:
        current = now or time.time()
        return [
            t for t in self._triggers.values()
            if t.state is TriggerState.ACTIVE
            and t.trigger_type is TriggerType.SCHEDULE
            and t.next_fire_at <= current
        ]

    def list_active(self, tenant_key: str = "") -> list[TriggerDefinition]:
        triggers = [
            t for t in self._triggers.values()
            if t.state is TriggerState.ACTIVE
        ]
        if tenant_key:
            triggers = [t for t in triggers if t.tenant_key == tenant_key]
        return triggers

    def _get(self, trigger_id: str) -> TriggerDefinition:
        trigger = self._triggers.get(trigger_id)
        if trigger is None:
            raise ValueError(f"trigger not found: {trigger_id}")
        return trigger
