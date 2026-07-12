from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from src.autonomous.journal.frame import JournalEvent
from src.autonomous.workforce.authority import (
    AuthorityMode,
    AuthoritySnapshot,
    LegacyMutationGuard,
    StaleAuthorityEpoch,
)
from src.slock_engine.agent_registry import AgentRegistry, MoveResult
from src.slock_engine.models import AgentIdentity
from tests.autonomous.workforce_helpers import (
    commit_events,
    make_writer,
    replay_state,
)


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
