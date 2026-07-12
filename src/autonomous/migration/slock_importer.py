"""Idempotent Slock-to-Autonomous importer."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from ..domain import EmployeeDefinition
from ..domain.ids import new_id
from ..journal.frame import JournalEvent
from ..journal.projections import ProjectionState
from ..journal.writer import JournalWriter
from ..workforce.projection import (
    commit_workforce_events,
    validate_workforce_events,
)


class LegacyEventJournal(Protocol):
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
    """Import legacy data through either compatibility or authenticated mode."""

    def __init__(
        self,
        journal: LegacyEventJournal | None = None,
        *,
        writer: JournalWriter | None = None,
        state: ProjectionState | None = None,
    ) -> None:
        legacy_mode = journal is not None
        authenticated_mode = writer is not None and state is not None
        incomplete_authenticated_mode = (writer is None) != (state is None)
        if (
            incomplete_authenticated_mode
            or legacy_mode == authenticated_mode
        ):
            raise ValueError(
                "configure exactly one importer mode: journal or writer+state"
            )
        self._journal = journal
        self._writer = writer
        self._state = state
        self._imported: dict[str, str] = {}
        self._source_hashes: dict[str, str] = {}

    def scan(self, legacy_data: dict[str, Any]) -> list[LegacyEntity]:
        entities: list[LegacyEntity] = []
        for group in legacy_data.get("groups", []):
            entities.append(
                LegacyEntity("employee", group.get("group_id", ""), group)
            )
        for agent in legacy_data.get("agents", []):
            entities.append(
                LegacyEntity("agent", agent.get("agent_id", ""), agent)
            )
        for task in legacy_data.get("tasks", []):
            entities.append(
                LegacyEntity("goal", task.get("task_id", ""), task)
            )
        for plan in legacy_data.get("plans", []):
            entities.append(
                LegacyEntity("plan", plan.get("plan_id", ""), plan)
            )
        return entities

    def plan(
        self,
        entities: list[LegacyEntity],
        *,
        dry_run: bool = False,
    ) -> ImportPlan:
        canonical = [self._canonicalize_entity(entity) for entity in entities]
        return ImportPlan(entities=canonical, dry_run=dry_run)

    def import_agent(self, entity: LegacyEntity) -> EmployeeDefinition:
        if entity.entity_type != "agent":
            raise ValueError("import_agent requires canonical agent entity type")
        if self._writer is None or self._state is None:
            raise ValueError("authenticated writer+state mode is required")

        alias_owner = self._state.legacy_agent_aliases.get(entity.legacy_id)
        hash_owner = self._state.legacy_source_hashes.get(entity.source_hash)
        if alias_owner is not None:
            if hash_owner != alias_owner:
                raise ValueError("legacy agent alias source hash changed")
            return self._state.employees[alias_owner]
        if hash_owner is not None:
            raise ValueError("legacy source hash already belongs to another alias")

        agent_id = new_id("agt")
        data = entity.data
        created = JournalEvent(
            event_type="employee.created",
            aggregate_id=agent_id,
            payload={
                "agent_id": agent_id,
                "tenant_key": str(data.get("tenant_key", "")),
                "owner_principal_id": str(
                    data.get("owner_principal_id", "")
                ),
                "name": str(data.get("name", "")),
                "emoji": str(data.get("emoji", "🤖")),
                "tool": str(data.get("agent_type", data.get("tool", ""))),
                "model": str(data.get("model_name", data.get("model", ""))),
                "persona": str(
                    data.get("system_prompt", data.get("persona", ""))
                ),
                "role": str(data.get("role", "")),
                "permissions": list(data.get("permissions", ())),
                "personality_traits": list(
                    data.get("personality_traits", ())
                ),
                "worker_type": "logical",
                "state": "draft",
                "member_groups": list(
                    data.get("member_groups")
                    or ([data["owner_group"]] if data.get("owner_group") else [])
                ),
            },
        )
        alias_bound = JournalEvent(
            event_type="employee.legacy_alias_bound",
            aggregate_id=agent_id,
            payload={
                "legacy_id_alias": entity.legacy_id,
                "source_hash": entity.source_hash,
            },
        )
        events = (created, alias_bound)
        validate_workforce_events(self._state, events)
        commit_workforce_events(self._writer, self._state, events)
        return self._state.employees[agent_id]

    async def apply(self, plan: ImportPlan) -> ImportResult:
        result = ImportResult()
        for raw_entity in plan.entities:
            entity = self._canonicalize_entity(raw_entity)
            durable_id = self._durable_mapping(entity)
            if durable_id is not None:
                result.skipped_count += 1
                result.id_mappings[entity.legacy_id] = durable_id
                continue
            if entity.legacy_id in self._imported:
                if self._source_hashes.get(entity.legacy_id) == entity.source_hash:
                    result.skipped_count += 1
                    continue
            if plan.dry_run:
                result.created_count += 1
                continue
            try:
                if entity.entity_type == "agent" and self._writer is not None:
                    new_id_val = self.import_agent(entity).agent_id
                else:
                    new_id_val = self._create_autonomous_entity(entity)
                    if self._journal is None:
                        raise ValueError(
                            "authenticated mode only supports durable agent imports"
                        )
                    await self._journal.write_event(
                        "migration.entity_created",
                        {
                            "legacy_id": entity.legacy_id,
                            "entity_type": entity.entity_type,
                            "autonomous_id": new_id_val,
                            "source_hash": entity.source_hash,
                        },
                    )
                self._imported[entity.legacy_id] = new_id_val
                self._source_hashes[entity.legacy_id] = entity.source_hash
                result.id_mappings[entity.legacy_id] = new_id_val
                result.created_count += 1
            except Exception as exc:
                result.error_count += 1
                result.errors.append(f"{entity.legacy_id}: {exc}")
        return result

    def verify(
        self,
        entities: list[LegacyEntity] | None = None,
    ) -> VerificationReport:
        report = VerificationReport()
        check_entities = entities or [
            LegacyEntity("unknown", legacy_id, {}, source_hash=source_hash)
            for legacy_id, source_hash in self._source_hashes.items()
        ]
        report.total_legacy = len(check_entities)
        durable_count = (
            len(self._state.legacy_agent_aliases)
            if self._state is not None
            else 0
        )
        report.total_migrated = len(self._imported) + durable_count
        for raw_entity in check_entities:
            entity = self._canonicalize_entity(raw_entity)
            mapped = self._durable_mapping(entity) or self._imported.get(
                entity.legacy_id
            )
            if mapped is None:
                report.missing.append(entity.legacy_id)
            elif (
                entity.source_hash
                and self._source_hash(entity.legacy_id) != entity.source_hash
            ):
                report.hash_mismatches.append(entity.legacy_id)
        return report

    def get_mapping(self, legacy_id: str) -> str | None:
        if self._state is not None:
            durable = self._state.legacy_agent_aliases.get(legacy_id)
            if durable is not None:
                return durable
        return self._imported.get(legacy_id)

    def _source_hash(self, legacy_id: str) -> str | None:
        if self._state is not None:
            agent_id = self._state.legacy_agent_aliases.get(legacy_id)
            if agent_id is not None:
                for source_hash, owner in self._state.legacy_source_hashes.items():
                    if owner == agent_id:
                        return source_hash
        return self._source_hashes.get(legacy_id)

    def _durable_mapping(self, entity: LegacyEntity) -> str | None:
        if self._state is None or entity.entity_type != "agent":
            return None
        agent_id = self._state.legacy_agent_aliases.get(entity.legacy_id)
        if agent_id is None:
            return None
        if self._state.legacy_source_hashes.get(entity.source_hash) != agent_id:
            return None
        return agent_id

    @staticmethod
    def _canonicalize_entity(entity: LegacyEntity) -> LegacyEntity:
        if entity.entity_type != "worker":
            return entity
        return LegacyEntity(
            entity_type="agent",
            legacy_id=entity.legacy_id,
            data=entity.data,
        )

    @staticmethod
    def _create_autonomous_entity(entity: LegacyEntity) -> str:
        prefix = {
            "employee": "emp",
            "agent": "agt",
            "goal": "goal",
            "plan": "plan",
        }.get(entity.entity_type, "ent")
        return new_id(prefix)
