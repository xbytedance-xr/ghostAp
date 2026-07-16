from __future__ import annotations

import json
import multiprocessing
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.autonomous.journal.frame import JournalEvent
from src.autonomous.workforce.authority import (
    AuthorityMode,
    AuthoritySnapshot,
    LegacyMutationGuard,
    StaleAuthorityEpoch,
)
from src.slock_engine.agent_registry import (
    AgentRegistry,
    DuplicateAgentNameError,
    MoveResult,
)
from src.slock_engine.models import AgentIdentity
from tests.autonomous.workforce_helpers import (
    commit_events,
    make_writer,
    replay_state,
)


def _register_without_flushing(
    base_path: str,
    agent_id: str,
    name: str,
    start,
    results,
) -> None:
    """Process helper proving the durable admission protocol, not RAM locking."""

    snapshot = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        base_path,
        mutation_guard=LegacyMutationGuard(lambda: snapshot, expected_epoch=1),
    )
    registry._persist_thread = SimpleNamespace(is_alive=lambda: True)
    start.wait(timeout=5)
    try:
        registry.register(
            AgentIdentity(
                agent_id=agent_id,
                name=name,
                owner_group="oc_large_team",
            )
        )
    except DuplicateAgentNameError:
        results.put("duplicate")
    else:
        results.put("accepted")


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


def test_registry_cutover_uses_replayable_projection_snapshot_provider(
    tmp_path,
) -> None:
    writer = make_writer(tmp_path)
    state = replay_state(writer)
    guard = LegacyMutationGuard(state.authority_snapshot, expected_epoch=0)
    registry = AgentRegistry(str(tmp_path / "legacy"), mutation_guard=guard)
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="Accepted"))

    def durable_advance() -> AuthoritySnapshot:
        commit_events(
            writer,
            state,
            JournalEvent(
                event_type="authority.cutover",
                aggregate_id="workforce_authority",
                payload={
                    "authority_epoch": 1,
                    "authority_mode": "v5_write",
                    "cutover_sequence": 41,
                },
            ),
        )
        return state.authority_snapshot()

    result = registry.cutover_authority(durable_advance)

    assert result == AuthoritySnapshot(1, AuthorityMode.V5_WRITE, 41)
    assert replay_state(writer).authority_snapshot() == result
    assert (
        tmp_path / "legacy" / "agents" / "legacy_1" / "identity.json"
    ).exists()


def test_cutover_flushes_accepted_blocked_worker_write_before_advancing(tmp_path) -> None:
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
    assert registry.get("legacy_1") is not None
    assert (tmp_path / "agents" / "legacy_1" / "identity.json").exists()
    release_worker.set()
    worker.join(timeout=2)

    assert not worker.is_alive()


def test_cutover_advance_failure_preserves_queue_memory_and_old_authority(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="Accepted"))

    def fail_advance() -> AuthoritySnapshot:
        raise RuntimeError("journal unavailable")

    with pytest.raises(RuntimeError, match="journal unavailable"):
        registry.cutover_authority(fail_advance)

    assert current.epoch == 1
    assert registry.get("legacy_1") is not None
    assert len(registry._persist_queue) == 1
    assert (tmp_path / "agents" / "legacy_1" / "identity.json").exists()


@pytest.mark.parametrize("failure", ["unpublished", "non_increasing"])
def test_cutover_validation_failure_preserves_accepted_queue(
    tmp_path,
    failure: str,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="Accepted"))

    def invalid_advance() -> AuthoritySnapshot:
        if failure == "unpublished":
            return AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    error = RuntimeError if failure == "unpublished" else ValueError
    with pytest.raises(error):
        registry.cutover_authority(invalid_advance)

    assert current == AuthoritySnapshot(1, AuthorityMode.LEGACY_WRITE, 0)
    assert registry.get("legacy_1") is not None
    assert len(registry._persist_queue) == 1
    assert registry._inflight_requests == []
    assert registry._admission_open is True
    assert (tmp_path / "agents" / "legacy_1" / "identity.json").exists()


def test_cutover_flush_failure_preserves_queue_memory_and_old_authority(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="Accepted"))
    advanced = False

    def advance() -> AuthoritySnapshot:
        nonlocal advanced, current
        advanced = True
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    with patch.object(
        registry,
        "_write_agent_to_disk",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError, match="disk full"):
            registry.cutover_authority(advance)

    assert not advanced
    assert current.epoch == 1
    assert registry.get("legacy_1") is not None
    assert len(registry._persist_queue) == 1
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


