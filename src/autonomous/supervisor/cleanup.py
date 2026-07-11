"""Cleanup - narrow scope cleanup execution for completed runs.

Only operates on explicitly permitted cleanup targets.
Archives old completed runs/plans after configurable retention.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol

from ..domain import RunState, new_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Journal protocol
# ---------------------------------------------------------------------------


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


# ---------------------------------------------------------------------------
# Cleanup types
# ---------------------------------------------------------------------------


class CleanupTargetType(Enum):
    """Types of entities that can be cleaned up."""

    COMPLETED_RUN = "completed_run"
    EXPIRED_PLAN = "expired_plan"
    DEAD_LETTER = "dead_letter"
    STALE_LEASE = "stale_lease"
    ORPHAN_ARTIFACT = "orphan_artifact"


@dataclass
class CleanupTarget:
    """A specific entity targeted for cleanup."""

    target_id: str = field(default_factory=lambda: new_id("cln"))
    entity_id: str = ""
    target_type: CleanupTargetType = CleanupTargetType.COMPLETED_RUN
    reason: str = ""
    age_seconds: float = 0.0
    safe_to_delete: bool = False

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "entity_id": self.entity_id,
            "target_type": self.target_type.value,
            "reason": self.reason,
            "age_seconds": self.age_seconds,
            "safe_to_delete": self.safe_to_delete,
        }


@dataclass
class CleanupResult:
    """Result of a cleanup execution."""

    targets_scanned: int = 0
    targets_archived: int = 0
    targets_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "targets_scanned": self.targets_scanned,
            "targets_archived": self.targets_archived,
            "targets_skipped": self.targets_skipped,
            "errors": self.errors,
            "timestamp": self.timestamp,
        }


@dataclass
class CleanupConfig:
    """Configuration for cleanup retention policies."""

    completed_run_retention_seconds: float = 7 * 24 * 3600  # 7 days
    expired_plan_retention_seconds: float = 3 * 24 * 3600  # 3 days
    dead_letter_retention_seconds: float = 14 * 24 * 3600  # 14 days
    max_cleanup_batch_size: int = 50
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Cleanup engine
# ---------------------------------------------------------------------------


class Cleanup:
    """Narrow cleanup execution for completed/expired entities.

    Only operates on explicitly permitted cleanup targets.
    Journal-backed for auditability.
    """

    def __init__(
        self,
        journal: Optional[JournalWriter] = None,
        config: Optional[CleanupConfig] = None,
    ):
        self._journal = journal
        self._config = config or CleanupConfig()
        self._archive: list[CleanupTarget] = []

    async def _journal_event(self, event_type: str, payload: dict) -> None:
        if self._journal:
            await self._journal.write_event(event_type, payload)

    async def identify_cleanup_targets(
        self,
        completed_runs: list[dict],
        dead_letters: list[dict],
    ) -> list[CleanupTarget]:
        """Identify entities eligible for cleanup based on retention policy."""
        now = time.time()
        targets: list[CleanupTarget] = []

        for run in completed_runs:
            run_state = run.get("state", "")
            if run_state not in ("succeeded", "failed", "canceled"):
                continue
            completed_at = run.get("completed_at", run.get("created_at", now))
            age = now - completed_at
            if age >= self._config.completed_run_retention_seconds:
                targets.append(CleanupTarget(
                    entity_id=run.get("run_id", ""),
                    target_type=CleanupTargetType.COMPLETED_RUN,
                    reason=f"Completed {age:.0f}s ago, exceeds retention",
                    age_seconds=age,
                    safe_to_delete=True,
                ))

        for dl in dead_letters:
            moved_at = dl.get("moved_at", now)
            age = now - moved_at
            if age >= self._config.dead_letter_retention_seconds:
                targets.append(CleanupTarget(
                    entity_id=dl.get("step_id", ""),
                    target_type=CleanupTargetType.DEAD_LETTER,
                    reason=f"Dead-lettered {age:.0f}s ago, exceeds retention",
                    age_seconds=age,
                    safe_to_delete=True,
                ))

        # Limit batch size
        return targets[: self._config.max_cleanup_batch_size]

    async def execute_cleanup(self, targets: list[CleanupTarget]) -> CleanupResult:
        """Execute cleanup on identified targets. Only acts on safe_to_delete targets."""
        result = CleanupResult(targets_scanned=len(targets))

        await self._journal_event("cleanup.batch_start", {
            "targets_count": len(targets),
            "dry_run": self._config.dry_run,
        })

        for target in targets:
            if not target.safe_to_delete:
                result.targets_skipped += 1
                continue

            if self._config.dry_run:
                result.targets_skipped += 1
                continue

            try:
                self._archive.append(target)
                result.targets_archived += 1
                await self._journal_event("cleanup.target_archived", {
                    "target_id": target.target_id,
                    "entity_id": target.entity_id,
                    "target_type": target.target_type.value,
                })
            except Exception as exc:
                result.errors.append(f"Failed to archive {target.entity_id}: {str(exc)}")
                result.targets_skipped += 1

        await self._journal_event("cleanup.batch_complete", result.to_dict())
        return result

    def get_archived(self) -> list[CleanupTarget]:
        """Return all archived cleanup targets."""
        return list(self._archive)
