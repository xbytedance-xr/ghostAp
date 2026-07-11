"""Durable control-plane activities with checkpoint, recovery, and lease binding."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..domain.ids import new_id


class ActivityState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CHECKPOINTED = "checkpointed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELED = "canceled"


class ActivityType(str, Enum):
    ADMISSION = "admission"
    PLANNING = "planning"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    RECONCILIATION = "reconciliation"
    REPORTING = "reporting"
    COMPENSATION = "compensation"


class StaleLease(Exception):
    pass


class ActivityNotFound(Exception):
    pass


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


@dataclass
class ActivityCheckpoint:
    checkpoint_id: str
    activity_id: str
    data: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    blob_ref: str = ""


@dataclass
class Activity:
    activity_id: str = field(default_factory=lambda: new_id("act"))
    activity_type: ActivityType = ActivityType.EXECUTION
    run_id: str = ""
    step_id: str = ""
    state: ActivityState = ActivityState.PENDING
    input_hash: str = ""
    attempt_number: int = 1
    lease_id: str = ""
    fencing_token: int = 0
    heartbeat_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    last_checkpoint: ActivityCheckpoint | None = None
    effect_ids: list[str] = field(default_factory=list)
    error: str = ""
    aggregate_version: int = 0


class ActivityExecutor:
    """Manages durable control-plane activity lifecycle.

    Activities have: input hash, state, attempt, lease binding, heartbeat,
    checkpoint BlobRef, and effect ledger. Reconciler can resume every
    control-plane phase.
    """

    def __init__(self, journal: JournalWriter) -> None:
        self._journal = journal
        self._activities: dict[str, Activity] = {}
        self._lease_to_activity: dict[str, str] = {}

    async def create(
        self,
        *,
        activity_type: ActivityType,
        run_id: str,
        step_id: str = "",
        input_hash: str = "",
        lease_id: str = "",
        fencing_token: int = 0,
    ) -> Activity:
        activity = Activity(
            activity_type=activity_type,
            run_id=run_id,
            step_id=step_id,
            input_hash=input_hash,
            lease_id=lease_id,
            fencing_token=fencing_token,
        )
        self._activities[activity.activity_id] = activity
        if lease_id:
            self._lease_to_activity[lease_id] = activity.activity_id

        await self._journal.write_event("activity.created", {
            "activity_id": activity.activity_id,
            "activity_type": activity_type.value,
            "run_id": run_id,
            "step_id": step_id,
            "input_hash": input_hash,
            "lease_id": lease_id,
            "fencing_token": fencing_token,
        })
        return activity

    async def start(self, activity_id: str, fencing_token: int) -> Activity:
        activity = self._get(activity_id)
        self._check_fence(activity, fencing_token)

        if activity.state not in (ActivityState.PENDING, ActivityState.CHECKPOINTED):
            raise ValueError(
                f"cannot start activity in state {activity.state.value}"
            )

        activity.state = ActivityState.RUNNING
        activity.started_at = time.time()
        activity.heartbeat_at = activity.started_at
        activity.aggregate_version += 1

        await self._journal.write_event("activity.started", {
            "activity_id": activity_id,
            "fencing_token": fencing_token,
        })
        return activity

    async def heartbeat(self, activity_id: str, fencing_token: int) -> None:
        activity = self._get(activity_id)
        self._check_fence(activity, fencing_token)
        activity.heartbeat_at = time.time()

    async def checkpoint(
        self,
        activity_id: str,
        fencing_token: int,
        data: dict[str, Any],
        blob_ref: str = "",
    ) -> ActivityCheckpoint:
        activity = self._get(activity_id)
        self._check_fence(activity, fencing_token)

        if activity.state is not ActivityState.RUNNING:
            raise ValueError("can only checkpoint a running activity")

        cp = ActivityCheckpoint(
            checkpoint_id=new_id("cp"),
            activity_id=activity_id,
            data=data,
            blob_ref=blob_ref,
        )
        activity.last_checkpoint = cp
        activity.state = ActivityState.CHECKPOINTED
        activity.aggregate_version += 1

        await self._journal.write_event("activity.checkpointed", {
            "activity_id": activity_id,
            "checkpoint_id": cp.checkpoint_id,
            "blob_ref": blob_ref,
        })
        return cp

    async def complete(
        self,
        activity_id: str,
        fencing_token: int,
        *,
        success: bool,
        error: str = "",
    ) -> Activity:
        activity = self._get(activity_id)
        self._check_fence(activity, fencing_token)

        if activity.state not in (
            ActivityState.RUNNING,
            ActivityState.CHECKPOINTED,
        ):
            raise ValueError(
                f"cannot complete activity in state {activity.state.value}"
            )

        activity.state = ActivityState.SUCCEEDED if success else ActivityState.FAILED
        activity.completed_at = time.time()
        activity.error = error
        activity.aggregate_version += 1

        await self._journal.write_event("activity.completed", {
            "activity_id": activity_id,
            "success": success,
            "error": error,
        })
        return activity

    async def cancel(self, activity_id: str) -> Activity:
        activity = self._get(activity_id)
        if activity.state in (
            ActivityState.SUCCEEDED,
            ActivityState.FAILED,
            ActivityState.CANCELED,
        ):
            raise ValueError(f"cannot cancel terminal activity")
        activity.state = ActivityState.CANCELED
        activity.completed_at = time.time()
        activity.aggregate_version += 1

        await self._journal.write_event("activity.canceled", {
            "activity_id": activity_id,
        })
        return activity

    async def timeout(self, activity_id: str) -> Activity:
        activity = self._get(activity_id)
        if activity.state in (
            ActivityState.SUCCEEDED,
            ActivityState.FAILED,
            ActivityState.CANCELED,
            ActivityState.TIMED_OUT,
        ):
            raise ValueError(f"cannot timeout terminal activity")
        activity.state = ActivityState.TIMED_OUT
        activity.completed_at = time.time()
        activity.aggregate_version += 1

        await self._journal.write_event("activity.timed_out", {
            "activity_id": activity_id,
        })
        return activity

    def get(self, activity_id: str) -> Activity | None:
        return self._activities.get(activity_id)

    def get_by_lease(self, lease_id: str) -> Activity | None:
        aid = self._lease_to_activity.get(lease_id)
        if aid:
            return self._activities.get(aid)
        return None

    def list_active(self, run_id: str) -> list[Activity]:
        return [
            a for a in self._activities.values()
            if a.run_id == run_id
            and a.state in (
                ActivityState.PENDING,
                ActivityState.RUNNING,
                ActivityState.CHECKPOINTED,
            )
        ]

    def list_resumable(self) -> list[Activity]:
        return [
            a for a in self._activities.values()
            if a.state is ActivityState.CHECKPOINTED
        ]

    def _get(self, activity_id: str) -> Activity:
        activity = self._activities.get(activity_id)
        if activity is None:
            raise ActivityNotFound(f"activity not found: {activity_id}")
        return activity

    def _check_fence(self, activity: Activity, fencing_token: int) -> None:
        if activity.fencing_token and fencing_token < activity.fencing_token:
            raise StaleLease(
                f"stale fencing token {fencing_token} < {activity.fencing_token}"
            )