def test_cutover_waits_for_worker_holding_write_lease(tmp_path) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="Legacy"))
    write_entered = threading.Event()
    release_write = threading.Event()
    cutover_done = threading.Event()
    original_write = registry._write_agent_to_disk

    def blocked_write(agent: AgentIdentity) -> None:
        write_entered.set()
        assert release_write.wait(timeout=2)
        original_write(agent)

    registry._write_agent_to_disk = blocked_write  # type: ignore[method-assign]
    worker = threading.Thread(target=registry._flush_persist_queue)
    worker.start()
    assert write_entered.wait(timeout=2)

    def advance() -> AuthoritySnapshot:
        nonlocal current
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    def cutover() -> None:
        registry.cutover_authority(advance)
        cutover_done.set()

    cutover_thread = threading.Thread(target=cutover)
    cutover_thread.start()
    assert not cutover_done.wait(timeout=0.05)
    assert current.epoch == 1

    release_write.set()
    worker.join(timeout=2)
    cutover_thread.join(timeout=2)

    assert cutover_done.is_set()
    assert current.epoch == 2
    assert (tmp_path / "agents" / "legacy_1" / "identity.json").exists()


def test_filesystem_failures_restore_register_update_remove_and_move(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    registry.MAX_PERSIST_QUEUE_SIZE = 0
    original = AgentIdentity(
        agent_id="legacy_1",
        name="Original",
        owner_group="oc_source",
    )

    with patch.object(
        registry,
        "_write_agent_to_disk",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError, match="disk full"):
            registry.register(original)
    assert registry.get("legacy_1") is None

    registry.MAX_PERSIST_QUEUE_SIZE = 256
    registry.register(original)
    if registry._persist_thread:
        registry._persist_thread.join(timeout=2)
    identity_file = tmp_path / "agents" / "legacy_1" / "identity.json"
    before = identity_file.read_bytes()
    registry.MAX_PERSIST_QUEUE_SIZE = 0

    updated = AgentIdentity(
        agent_id="legacy_1",
        name="Updated",
        owner_group="oc_source",
    )
    with patch.object(
        registry,
        "_write_agent_to_disk",
        side_effect=OSError("disk full"),
    ):
        with pytest.raises(OSError, match="disk full"):
            registry.update(updated)
    assert registry.get("legacy_1").name == "Original"
    assert identity_file.read_bytes() == before

    with patch("src.slock_engine.agent_registry.os.remove", side_effect=OSError):
        assert registry.remove("legacy_1") is False
    assert registry.get("legacy_1") is not None
    assert identity_file.read_bytes() == before

    with patch.object(
        registry,
        "_write_agent_to_disk",
        side_effect=OSError("disk full"),
    ):
        outcome = registry.move_agent("legacy_1", "oc_source", "oc_target")
    assert outcome.status is MoveResult.PERSIST_FAILED
    restored = registry.get("legacy_1")
    assert restored.owner_group == "oc_source"
    assert restored.member_groups == ["oc_source"]
    assert identity_file.read_bytes() == before

    def advance() -> AuthoritySnapshot:
        nonlocal current
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    registry.cutover_authority(advance)
    after_cutover = registry.get("legacy_1")
    assert after_cutover is not None
    assert after_cutover.name == "Original"
    assert after_cutover.owner_group == "oc_source"
    assert identity_file.read_bytes() == before


def test_background_write_failure_reconciles_before_cutover(tmp_path) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    guard = LegacyMutationGuard(lambda: current, expected_epoch=1)
    registry = AgentRegistry(str(tmp_path), mutation_guard=guard)
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="Ghost"))

    with patch.object(
        registry,
        "_write_agent_to_disk",
        side_effect=OSError("disk full"),
    ):
        registry._flush_persist_queue()

    assert registry.get("legacy_1") is None
    assert registry._inflight_requests == []

    def advance() -> AuthoritySnapshot:
        nonlocal current
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_ONLY)
        return current

    registry.cutover_authority(advance)
    assert registry.get("legacy_1") is None
    assert not (tmp_path / "agents" / "legacy_1" / "identity.json").exists()


