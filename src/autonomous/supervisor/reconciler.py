"""Reconciler - handles orphan attempts, unknown effects, outbox, and trigger state.

Responsible for: expired lease reclamation, unknown effect resolution,
undelivered outbox retry, stale trigger cursor advancement,
misfire detection, and ensuring eventual consistency after crashes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional, Protocol

from ..domain import (
    Effect,
    EffectState,
    new_id,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


class SchedulerProtocol(Protocol):
    async def check_expired_leases(self) -> list: ...
    async def release_lease(self, lease_id: str, success: bool) -> None: ...


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class ReconciliationActionType(Enum):
    RECLAIM_LEASE = "reclaim_lease"
    RETRY_STEP = "retry_step"
    MARK_UNKNOWN = "mark_unknown"
    DEAD_LETTER = "dead_letter"
    COMPENSATE = "compensate"
    RETRY_OUTBOX = "retry_outbox"
    ADVANCE_CURSOR = "advance_cursor"
    COMMIT_EFFECT = "commit_effect"
    FAIL_EFFECT = "fail_effect"


@dataclass
class ReconciliationAction:
    action_id: str = field(default_factory=lambda: new_id("ract"))
    action_type: str = ""
    entity_id: str = ""
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    resolved: bool = False

    def to_dict(self) -> dict:
        return {
            "action_id": self.action_id,
            "action_type": self.action_type,
            "entity_id": self.entity_id,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "resolved": self.resolved,
        }


@dataclass
class ReconciliationReport:
    """Summary of a reconciliation pass."""

    lease_actions: int = 0
    effect_actions: int = 0
    outbox_actions: int = 0
    trigger_actions: int = 0
    orphan_actions: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "lease_actions": self.lease_actions,
            "effect_actions": self.effect_actions,
            "outbox_actions": self.outbox_actions,
            "trigger_actions": self.trigger_actions,
            "orphan_actions": self.orphan_actions,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class Reconciler:
    """Detects and resolves inconsistencies after crashes/timeouts.

    Reconciles: leases, effects, outbox, triggers, orphan attempts.
    All actions are journal-backed.
    """

    def __init__(
        self,
        journal: JournalWriter,
        scheduler: SchedulerProtocol,
        effect_age_threshold: float = 3600.0,
        outbox_retry_threshold: float = 300.0,
        trigger_stale_threshold: float = 1800.0,
    ):
        self._journal = journal
        self._scheduler = scheduler
        self._effect_age_threshold = effect_age_threshold
        self._outbox_retry_threshold = outbox_retry_threshold
        self._trigger_stale_threshold = trigger_stale_threshold
        self._actions: list[ReconciliationAction] = []

    async def reconcile_leases(self) -> list[ReconciliationAction]:
        """Expired leases -> retry or dead-letter."""
        expired = await self._scheduler.check_expired_leases()
        actions: list[ReconciliationAction] = []

        for lease in expired:
            action = ReconciliationAction(
                action_type=ReconciliationActionType.RECLAIM_LEASE.value,
                entity_id=lease.lease_id,
                reason=f"Lease expired for step {lease.step_id}",
            )
            actions.append(action)
            await self._scheduler.release_lease(lease.lease_id, success=False)

        if actions:
            await self._journal.write_event("reconciler.leases_reclaimed", {
                "count": len(actions),
                "lease_ids": [a.entity_id for a in actions],
            })

        self._actions.extend(actions)
        return actions

    async def reconcile_effects(self, effects: list[Effect]) -> list[ReconciliationAction]:
        """UNKNOWN effects -> query remote -> commit/fail.

        Note: Effect domain objects are frozen/immutable. This method only
        records actions; callers must apply state transitions via the journal.
        """
        actions: list[ReconciliationAction] = []
        now = time.time()

        for effect in effects:
            if effect.state != EffectState.UNKNOWN_EFFECT:
                continue

            age = now - effect.created_at
            if age <= self._effect_age_threshold:
                continue

            # After threshold, record action for manual reconciliation
            action = ReconciliationAction(
                action_type=ReconciliationActionType.MARK_UNKNOWN.value,
                entity_id=effect.effect_id,
                reason=f"Effect unknown for {age:.0f}s, exceeds threshold of {self._effect_age_threshold}s",
            )
            actions.append(action)

        if actions:
            await self._journal.write_event("reconciler.effects_reconciled", {
                "count": len(actions),
                "effect_ids": [a.entity_id for a in actions],
            })

        self._actions.extend(actions)
        return actions

    async def reconcile_outbox(self, outbox_entries: list[dict]) -> list[ReconciliationAction]:
        """Undelivered reports -> retry."""
        actions: list[ReconciliationAction] = []
        now = time.time()

        for entry in outbox_entries:
            state = entry.get("state", "")
            if state not in ("pending", "failed"):
                continue

            created_at = entry.get("created_at", now)
            age = now - created_at
            if age < self._outbox_retry_threshold:
                continue

            # Check if still retryable
            attempt_count = entry.get("attempt_count", 0)
            max_attempts = entry.get("max_attempts", 5)
            if attempt_count >= max_attempts:
                action = ReconciliationAction(
                    action_type=ReconciliationActionType.DEAD_LETTER.value,
                    entity_id=entry.get("entry_id", ""),
                    reason=f"Outbox entry exhausted {max_attempts} attempts",
                )
            else:
                action = ReconciliationAction(
                    action_type=ReconciliationActionType.RETRY_OUTBOX.value,
                    entity_id=entry.get("entry_id", ""),
                    reason=f"Outbox entry pending for {age:.0f}s, scheduling retry",
                )
            actions.append(action)

        if actions:
            await self._journal.write_event("reconciler.outbox_reconciled", {
                "count": len(actions),
                "entry_ids": [a.entity_id for a in actions],
            })

        self._actions.extend(actions)
        return actions

    async def reconcile_triggers(self, triggers: list[dict]) -> list[ReconciliationAction]:
        """Stale trigger cursors -> advance."""
        actions: list[ReconciliationAction] = []
        now = time.time()

        for trigger in triggers:
            if not trigger.get("active", False):
                continue

            cursor = trigger.get("cursor", {})
            last_success = cursor.get("last_success_at", 0)
            if last_success == 0:
                continue

            staleness = now - last_success
            if staleness <= self._trigger_stale_threshold:
                continue

            action = ReconciliationAction(
                action_type=ReconciliationActionType.ADVANCE_CURSOR.value,
                entity_id=trigger.get("subscription_id", ""),
                reason=f"Trigger cursor stale for {staleness:.0f}s, advancing",
            )
            actions.append(action)

        if actions:
            await self._journal.write_event("reconciler.triggers_reconciled", {
                "count": len(actions),
                "subscription_ids": [a.entity_id for a in actions],
            })

        self._actions.extend(actions)
        return actions

    async def reconcile_orphan_attempts(
        self, attempts: list[dict], active_workers: set[str]
    ) -> list[ReconciliationAction]:
        """Find attempts whose worker is dead."""
        actions: list[ReconciliationAction] = []

        for att in attempts:
            if att.get("state") != "active":
                continue
            if att.get("worker_id") and att["worker_id"] not in active_workers:
                action = ReconciliationAction(
                    action_type=ReconciliationActionType.RETRY_STEP.value,
                    entity_id=att["attempt_id"],
                    reason=f"Worker {att['worker_id']} is dead",
                )
                actions.append(action)

        if actions:
            await self._journal.write_event("reconciler.orphans_found", {
                "count": len(actions),
                "attempt_ids": [a.entity_id for a in actions],
            })

        self._actions.extend(actions)
        return actions

    async def full_reconciliation(
        self,
        effects: Optional[list[Effect]] = None,
        outbox_entries: Optional[list[dict]] = None,
        triggers: Optional[list[dict]] = None,
        attempts: Optional[list[dict]] = None,
        active_workers: Optional[set[str]] = None,
    ) -> ReconciliationReport:
        """Run all reconciliation checks and return a summary report."""
        report = ReconciliationReport()

        await self._journal.write_event("reconciler.full_start", {})

        lease_actions = await self.reconcile_leases()
        report.lease_actions = len(lease_actions)

        if effects is not None:
            effect_actions = await self.reconcile_effects(effects)
            report.effect_actions = len(effect_actions)

        if outbox_entries is not None:
            outbox_actions = await self.reconcile_outbox(outbox_entries)
            report.outbox_actions = len(outbox_actions)

        if triggers is not None:
            trigger_actions = await self.reconcile_triggers(triggers)
            report.trigger_actions = len(trigger_actions)

        if attempts is not None and active_workers is not None:
            orphan_actions = await self.reconcile_orphan_attempts(attempts, active_workers)
            report.orphan_actions = len(orphan_actions)

        await self._journal.write_event("reconciler.full_complete", report.to_dict())
        return report

    def get_recent_actions(self, limit: int = 50) -> list[ReconciliationAction]:
        return self._actions[-limit:]

    def get_action_count(self) -> int:
        return len(self._actions)
