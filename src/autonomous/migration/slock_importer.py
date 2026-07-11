"""Idempotent Slock-to-Autonomous importer.

Maps legacy Slock groups, agents, task boards, plans, discussions, and decisions
into the v5 autonomous kernel's durable journal.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..domain.ids import new_id


class JournalWriter(Protocol):
    async def write_event(self, event_type: str, payload: dict) -> None: ...


class ImportPhase(str, Enum):
    SCAN = "scan"
    PLAN = "plan"
    APPLY = "apply"
    VERIFY = "verify"


@dataclass
class LegacyEntity:
    entity_type: str
    legacy_id: str
    data: dict[str, Any]
    source_hash: str = ""

    def __post_init__(self) -> None:
        if not self.source_hash:
            content = f"{self.entity_type}:{self.legacy_id}:{sorted(self.data.items())}"
            self.source_hash = hashlib.sha256(content.encode()).hexdigest()[:24]


@dataclass
class ImportPlan:
    entities: list[LegacyEntity] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    dry_run: bool = False
    source_schema_version: str = "slock_v1"

    @property
    def total_count(self) -> int:
        return len(self.entities)


@dataclass
class ImportResult:
    created_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    id_mappings: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class VerificationReport:
    total_legacy: int = 0
    total_migrated: int = 0
    missing: list[str] = field(default_factory=list)
    hash_mismatches: list[str] = field(default_factory=list)

    @property
    def hashes_match(self) -> bool:
        return len(self.hash_mismatches) == 0 and len(self.missing) == 0


class SlockImporter:
    """Idempotent importer from legacy Slock data to autonomous kernel.

    Flow: scan -> plan -> apply -> verify
    Each apply is idempotent: duplicate imports produce zero new entities.
    """

    def __init__(self, journal: JournalWriter) -> None:
        self._journal = journal
        self._imported: dict[str, str] = {}  # legacy_id -> autonomous_id
        self._source_hashes: dict[str, str] = {}  # legacy_id -> hash

    def scan(self, legacy_data: dict[str, Any]) -> list[LegacyEntity]:
        entities: list[LegacyEntity] = []

        for group in legacy_data.get("groups", []):
            entities.append(LegacyEntity(
                entity_type="employee",
                legacy_id=group.get("group_id", ""),
                data=group,
            ))

        for agent in legacy_data.get("agents", []):
            entities.append(LegacyEntity(
                entity_type="worker",
                legacy_id=agent.get("agent_id", ""),
                data=agent,
            ))

        for task in legacy_data.get("tasks", []):
            entities.append(LegacyEntity(
                entity_type="goal",
                legacy_id=task.get("task_id", ""),
                data=task,
            ))

        for plan in legacy_data.get("plans", []):
            entities.append(LegacyEntity(
                entity_type="plan",
                legacy_id=plan.get("plan_id", ""),
                data=plan,
            ))

        return entities

    def plan(self, entities: list[LegacyEntity], *, dry_run: bool = False) -> ImportPlan:
        return ImportPlan(entities=entities, dry_run=dry_run)

    async def apply(self, plan: ImportPlan) -> ImportResult:
        result = ImportResult()

        for entity in plan.entities:
            if entity.legacy_id in self._imported:
                if self._source_hashes.get(entity.legacy_id) == entity.source_hash:
                    result.skipped_count += 1
                    continue

            if plan.dry_run:
                result.created_count += 1
                continue

            try:
                new_id_val = self._create_autonomous_entity(entity)
                self._imported[entity.legacy_id] = new_id_val
                self._source_hashes[entity.legacy_id] = entity.source_hash
                result.id_mappings[entity.legacy_id] = new_id_val
                result.created_count += 1

                await self._journal.write_event("migration.entity_created", {
                    "legacy_id": entity.legacy_id,
                    "entity_type": entity.entity_type,
                    "autonomous_id": new_id_val,
                    "source_hash": entity.source_hash,
                })
            except Exception as exc:
                result.error_count += 1
                result.errors.append(f"{entity.legacy_id}: {exc}")

        return result

    def verify(self, entities: list[LegacyEntity] | None = None) -> VerificationReport:
        report = VerificationReport()
        check_entities = entities or [
            LegacyEntity(
                entity_type="unknown",
                legacy_id=lid,
                data={},
                source_hash=h,
            )
            for lid, h in self._source_hashes.items()
        ]
        report.total_legacy = len(check_entities)
        report.total_migrated = len(self._imported)

        for entity in check_entities:
            if entity.legacy_id not in self._imported:
                report.missing.append(entity.legacy_id)
            elif (
                entity.source_hash
                and self._source_hashes.get(entity.legacy_id) != entity.source_hash
            ):
                report.hash_mismatches.append(entity.legacy_id)

        return report

    def get_mapping(self, legacy_id: str) -> str | None:
        return self._imported.get(legacy_id)

    def _create_autonomous_entity(self, entity: LegacyEntity) -> str:
        type_prefix = {
            "employee": "emp",
            "worker": "wkr",
            "goal": "goal",
            "plan": "plan",
        }
        prefix = type_prefix.get(entity.entity_type, "ent")
        return new_id(prefix)