def test_failed_earlier_write_keeps_later_accepted_value_in_cache_and_disk(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="r1"))
    registry.update(AgentIdentity(agent_id="legacy_1", name="r2"))
    original_write = registry._write_agent_to_disk
    writes = 0

    def fail_first_write(agent: AgentIdentity) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("r1 failed")
        original_write(agent)

    registry._write_agent_to_disk = fail_first_write  # type: ignore[method-assign]
    registry._flush_persist_queue()

    assert registry.get("legacy_1").name == "r2"
    persisted = json.loads(
        (tmp_path / "agents" / "legacy_1" / "identity.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["name"] == "r2"


def test_discard_and_reconcile_blocks_concurrent_reload_from_overlaying_ghost(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    ghost = AgentIdentity(agent_id="legacy_1", name="ghost")
    request = registry._make_persist_request(
        "register", ghost, validated_epoch=1
    )
    registry._agents[ghost.agent_id] = ghost
    registry._loaded = True
    registry._inflight_requests = [request]
    restoring = threading.Event()
    release_restore = threading.Event()
    reload_done = threading.Event()
    original_restore = registry._restore_requests_from_disk

    def blocked_restore(requests) -> None:
        restoring.set()
        assert release_restore.wait(timeout=2)
        original_restore(requests)

    registry._restore_requests_from_disk = blocked_restore  # type: ignore[method-assign]
    discard_thread = threading.Thread(
        target=registry._discard_inflight_and_reconcile,
        args=(request,),
    )
    discard_thread.start()
    assert restoring.wait(timeout=2)

    def reload() -> None:
        with registry._lock:
            registry._reload_from_disk()
        reload_done.set()

    reload_thread = threading.Thread(target=reload)
    reload_thread.start()
    assert not reload_done.wait(timeout=0.05)
    release_restore.set()
    discard_thread.join(timeout=2)
    reload_thread.join(timeout=2)

    assert not discard_thread.is_alive()
    assert reload_done.is_set()
    assert registry.get("legacy_1") is None
    assert registry._inflight_requests == []


def test_stale_background_write_discard_rebuilds_cache_without_ghost(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="ghost"))
    current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)

    registry._flush_persist_queue()

    assert registry.get("legacy_1") is None
    assert registry._inflight_requests == []
    assert not (tmp_path / "agents" / "legacy_1" / "identity.json").exists()


def test_failed_earlier_write_then_cutover_preserves_later_accepted_value(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="legacy_1", name="r1"))
    registry.update(AgentIdentity(agent_id="legacy_1", name="r2"))
    with registry._lock:
        failed, remaining = registry._persist_queue
        registry._persist_queue = [remaining]
        registry._inflight_requests = [failed]
    registry._discard_inflight_and_reconcile(failed)

    assert registry.get("legacy_1").name == "r2"
    assert registry._persist_queue == [remaining]

    def advance() -> AuthoritySnapshot:
        nonlocal current
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    registry.cutover_authority(advance)

    assert registry.get("legacy_1").name == "r2"
    persisted = json.loads(
        (tmp_path / "agents" / "legacy_1" / "identity.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["name"] == "r2"
    assert registry._persist_queue == []
    assert registry._inflight_requests == []


def test_cross_process_async_admission_reserves_casefold_name_before_identity_write(
    tmp_path,
) -> None:
    """The flock-protected durable index is the admission linearization point."""

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_register_without_flushing,
            args=(str(tmp_path), "role_a", "Straße", start, results),
        ),
        context.Process(
            target=_register_without_flushing,
            args=(str(tmp_path), "role_b", "STRASSE", start, results),
        ),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    assert sorted(results.get(timeout=2) for _ in processes) == [
        "accepted",
        "duplicate",
    ]
    persisted = AgentRegistry.legacy(str(tmp_path)).list_agents("oc_large_team")
    assert len(persisted) == 1


def test_async_remove_is_linearized_with_pending_register_across_instances(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    first = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    second = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    first._persist_thread = MagicMock(is_alive=lambda: True)
    first.register(
        AgentIdentity(
            agent_id="role_pending",
            name="Pending",
            owner_group="oc_large_team",
        )
    )

    assert second.remove("role_pending") is True
    first._flush_persist_queue()

    assert AgentRegistry.legacy(str(tmp_path)).get("role_pending") is None
    assert not (tmp_path / "agents" / "role_pending" / "identity.json").exists()


def test_cross_instance_async_updates_share_durable_name_admission(tmp_path) -> None:
    seed = AgentRegistry.legacy(str(tmp_path))
    seed.register(
        AgentIdentity(agent_id="role_a", name="First", owner_group="oc_large_team")
    )
    seed.register(
        AgentIdentity(agent_id="role_b", name="Second", owner_group="oc_large_team")
    )
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    first = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    second = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    first._persist_thread = MagicMock(is_alive=lambda: True)
    second._persist_thread = MagicMock(is_alive=lambda: True)

    assert first.update(
        AgentIdentity(
            agent_id="role_a",
            name="Straße",
            owner_group="oc_large_team",
        )
    )
    with pytest.raises(DuplicateAgentNameError):
        second.update(
            AgentIdentity(
                agent_id="role_b",
                name="STRASSE",
                owner_group="oc_large_team",
            )
        )


def test_new_reader_prefers_accepted_index_over_stale_identity_copy(tmp_path) -> None:
    seed = AgentRegistry.legacy(str(tmp_path))
    seed.register(
        AgentIdentity(agent_id="role_a", name="Old", owner_group="oc_large_team")
    )
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    writer = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    writer._persist_thread = MagicMock(is_alive=lambda: True)
    assert writer.update(
        AgentIdentity(agent_id="role_a", name="New", owner_group="oc_large_team")
    )

    observed = AgentRegistry.legacy(str(tmp_path)).get("role_a")

    assert observed is not None
    assert observed.name == "New"


def test_failed_old_copy_cannot_rollback_newer_same_payload_revision(tmp_path) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    first = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    second = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    first._persist_thread = MagicMock(is_alive=lambda: True)
    second._persist_thread = MagicMock(is_alive=lambda: True)
    accepted = first.register(
        AgentIdentity(
            agent_id="role_aba",
            name="Same payload",
            owner_group="oc_large_team",
        )
    )
    with first._lock:
        failed = first._persist_queue.pop()
        first._inflight_requests.append(failed)
    first._write_agent_to_disk = MagicMock(side_effect=OSError("copy failed"))
    with pytest.raises(OSError, match="copy failed"):
        first._persist_request(failed)

    second.register(AgentIdentity.from_dict(accepted.to_dict()))
    index = json.loads(
        (tmp_path / "agent_registry.v1.json").read_text(encoding="utf-8")
    )
    assert index["agents"]["role_aba"]["revision"] != failed.revision

    first._discard_inflight_and_reconcile(failed)

    assert AgentRegistry.legacy(str(tmp_path)).get("role_aba") is not None
    with second._lock:
        failed_second = second._persist_queue.pop()
        second._inflight_requests.append(failed_second)
    second._write_agent_to_disk = MagicMock(side_effect=OSError("second failed"))
    with pytest.raises(OSError, match="second failed"):
        second._persist_request(failed_second)
    second._discard_inflight_and_reconcile(failed_second)

    assert AgentRegistry.legacy(str(tmp_path)).get("role_aba") is None


@pytest.mark.parametrize("shape", ["flat_v1", "revision_v1"])
def test_pre_materialization_index_migration_rolls_back_to_compat_identity(
    tmp_path, shape
) -> None:
    durable = AgentIdentity(
        agent_id="role_migrate",
        name="A durable",
        owner_group="oc_large_team",
    )
    accepted = AgentIdentity.from_dict(durable.to_dict())
    accepted.name = "B accepted"
    identity_dir = tmp_path / "agents" / durable.agent_id
    identity_dir.mkdir(parents=True)
    (identity_dir / "identity.json").write_text(
        json.dumps(durable.to_dict()), encoding="utf-8"
    )
    record = (
        accepted.to_dict()
        if shape == "flat_v1"
        else {"revision": "rev-b", "identity": accepted.to_dict()}
    )
    (tmp_path / "agent_registry.v1.json").write_text(
        json.dumps({"version": 1, "agents": {accepted.agent_id: record}}),
        encoding="utf-8",
    )
    registry = AgentRegistry.legacy(str(tmp_path))
    assert registry.get(accepted.agent_id).name == "B accepted"
    request = registry._make_persist_request(
        "update",
        accepted,
        validated_epoch=0,
        revision=registry._revisions[accepted.agent_id],
    )
    registry._inflight_requests = [request]

    registry._discard_inflight_and_reconcile(request)

    observed = AgentRegistry.legacy(str(tmp_path)).get(accepted.agent_id)
    assert observed is not None
    assert observed.name == "A durable"


@pytest.mark.parametrize("operation", ["register", "update", "move"])
def test_materialized_index_commit_failure_compensates_identity_and_current(
    tmp_path, operation
) -> None:
    registry = AgentRegistry.legacy(str(tmp_path / operation))
    baseline = registry.register(
        AgentIdentity(
            agent_id="role_compensate",
            name="A durable",
            owner_group="source",
        )
    )
    baseline_revision = registry._revisions[baseline.agent_id]
    original_write_index = registry._write_index_to_disk
    failed = False

    def fail_new_materialized_commit() -> None:
        nonlocal failed
        materialized = registry._materialized.get(baseline.agent_id)
        current_revision = registry._revisions.get(baseline.agent_id)
        if (
            not failed
            and materialized is not None
            and materialized.revision == current_revision
            and current_revision != baseline_revision
        ):
            failed = True
            raise OSError("materialized index commit failed")
        original_write_index()

    registry._write_index_to_disk = fail_new_materialized_commit  # type: ignore[method-assign]
    replacement = AgentIdentity.from_dict(baseline.to_dict())
    replacement.name = "B attempted"
    if operation == "move":
        outcome = registry.move_agent(baseline.agent_id, "source", "target")
        assert outcome.status == MoveResult.PERSIST_FAILED
    else:
        with pytest.raises(OSError, match="materialized index commit failed"):
            getattr(registry, operation)(replacement)

    fresh = AgentRegistry.legacy(registry.base_path)
    observed = fresh.get(baseline.agent_id)
    assert observed is not None
    assert observed.name == "A durable"
    assert observed.owner_group == "source"
    compat = json.loads(
        (Path(registry.base_path) / "agents" / baseline.agent_id / "identity.json").read_text(
            encoding="utf-8"
        )
    )
    assert compat["name"] == "A durable"
    index = json.loads(
        (Path(registry.base_path) / "agent_registry.v1.json").read_text(
            encoding="utf-8"
        )
    )["agents"][baseline.agent_id]
    assert index["revision"] == baseline_revision
    assert index["materialized"]["revision"] == baseline_revision


@pytest.mark.parametrize("operation", ["register", "update", "remove", "move"])
def test_post_replace_index_dir_fsync_failure_uses_committed_admission_semantics(
    tmp_path, monkeypatch, operation
) -> None:
    registry = AgentRegistry.legacy(str(tmp_path / operation))
    baseline = registry.register(
        AgentIdentity(agent_id="role_uncertain", name="A", owner_group="source")
    )
    real_fsync_directory = AgentRegistry._fsync_directory
    failed = False

    def fail_first_index_parent_fsync(path: str) -> None:
        nonlocal failed
        if not failed and os.path.realpath(path) == registry.base_path:
            failed = True
            raise OSError("post-replace dir fsync failed")
        real_fsync_directory(path)

    monkeypatch.setattr(
        AgentRegistry,
        "_fsync_directory",
        staticmethod(fail_first_index_parent_fsync),
    )
    replacement = AgentIdentity.from_dict(baseline.to_dict())
    replacement.name = "B"
    if operation == "register":
        registry.register(
            AgentIdentity(agent_id="role_new", name="B", owner_group="source")
        )
    elif operation == "update":
        assert registry.update(replacement) is True
    elif operation == "remove":
        assert registry.remove(baseline.agent_id) is True
    else:
        assert registry.move_agent(baseline.agent_id, "source", "target").success

    fresh = AgentRegistry.legacy(registry.base_path)
    if operation == "register":
        assert fresh.get("role_new") is not None
    elif operation == "remove":
        assert fresh.get(baseline.agent_id) is None
    elif operation == "move":
        assert fresh.get(baseline.agent_id).owner_group == "target"
    else:
        assert fresh.get(baseline.agent_id).name == "B"


@pytest.mark.parametrize("operation", ["register", "update", "move"])
def test_post_replace_materialized_index_fsync_failure_keeps_committed_result(
    tmp_path, monkeypatch, operation
) -> None:
    registry = AgentRegistry.legacy(str(tmp_path / f"materialized-{operation}"))
    baseline = registry.register(
        AgentIdentity(agent_id="role_materialized", name="A", owner_group="source")
    )
    baseline_revision = registry._revisions[baseline.agent_id]
    real_fsync_directory = AgentRegistry._fsync_directory
    failed = False

    def fail_materialized_index_parent_fsync(path: str) -> None:
        nonlocal failed
        materialized = registry._materialized.get(baseline.agent_id)
        current = registry._revisions.get(baseline.agent_id)
        if (
            not failed
            and os.path.realpath(path) == registry.base_path
            and materialized is not None
            and materialized.revision == current
            and current != baseline_revision
        ):
            failed = True
            raise OSError("materialized dir fsync failed")
        real_fsync_directory(path)

    monkeypatch.setattr(
        AgentRegistry,
        "_fsync_directory",
        staticmethod(fail_materialized_index_parent_fsync),
    )
    replacement = AgentIdentity.from_dict(baseline.to_dict())
    replacement.name = "B"
    if operation == "register":
        registry.register(replacement)
    elif operation == "update":
        assert registry.update(replacement) is True
    else:
        assert registry.move_agent(baseline.agent_id, "source", "target").success

    fresh = AgentRegistry.legacy(registry.base_path)
    observed = fresh.get(baseline.agent_id)
    assert observed is not None
    if operation == "move":
        assert observed.owner_group == "target"
    else:
        assert observed.name == "B"


def test_legacy_scan_materialized_snapshot_is_not_mutated_by_failed_move(
    tmp_path,
) -> None:
    baseline = AgentIdentity(
        agent_id="legacy_move",
        name="Legacy move",
        owner_group="source",
        member_groups=["source"],
    )
    identity_dir = tmp_path / "agents" / baseline.agent_id
    identity_dir.mkdir(parents=True)
    (identity_dir / "identity.json").write_text(
        json.dumps(baseline.to_dict()), encoding="utf-8"
    )
    registry = AgentRegistry.legacy(str(tmp_path))
    original_write_index = registry._write_index_to_disk
    writes = 0

    def fail_materialized_commit() -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("materialized index commit failed")
        original_write_index()

    registry._write_index_to_disk = fail_materialized_commit  # type: ignore[method-assign]

    outcome = registry.move_agent(baseline.agent_id, "source", "target")

    assert outcome.status == MoveResult.PERSIST_FAILED
    observed = AgentRegistry.legacy(str(tmp_path)).get(baseline.agent_id)
    assert observed is not None
    assert observed.owner_group == "source"


def test_reader_scan_and_index_publish_are_serialized_by_storage_flock(
    tmp_path,
) -> None:
    old = AgentIdentity(agent_id="old", name="Old", owner_group="oc_large_team")
    old_dir = tmp_path / "agents" / old.agent_id
    old_dir.mkdir(parents=True)
    (old_dir / "identity.json").write_text(
        json.dumps(old.to_dict()), encoding="utf-8"
    )
    reader = AgentRegistry.legacy(str(tmp_path))
    writer = AgentRegistry.legacy(str(tmp_path))
    scanned = threading.Event()
    release_scan = threading.Event()
    writer_done = threading.Event()
    writer_started = threading.Event()
    original_writer_storage_lock = writer._storage_mutation_lock

    @contextmanager
    def observed_writer_storage_lock():
        writer_started.set()
        with original_writer_storage_lock():
            yield

    writer._storage_mutation_lock = observed_writer_storage_lock  # type: ignore[method-assign]
    original_scan = reader._scan_agents_dir

    def blocked_scan():
        result = original_scan()
        scanned.set()
        assert release_scan.wait(timeout=2)
        return result

    reader._scan_agents_dir = blocked_scan  # type: ignore[method-assign]
    reader_thread = threading.Thread(target=reader.list_agents)
    reader_thread.start()
    assert scanned.wait(timeout=2)

    def publish() -> None:
        writer.register(
            AgentIdentity(
                agent_id="new",
                name="New",
                owner_group="oc_large_team",
            )
        )
        writer_done.set()

    writer_thread = threading.Thread(target=publish)
    writer_thread.start()
    assert writer_started.wait(timeout=2)
    assert not writer_done.wait(timeout=0.05)
    release_scan.set()
    reader_thread.join(timeout=2)
    writer_thread.join(timeout=2)

    assert not reader_thread.is_alive()
    assert writer_done.is_set()
    assert {agent.agent_id for agent in AgentRegistry.legacy(str(tmp_path)).list_agents()} == {
        "old",
        "new",
    }


def test_cutover_materializes_other_instance_accepted_index_before_advance(
    tmp_path,
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    accepting = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    cutting_over = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    accepting._persist_thread = MagicMock(is_alive=lambda: True)
    accepting.register(
        AgentIdentity(
            agent_id="role_remote",
            name="Remote accepted",
            owner_group="oc_large_team",
        )
    )

    def advance() -> AuthoritySnapshot:
        nonlocal current
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    cutting_over.cutover_authority(advance)
    accepting._flush_persist_queue()

    identity = tmp_path / "agents" / "role_remote" / "identity.json"
    assert identity.exists()
    assert json.loads(identity.read_text(encoding="utf-8"))["name"] == "Remote accepted"
    index = json.loads(
        (tmp_path / "agent_registry.v1.json").read_text(encoding="utf-8")
    )
    assert "role_remote" in index["agents"]
    assert AgentRegistry.legacy(str(tmp_path)).get("role_remote") is not None


def test_cutover_does_not_advance_when_identity_fsync_fails(
    tmp_path, monkeypatch
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="role_durable", name="Durable"))
    advanced = False

    def advance() -> AuthoritySnapshot:
        nonlocal advanced, current
        advanced = True
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    monkeypatch.setattr(os, "fsync", MagicMock(side_effect=OSError("fsync failed")))

    with pytest.raises(OSError, match="fsync failed"):
        registry.cutover_authority(advance)

    assert advanced is False
    assert current == AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)


@pytest.mark.parametrize("boundary", ["identity", "index"])
def test_cutover_rejects_post_replace_directory_fsync_failure(
    tmp_path, monkeypatch, boundary
) -> None:
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    registry._persist_thread = MagicMock(is_alive=lambda: True)
    registry.register(AgentIdentity(agent_id="role_strict", name="Strict"))
    agent_dir = tmp_path / "agents" / "role_strict"
    agent_dir.mkdir(parents=True, exist_ok=True)
    if boundary == "identity":
        real_fsync = os.fsync

        def fail_identity_dir_fd(fd) -> None:
            fd_path = os.path.realpath(f"/proc/self/fd/{fd}")
            if fd_path == os.path.realpath(agent_dir):
                raise OSError("strict identity directory fsync failed")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_identity_dir_fd)
    else:
        real_fsync_directory = AgentRegistry._fsync_directory

        def fail_index_parent(path: str) -> None:
            if os.path.realpath(path) == os.path.realpath(tmp_path):
                raise OSError("strict index directory fsync failed")
            real_fsync_directory(path)

        monkeypatch.setattr(
            AgentRegistry,
            "_fsync_directory",
            staticmethod(fail_index_parent),
        )
    advanced = False

    def advance() -> AuthoritySnapshot:
        nonlocal advanced, current
        advanced = True
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    with pytest.raises(OSError, match=f"strict {boundary}"):
        registry.cutover_authority(advance)

    assert advanced is False
    assert current.epoch == 1


