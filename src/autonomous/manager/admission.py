"""Durable Inbox and Admission - event ingress with dedup and one-shot goal creation.

The DurableInbox provides:
- Dedup on `tenant + chat_id + message_id + source_type`
- Atomic trigger consumption (accepted event, proposal decision, run/dedup,
  occurrence tombstone, cursor advance) in one journal frame
- One-shot goal creation from accepted events

The Admission layer coordinates inbox acceptance with goal/run lifecycle.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ..domain.enums import GoalState, GoalType, RunState
from ..domain.goals import GoalDefinition, GoalSpec, Run
from ..domain.ids import new_id
from ..journal.frame import JournalEvent
from ..journal.projections import InboxRecord, ProjectionState, apply_frame

if TYPE_CHECKING:
    from ..journal.writer import JournalWriter


class InboxEventType(str, Enum):
    USER_MESSAGE = "user_message"
    CARD_CALLBACK = "card_callback"
    SCHEDULE_TRIGGER = "schedule_trigger"
    EVENT_TRIGGER = "event_trigger"
    WORK_PROPOSAL = "work_proposal"
    APPROVAL_RESPONSE = "approval_response"
    DECISION_RESPONSE = "decision_response"


@dataclass(frozen=True)
class InboxEvent:
    """An inbound event to be accepted into the durable inbox."""

    event_id: str = field(default_factory=lambda: new_id("evt"))
    tenant: str = ""
    chat_id: str = ""
    message_id: str = ""
    source_type: str = "user_message"
    payload: dict[str, Any] = field(default_factory=dict)
    received_at: float = field(default_factory=time.time)

    @property
    def dedup_key(self) -> str:
        """Canonical dedup key: tenant + chat_id + message_id + source_type."""
        raw = f"{self.tenant}|{self.chat_id}|{self.message_id}|{self.source_type}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tenant": self.tenant,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "source_type": self.source_type,
            "payload": self.payload,
            "received_at": self.received_at,
            "dedup_key": self.dedup_key,
        }


class AdmissionResult(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    NEEDS_CLARIFICATION = "needs_clarification"


@dataclass(frozen=True)
class AdmissionDecision:
    result: AdmissionResult
    event_id: str = ""
    goal_id: str | None = None
    run_id: str | None = None
    reason: str = ""


class DurableInbox:
    """Durable inbox with dedup and atomic trigger consumption.

    All state is derived from journal replay (via ProjectionState).
    """

    def __init__(
        self,
        writer: JournalWriter,
        state: ProjectionState,
    ) -> None:
        self._writer = writer
        self._state = state

    @property
    def state(self) -> ProjectionState:
        return self._state

    def accept(self, event: InboxEvent) -> str | None:
        """Accept an event into the inbox. Returns event_id or None if duplicate.

        Dedup key: tenant + chat_id + message_id + source_type.
        Commits one journal frame with the inbox.event_received event.
        """
        dedup_key = event.dedup_key

        # Dedup check
        if dedup_key in self._state.dedup_keys:
            return None

        # Build journal event
        journal_event = JournalEvent(
            event_type="inbox.event_received",
            aggregate_id=event.event_id,
            payload={
                "event_id": event.event_id,
                "dedup_key": dedup_key,
                "tenant": event.tenant,
                "chat_id": event.chat_id,
                "message_id": event.message_id,
                "source_type": event.source_type,
                "payload": event.payload,
            },
        )

        # Commit to journal
        expected_versions = {
            event.event_id: self._state.goals.get(event.event_id, None)
            and 0
            or 0,
        }
        # Use aggregate_id versioning
        aggregate_id = event.event_id
        current_version = self._writer._aggregate_versions.get(aggregate_id, 0)
        expected_versions = {aggregate_id: current_version}

        result = self._writer.commit([journal_event], expected_versions)
        apply_frame(self._state, result.frame)
        return event.event_id

    def consume_trigger(
        self,
        event_id: str,
        *,
        goal_id: str,
        run_id: str,
        occurrence_key: str,
        cursor_sequence: int,
        cursor_hash: str = "",
    ) -> None:
        """Atomically consume a trigger - records all decisions in one frame.

        Records:
        1. Accepted event processed marker
        2. Proposal decision (goal_id, run_id)
        3. Run/dedup
        4. Occurrence tombstone
        5. Cursor advance
        """
        record = self._state.inbox.get(event_id)
        if record is None:
            raise ValueError(f"inbox event {event_id} not found")
        if record.processed:
            raise ValueError(f"inbox event {event_id} already processed")

        # Build atomic frame with all events
        events: list[JournalEvent] = []
        aggregate_ids: set[str] = set()

        # 1. Mark event processed
        events.append(JournalEvent(
            event_type="inbox.event_processed",
            aggregate_id=event_id,
            payload={
                "event_id": event_id,
                "goal_id": goal_id,
                "run_id": run_id,
            },
        ))
        aggregate_ids.add(event_id)

        # 2. Run creation
        events.append(JournalEvent(
            event_type="run.created",
            aggregate_id=run_id,
            payload={
                "run_id": run_id,
                "goal_id": goal_id,
                "occurrence_key": occurrence_key,
                "trigger_event_id": event_id,
                "state": RunState.QUEUED.value,
            },
        ))
        aggregate_ids.add(run_id)

        # 3. Occurrence tombstone
        events.append(JournalEvent(
            event_type="occurrence.consumed",
            aggregate_id=goal_id,
            payload={
                "occurrence_key": occurrence_key,
                "event_id": event_id,
            },
        ))
        aggregate_ids.add(goal_id)

        # 4. Cursor advance
        cursor_agg_id = f"cursor_{event_id}"
        events.append(JournalEvent(
            event_type="cursor.advanced",
            aggregate_id=cursor_agg_id,
            payload={
                "sequence": cursor_sequence,
                "hash": cursor_hash,
                "event_id": event_id,
            },
        ))
        aggregate_ids.add(cursor_agg_id)

        # Build expected versions for all aggregates
        expected_versions = {
            agg_id: self._writer._aggregate_versions.get(agg_id, 0)
            for agg_id in aggregate_ids
        }

        result = self._writer.commit(tuple(events), expected_versions)
        apply_frame(self._state, result.frame)

    def pending_events(self) -> list[InboxRecord]:
        """Return all unprocessed, non-tombstoned inbox events."""
        return self._state.pending_inbox()

    def is_duplicate(self, event: InboxEvent) -> bool:
        """Check if an event would be deduplicated."""
        return event.dedup_key in self._state.dedup_keys


class Admission:
    """Admission control - coordinates inbox + goal/run lifecycle.

    Uses DurableInbox for ingress and provides one-shot goal creation.
    """

    def __init__(self, writer: JournalWriter, state: ProjectionState) -> None:
        self._writer = writer
        self._state = state
        self._inbox = DurableInbox(writer, state)

    @property
    def inbox(self) -> DurableInbox:
        return self._inbox

    def create_one_shot_from_event(
        self,
        event_id: str,
        *,
        tenant: str = "",
        objective: str = "",
        owner_id: str = "",
    ) -> tuple[str, str]:
        """Create a one-shot goal + run from an accepted inbox event.

        Returns (goal_id, run_id).
        Commits atomically: goal.created + goal.activated + run.created +
        inbox.event_processed + occurrence.consumed + cursor.advanced.
        """
        record = self._state.inbox.get(event_id)
        if record is None:
            raise ValueError(f"inbox event {event_id} not found")
        if record.processed:
            raise ValueError(f"inbox event {event_id} already processed")

        goal_id = new_id("goal")
        run_id = new_id("run")
        occurrence_key = f"{event_id}:one_shot"

        # Build all events in one atomic frame
        events: list[JournalEvent] = []
        aggregate_ids: set[str] = set()

        # 1. Goal created
        goal_payload = GoalDefinition(
            goal_id=goal_id,
            tenant_key=tenant or record.payload.get("tenant", ""),
            owner_id=owner_id,
            goal_type=GoalType.ONE_SHOT,
            state=GoalState.ACTIVE,
            spec=GoalSpec(objective=objective or f"One-shot from event {event_id}"),
        ).to_dict()
        events.append(JournalEvent(
            event_type="goal.created",
            aggregate_id=goal_id,
            payload=goal_payload,
        ))
        aggregate_ids.add(goal_id)

        # 2. Run created
        events.append(JournalEvent(
            event_type="run.created",
            aggregate_id=run_id,
            payload={
                "run_id": run_id,
                "goal_id": goal_id,
                "goal_version": 1,
                "occurrence_key": occurrence_key,
                "trigger_event_id": event_id,
                "state": RunState.QUEUED.value,
                "tenant_key": tenant or record.payload.get("tenant", ""),
            },
        ))
        aggregate_ids.add(run_id)

        # 3. Inbox event processed
        events.append(JournalEvent(
            event_type="inbox.event_processed",
            aggregate_id=event_id,
            payload={
                "event_id": event_id,
                "goal_id": goal_id,
                "run_id": run_id,
            },
        ))
        aggregate_ids.add(event_id)

        # 4. Occurrence tombstone
        events.append(JournalEvent(
            event_type="occurrence.consumed",
            aggregate_id=f"occ_{goal_id}",
            payload={
                "occurrence_key": occurrence_key,
                "event_id": event_id,
                "goal_id": goal_id,
            },
        ))
        aggregate_ids.add(f"occ_{goal_id}")

        # 5. Cursor advance
        cursor_agg_id = f"cursor_{goal_id}"
        events.append(JournalEvent(
            event_type="cursor.advanced",
            aggregate_id=cursor_agg_id,
            payload={
                "sequence": self._state.cursor_sequence + 1,
                "hash": "",
                "event_id": event_id,
            },
        ))
        aggregate_ids.add(cursor_agg_id)

        # Commit atomically
        expected_versions = {
            agg_id: self._writer._aggregate_versions.get(agg_id, 0)
            for agg_id in aggregate_ids
        }

        result = self._writer.commit(tuple(events), expected_versions)
        apply_frame(self._state, result.frame)

        return goal_id, run_id

    def admit_event(self, event: InboxEvent) -> AdmissionDecision:
        """Accept an event into the durable inbox with dedup."""
        event_id = self._inbox.accept(event)
        if event_id is None:
            return AdmissionDecision(
                result=AdmissionResult.DUPLICATE,
                event_id=event.event_id,
                reason="Duplicate dedup key",
            )
        return AdmissionDecision(
            result=AdmissionResult.ACCEPTED,
            event_id=event_id,
        )

    def get_goal(self, goal_id: str) -> GoalDefinition | None:
        return self._state.get_goal(goal_id)

    def get_run(self, run_id: str) -> Run | None:
        return self._state.get_run(run_id)

    def list_goals(self) -> list[GoalDefinition]:
        return list(self._state.goals.values())

    def list_runs(self, goal_id: str | None = None) -> list[Run]:
        if goal_id:
            return [r for r in self._state.runs.values() if r.goal_id == goal_id]
        return list(self._state.runs.values())


# Re-export for backward compatibility
GoalInbox = DurableInbox
