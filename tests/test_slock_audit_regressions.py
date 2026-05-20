"""Regression coverage for confirmed Slock audit findings."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

from src.slock_engine.engine import SlockEngine
from src.slock_engine.memory_manager import MemoryManager
from src.slock_engine.models import AgentIdentity, AgentStatus, SlockChannel, SlockTask
from src.slock_engine.observer_queue import ObserverLearningQueue
from src.slock_engine.task_board_manager import TaskBoardManager


def test_task_board_flush_persists_latest_state_when_snapshot_is_stale(tmp_path):
    """Dirty flush must not clear a newer mutation by writing an older snapshot."""
    memory = MemoryManager(base_path=str(tmp_path))
    lock = threading.RLock()
    tasks = [SlockTask(task_id="old-task", content="old", created_in="chat-001")]
    dirty = True
    channel = SlockChannel(channel_id="chat-001", name="Team")

    def get_dirty() -> bool:
        return dirty

    def set_dirty(value: bool) -> None:
        nonlocal dirty
        dirty = value

    manager = TaskBoardManager(
        lock=lock,
        tasks=tasks,
        channel_getter=lambda: channel,
        chat_id_getter=lambda: "chat-001",
        dirty_getter=get_dirty,
        dirty_setter=set_dirty,
        router=MagicMock(),
        memory=memory,
        registry_get=MagicMock(),
        execute_agent_fn=MagicMock(),
    )

    stale_snapshot = list(tasks)
    tasks.append(SlockTask(task_id="new-task", content="new", created_in="chat-001"))
    set_dirty(True)

    manager._flush_if_dirty(stale_snapshot)

    persisted_ids = {task.task_id for task in memory.read_task_board("chat-001")}
    assert persisted_ids == {"old-task", "new-task"}
    assert dirty is False


def test_observer_flush_requeues_unprocessed_records_after_timeout():
    """A timed-out observer flush should preserve records it did not process."""

    class SlowMemory:
        def __init__(self) -> None:
            self.calls = 0

        def record_skill_feedback(self, agent_id, skill_tags, *, quality_score):
            self.calls += 1
            time.sleep(0.02)
            return []

        def update_agent_context(self, agent_id, entry):
            return None

    router = MagicMock()
    queue = ObserverLearningQueue(
        SlowMemory(),
        router,
        flush_interval=3600.0,
        flush_timeout=0.001,
    )
    queue.enqueue("observer", "actor-1", "first", ["code"])
    queue.enqueue("observer", "actor-2", "second", ["code"])

    flushed = queue.flush()

    assert flushed == 1
    assert queue.pending_count == 1


def test_execute_agent_aborts_when_intermediate_transition_fails(tmp_path):
    """State-machine failures during execution should stop work and restore IDLE."""
    engine = SlockEngine(
        chat_id="chat-transitions",
        root_path=str(tmp_path),
        memory_base_path=str(tmp_path),
    )
    engine.activate_channel(SlockChannel(channel_id="chat-transitions", name="Transitions"))
    agent = AgentIdentity(agent_id="agent-transition", name="TransitionBot", agent_type="coco")

    with patch.object(engine, "transition_agent", side_effect=[True, False]):
        with patch.object(engine, "_run_acp_session") as run_session:
            result = engine._execute_agent(agent, "do work", None)

    assert result is None
    run_session.assert_not_called()
    assert engine.get_agent_status(agent.agent_id) == AgentStatus.IDLE