def test_legacy_directory_cutover_publishes_index_before_authority_advance(
    tmp_path,
) -> None:
    legacy = AgentIdentity(agent_id="legacy_only", name="Legacy only")
    legacy_dir = tmp_path / "agents" / legacy.agent_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "identity.json").write_text(
        json.dumps(legacy.to_dict()), encoding="utf-8"
    )
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    registry._write_index_to_disk = MagicMock(
        side_effect=OSError("index fsync failed")
    )
    advanced = False

    def advance() -> AuthoritySnapshot:
        nonlocal advanced, current
        advanced = True
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    with pytest.raises(OSError, match="index fsync failed"):
        registry.cutover_authority(advance)

    assert advanced is False
    assert current == AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)


def test_remove_fsyncs_identity_unlink_and_agent_directory(tmp_path, monkeypatch) -> None:
    registry = AgentRegistry.legacy(str(tmp_path))
    registry.register(AgentIdentity(agent_id="role_remove", name="Remove"))
    real_unlink = os.remove
    real_fsync = os.fsync
    unlinked = False
    post_unlink_fsyncs = 0

    def observed_unlink(path, *args, **kwargs) -> None:
        nonlocal unlinked
        real_unlink(path, *args, **kwargs)
        unlinked = True

    def observed_fsync(fd) -> None:
        nonlocal post_unlink_fsyncs
        real_fsync(fd)
        if unlinked:
            post_unlink_fsyncs += 1

    monkeypatch.setattr(os, "remove", observed_unlink)
    monkeypatch.setattr(os, "fsync", observed_fsync)

    assert registry.remove("role_remove") is True
    assert post_unlink_fsyncs >= 2
    assert not (tmp_path / "agents" / "role_remove").exists()


