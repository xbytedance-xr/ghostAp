from __future__ import annotations

import pytest

from src.autonomous.workforce.authority import (
    AuthorityMode,
    AuthoritySnapshot,
    LegacyMutationGuard,
    StaleAuthorityEpoch,
)
from src.slock_engine.agent_registry import AgentRegistry
from src.slock_engine.models import AgentIdentity


def test_v5_cutover_rejects_legacy_registry_mutation_before_memory_change(
    tmp_path,
) -> None:
    snapshot = AuthoritySnapshot(
        epoch=2,
        mode=AuthorityMode.V5_WRITE,
        cutover_sequence=91,
    )
    guard = LegacyMutationGuard(lambda: snapshot, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)

    with pytest.raises(StaleAuthorityEpoch):
        registry.register(AgentIdentity(agent_id="legacy_1", name="Legacy"))

    assert registry.get("legacy_1") is None


def test_queued_legacy_write_is_rechecked_after_cutover(tmp_path) -> None:
    current = AuthoritySnapshot(
        epoch=1,
        mode=AuthorityMode.LEGACY_WRITE,
        cutover_sequence=0,
    )
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    request = registry._make_persist_request(
        "register",
        AgentIdentity(agent_id="legacy_1", name="Legacy"),
        validated_epoch=1,
    )
    current = AuthoritySnapshot(
        epoch=2,
        mode=AuthorityMode.V5_WRITE,
        cutover_sequence=91,
    )

    with pytest.raises(StaleAuthorityEpoch):
        registry._persist_request(request)

    assert not (tmp_path / "agents" / "legacy_1" / "identity.json").exists()


def test_shadow_read_allows_legacy_write_only_at_matching_epoch() -> None:
    current = AuthoritySnapshot(epoch=4, mode=AuthorityMode.SHADOW_READ)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=4)

    assert guard.assert_writable("update") == 4
    with pytest.raises(StaleAuthorityEpoch):
        guard.assert_writable("queued update", validated_epoch=3)
