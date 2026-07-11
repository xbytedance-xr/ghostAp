"""Integration tests for SlockImporter idempotency and migration."""

from __future__ import annotations

import pytest

from src.autonomous.migration.slock_importer import (
    ImportPlan,
    ImportResult,
    LegacyEntity,
    SlockImporter,
    VerificationReport,
)


class FakeJournal:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def write_event(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


@pytest.fixture
def importer() -> SlockImporter:
    return SlockImporter(journal=FakeJournal())


@pytest.fixture
def legacy_data() -> dict:
    return {
        "groups": [
            {"group_id": "grp_1", "name": "Development", "role": "coder"},
            {"group_id": "grp_2", "name": "Review", "role": "reviewer"},
        ],
        "agents": [
            {"agent_id": "agent_1", "tool": "codex", "model": "gpt-4o"},
        ],
        "tasks": [
            {"task_id": "task_1", "description": "implement feature X"},
            {"task_id": "task_2", "description": "review PR"},
        ],
        "plans": [
            {"plan_id": "plan_1", "steps": ["code", "test", "review"]},
        ],
    }


@pytest.mark.asyncio
async def test_import_twice_creates_one_set_of_entities(
    importer: SlockImporter, legacy_data: dict
) -> None:
    entities = importer.scan(legacy_data)
    plan = importer.plan(entities)

    first = await importer.apply(plan)
    assert first.created_count == 6  # 2 groups + 1 agent + 2 tasks + 1 plan

    second = await importer.apply(plan)
    assert second.created_count == 0
    assert second.skipped_count == 6


@pytest.mark.asyncio
async def test_verify_after_import_matches(
    importer: SlockImporter, legacy_data: dict
) -> None:
    entities = importer.scan(legacy_data)
    plan = importer.plan(entities)
    await importer.apply(plan)

    report = importer.verify(entities)
    assert report.hashes_match
    assert report.total_legacy == 6
    assert report.total_migrated == 6


@pytest.mark.asyncio
async def test_verify_detects_missing_entities(importer: SlockImporter) -> None:
    report = importer.verify([
        LegacyEntity(entity_type="goal", legacy_id="missing_1", data={}),
    ])
    assert not report.hashes_match
    assert "missing_1" in report.missing


@pytest.mark.asyncio
async def test_dry_run_does_not_persist(
    importer: SlockImporter, legacy_data: dict
) -> None:
    entities = importer.scan(legacy_data)
    plan = importer.plan(entities, dry_run=True)
    result = await importer.apply(plan)

    assert result.created_count == 6
    assert importer.get_mapping("grp_1") is None  # not persisted


@pytest.mark.asyncio
async def test_id_mappings_returned(
    importer: SlockImporter, legacy_data: dict
) -> None:
    entities = importer.scan(legacy_data)
    plan = importer.plan(entities)
    result = await importer.apply(plan)

    assert "grp_1" in result.id_mappings
    assert result.id_mappings["grp_1"].startswith("emp_")
    assert "task_1" in result.id_mappings
    assert result.id_mappings["task_1"].startswith("goal_")


@pytest.mark.asyncio
async def test_scan_produces_source_hashes(legacy_data: dict) -> None:
    importer = SlockImporter(journal=FakeJournal())
    entities = importer.scan(legacy_data)

    for entity in entities:
        assert entity.source_hash
        assert len(entity.source_hash) == 24