def test_remove_post_unlink_fsync_failure_uses_committed_visible_semantics(
    tmp_path, monkeypatch
) -> None:
    registry = AgentRegistry.legacy(str(tmp_path))
    registry.register(AgentIdentity(agent_id="role_remove", name="Remove"))
    real_remove = os.remove
    real_fsync = os.fsync
    unlinked = False
    failed = False

    def observed_remove(path, *args, **kwargs) -> None:
        nonlocal unlinked
        real_remove(path, *args, **kwargs)
        unlinked = True

    def fail_first_post_unlink_fsync(fd) -> None:
        nonlocal failed
        if unlinked and not failed:
            failed = True
            raise OSError("post-unlink fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "remove", observed_remove)
    monkeypatch.setattr(os, "fsync", fail_first_post_unlink_fsync)

    assert registry.remove("role_remove") is True
    assert AgentRegistry.legacy(str(tmp_path)).get("role_remove") is None


def test_cutover_removes_index_orphan_before_authority_advance(tmp_path) -> None:
    # Publish an empty authoritative index, then emulate a crash that left a
    # legacy compatibility identity after its index tombstone.
    assert AgentRegistry.legacy(str(tmp_path)).list_agents() == []
    orphan = AgentIdentity(agent_id="orphan", name="Orphan")
    orphan_dir = tmp_path / "agents" / orphan.agent_id
    orphan_dir.mkdir(parents=True)
    orphan_file = orphan_dir / "identity.json"
    orphan_file.write_text(json.dumps(orphan.to_dict()), encoding="utf-8")
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )

    def advance() -> AuthoritySnapshot:
        nonlocal current
        assert not orphan_file.exists()
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    registry.cutover_authority(advance)

    assert not orphan_dir.exists()


