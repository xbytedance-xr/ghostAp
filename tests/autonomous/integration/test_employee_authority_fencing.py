from __future__ import annotations

import threading
from unittest.mock import MagicMock

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


def test_cutover_discards_blocked_worker_write_and_ghost_memory(tmp_path) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="Legacy"))

    worker_blocked = threading.Event()
    release_worker = threading.Event()
    original_persist_request = registry._persist_request

    def blocked_persist(request) -> None:
        worker_blocked.set()
        assert release_worker.wait(timeout=2)
        original_persist_request(request)

    registry._persist_request = blocked_persist  # type: ignore[method-assign]
    worker = threading.Thread(target=registry._flush_persist_queue)
    worker.start()
    assert worker_blocked.wait(timeout=2)

    def advance() -> AuthoritySnapshot:
        nonlocal current
        current = AuthoritySnapshot(
            epoch=2,
            mode=AuthorityMode.V5_WRITE,
            cutover_sequence=91,
        )
        return current

    registry.cutover_authority(advance)
    assert registry.get("legacy_1") is None
    release_worker.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert not (tmp_path / "agents" / "legacy_1" / "identity.json").exists()


def test_cutover_rejects_remove_and_move_without_memory_or_disk_change(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    agent = registry.register(
        AgentIdentity(
            agent_id="legacy_1",
            name="Legacy",
            owner_group="oc_source",
        )
    )
    if registry._persist_thread:
        registry._persist_thread.join(timeout=2)
    identity_file = tmp_path / "agents" / "legacy_1" / "identity.json"
    before = identity_file.read_bytes()

    def advance() -> AuthoritySnapshot:
        nonlocal current
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_ONLY)
        return current

    registry.cutover_authority(advance)

    with pytest.raises(StaleAuthorityEpoch):
        registry.remove(agent.agent_id)
    with pytest.raises(StaleAuthorityEpoch):
        registry.move_agent(agent.agent_id, "oc_source", "oc_target")

    projected = registry.get(agent.agent_id)
    assert projected is not None
    assert projected.owner_group == "oc_source"
    assert projected.member_groups == ["oc_source"]
    assert identity_file.read_bytes() == before