def test_cutover_rejects_symlink_agent_dir_without_touching_external_identity(
    tmp_path,
) -> None:
    assert AgentRegistry.legacy(str(tmp_path)).list_agents() == []
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir()
    external_identity = external / "identity.json"
    external_identity.write_text(
        json.dumps(AgentIdentity(agent_id="evil", name="External").to_dict()),
        encoding="utf-8",
    )
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(exist_ok=True)
    (agents_dir / "evil").symlink_to(external, target_is_directory=True)
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    advanced = False

    def advance() -> AuthoritySnapshot:
        nonlocal advanced, current
        advanced = True
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    with pytest.raises(OSError, match="symlink|containment|unsafe"):
        registry.cutover_authority(advance)

    assert advanced is False
    assert external_identity.exists()


@pytest.mark.parametrize("operation", ["register", "update", "move"])
def test_mutation_rejects_agent_directory_symlink_without_external_write(
    tmp_path, operation
) -> None:
    registry = AgentRegistry.legacy(str(tmp_path))
    baseline = registry.register(
        AgentIdentity(agent_id="role_symlink", name="A", owner_group="source")
    )
    managed_dir = tmp_path / "agents" / baseline.agent_id
    external = tmp_path.parent / f"{tmp_path.name}-{operation}-external"
    managed_dir.rename(external)
    external_identity = external / "identity.json"
    before = external_identity.read_bytes()
    managed_dir.symlink_to(external, target_is_directory=True)
    replacement = AgentIdentity.from_dict(baseline.to_dict())
    replacement.name = "B"

    if operation == "move":
        outcome = registry.move_agent(baseline.agent_id, "source", "target")
        assert outcome.status == MoveResult.PERSIST_FAILED
    else:
        with pytest.raises(OSError, match="symlink|unsafe|nofollow"):
            getattr(registry, operation)(replacement)

    assert external_identity.read_bytes() == before
    fresh = AgentRegistry.legacy(str(tmp_path)).get(baseline.agent_id)
    assert fresh is not None
    assert fresh.name == "A"
    assert fresh.owner_group == "source"


def test_legacy_only_cutover_rejects_agent_dir_symlink_before_external_write(
    tmp_path,
) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-legacy-external"
    external.mkdir()
    external_identity = external / "identity.json"
    external_identity.write_text(
        json.dumps(
            AgentIdentity(agent_id="legacy_evil", name="External").to_dict()
        ),
        encoding="utf-8",
    )
    before = external_identity.read_bytes()
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "legacy_evil").symlink_to(external, target_is_directory=True)
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    advanced = False

    def advance() -> AuthoritySnapshot:
        nonlocal advanced, current
        advanced = True
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    with pytest.raises(OSError, match="symlink|unsafe|nofollow"):
        registry.cutover_authority(advance)

    assert advanced is False
    assert external_identity.read_bytes() == before


def test_update_rejects_compat_identity_symlink_without_external_write(
    tmp_path,
) -> None:
    registry = AgentRegistry.legacy(str(tmp_path))
    baseline = registry.register(
        AgentIdentity(agent_id="identity_symlink", name="A", owner_group="source")
    )
    identity_file = tmp_path / "agents" / baseline.agent_id / "identity.json"
    identity_file.unlink()
    external = tmp_path.parent / f"{tmp_path.name}-identity-external.json"
    external.write_text("external sentinel", encoding="utf-8")
    before = external.read_bytes()
    identity_file.symlink_to(external)
    replacement = AgentIdentity.from_dict(baseline.to_dict())
    replacement.name = "B"

    with pytest.raises(OSError, match="symlink|unsafe|nofollow"):
        registry.update(replacement)

    assert external.read_bytes() == before
    assert identity_file.is_symlink()


def test_cutover_does_not_advance_after_orphan_unlink_fsync_failure(
    tmp_path, monkeypatch
) -> None:
    assert AgentRegistry.legacy(str(tmp_path)).list_agents() == []
    orphan = AgentIdentity(agent_id="orphan", name="Orphan")
    orphan_dir = tmp_path / "agents" / orphan.agent_id
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "identity.json").write_text(
        json.dumps(orphan.to_dict()), encoding="utf-8"
    )
    current = AuthoritySnapshot(epoch=1, mode=AuthorityMode.LEGACY_WRITE)
    registry = AgentRegistry(
        str(tmp_path),
        mutation_guard=LegacyMutationGuard(lambda: current, expected_epoch=1),
    )
    real_remove = os.remove
    real_fsync = os.fsync
    unlinked = False

    def observed_remove(path, *args, **kwargs) -> None:
        nonlocal unlinked
        real_remove(path, *args, **kwargs)
        unlinked = True

    def fail_after_unlink(fd) -> None:
        if unlinked:
            raise OSError("orphan unlink fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "remove", observed_remove)
    monkeypatch.setattr(os, "fsync", fail_after_unlink)
    advanced = False

    def advance() -> AuthoritySnapshot:
        nonlocal advanced, current
        advanced = True
        current = AuthoritySnapshot(epoch=2, mode=AuthorityMode.V5_WRITE)
        return current

    with pytest.raises(OSError, match="orphan unlink fsync failed"):
        registry.cutover_authority(advance)

    assert advanced is False
    assert current.epoch == 1
