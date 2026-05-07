"""Tests for src.card.orchestrator module."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from src.card.events import CardEvent, CardEventType
from src.card.orchestrator import TaskOrchestrator
from src.card.task_registry import TaskRegistry


class FakeSession:
    """Minimal fake CardSession for testing dispatch routing."""

    def __init__(self, session_id: str = ""):
        self.session_id = session_id
        self.dispatched_events: list[CardEvent] = []
        self._lock = threading.Lock()

    def dispatch(self, event: CardEvent) -> None:
        with self._lock:
            self.dispatched_events.append(event)

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self.dispatched_events)

    def events_of_type(self, event_type: CardEventType) -> list[CardEvent]:
        with self._lock:
            return [e for e in self.dispatched_events if e.type == event_type]


def _make_orchestrator(session_creator=None):
    """Create a TaskOrchestrator with optional session creator."""
    registry = TaskRegistry()

    if session_creator is None:
        sessions_created: dict[str, FakeSession] = {}

        def _creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            sessions_created[task_id] = s
            return s

        session_creator = _creator
    else:
        sessions_created = {}

    orch = TaskOrchestrator(
        chat_id="chat_123",
        session_creator=session_creator,
        registry=registry,
    )
    return orch, registry, sessions_created


class TestOnPlanReceived:
    def test_creates_sessions_for_each_task(self):
        """on_plan_received creates one session per valid task."""
        orch, registry, sessions = _make_orchestrator()
        tasks = [
            {"task_id": "t1", "name": "分析需求"},
            {"task_id": "t2", "name": "编写代码"},
            {"task_id": "t3", "name": "运行测试"},
        ]
        orch.on_plan_received(tasks)

        assert orch.active_session_count == 3
        assert registry.count == 3
        assert set(sessions.keys()) == {"t1", "t2", "t3"}

    def test_sessions_receive_initial_task_list(self):
        """Each session gets an initial TASK_LIST_UPDATED on creation."""
        orch, _, sessions = _make_orchestrator()
        tasks = [
            {"task_id": "t1", "name": "Task 1"},
            {"task_id": "t2", "name": "Task 2"},
        ]
        orch.on_plan_received(tasks)

        for tid, session in sessions.items():
            task_list_events = session.events_of_type(CardEventType.TASK_LIST_UPDATED)
            assert len(task_list_events) >= 1
            payload = task_list_events[0].payload
            assert payload["current_task_id"] == tid
            assert len(payload["tasks"]) == 2

    def test_fallback_on_empty_plan(self):
        """Empty plan enters fallback mode."""
        orch, _, _ = _make_orchestrator()
        orch.on_plan_received([])
        assert orch.is_fallback_mode is True
        assert orch.active_session_count == 0

    def test_fallback_on_invalid_tasks(self):
        """Tasks without task_id/name enter fallback mode."""
        orch, _, _ = _make_orchestrator()
        orch.on_plan_received([{"foo": "bar"}, {"x": 1}])
        assert orch.is_fallback_mode is True

    def test_fallback_on_none(self):
        """None plan enters fallback mode."""
        orch, _, _ = _make_orchestrator()
        orch.on_plan_received(None)
        assert orch.is_fallback_mode is True


class TestDispatchToTask:
    def test_routes_to_correct_session(self):
        """dispatch_to_task routes event to the bound session only."""
        orch, _, sessions = _make_orchestrator()
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "b1", "text": "hello"})
        orch.dispatch_to_task("t1", event)

        # t1 should have received the event (plus initial TASK_LIST_UPDATED)
        assert any(e.type == CardEventType.TEXT_DELTA for e in sessions["t1"].dispatched_events)
        # t2 should NOT have received TEXT_DELTA
        assert not any(e.type == CardEventType.TEXT_DELTA for e in sessions["t2"].dispatched_events)

    def test_unknown_task_id_logs_warning(self):
        """Unknown task_id is dropped with warning."""
        orch, _, _ = _make_orchestrator()
        orch.on_plan_received([{"task_id": "t1", "name": "A"}])

        with patch("src.card.orchestrator.logger") as mock_logger:
            event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "b1", "text": "x"})
            orch.dispatch_to_task("nonexistent", event)
            mock_logger.warning.assert_called()

    def test_fallback_mode_dispatches_to_fallback_session(self):
        """In fallback mode, all dispatches go to fallback session."""
        orch, _, _ = _make_orchestrator()
        orch.on_plan_received([])  # enters fallback

        fallback = FakeSession("fallback")
        orch.set_fallback_session(fallback)

        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "b1", "text": "x"})
        orch.dispatch_to_task("any_id", event)

        assert fallback.event_count == 1


class TestBroadcastStatusChange:
    def test_broadcast_updates_all_sessions(self):
        """Status change broadcasts TASK_LIST_UPDATED to all sessions."""
        orch, registry, sessions = _make_orchestrator()
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        # Clear initial events
        for s in sessions.values():
            s.dispatched_events.clear()

        orch.broadcast_status_change("t1", "completed")
        # Wait for debounce + processing
        time.sleep(0.2)

        # Both sessions should have received TASK_LIST_UPDATED
        for tid, session in sessions.items():
            task_list_events = session.events_of_type(CardEventType.TASK_LIST_UPDATED)
            assert len(task_list_events) >= 1
            # Verify t1 status is updated in payload
            payload = task_list_events[-1].payload
            t1_in_payload = next(t for t in payload["tasks"] if t["task_id"] == "t1")
            assert t1_in_payload["status"] == "completed"

    def test_broadcast_debounce(self):
        """Rapid status changes are debounced into fewer broadcasts."""
        orch, _, sessions = _make_orchestrator()
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        for s in sessions.values():
            s.dispatched_events.clear()

        # Rapid fire multiple status changes
        orch.broadcast_status_change("t1", "in_progress")
        orch.broadcast_status_change("t2", "in_progress")
        orch.broadcast_status_change("t1", "completed")

        time.sleep(0.3)

        # Should have fewer broadcasts than changes (debounce coalescing)
        for session in sessions.values():
            task_list_events = session.events_of_type(CardEventType.TASK_LIST_UPDATED)
            # At least 1 but potentially fewer than 3 (debounced)
            assert len(task_list_events) >= 1


class TestSubagentSession:
    def test_create_subagent_session(self):
        """create_subagent_session adds new task and session."""
        orch, registry, sessions = _make_orchestrator()
        orch.on_plan_received([{"task_id": "t1", "name": "Main Task"}])

        orch.create_subagent_session("sub1", "Subagent 探索")

        assert registry.count == 2
        assert orch.active_session_count == 2
        assert "sub1" in sessions

    def test_subagent_ignored_in_fallback(self):
        """Subagent creation is skipped in fallback mode."""
        orch, registry, _ = _make_orchestrator()
        orch.on_plan_received([])  # fallback
        orch.create_subagent_session("sub1", "Sub")
        assert registry.count == 0


class TestClose:
    def test_close_dispatches_completed_to_all(self):
        """close() dispatches COMPLETED to all sessions."""
        orch, _, sessions = _make_orchestrator()
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        orch.close()

        for session in sessions.values():
            completed = session.events_of_type(CardEventType.COMPLETED)
            assert len(completed) == 1

    def test_close_idempotent(self):
        """Calling close() twice is safe."""
        orch, _, _ = _make_orchestrator()
        orch.on_plan_received([{"task_id": "t1", "name": "A"}])
        orch.close()
        orch.close()  # Should not raise

    def test_dispatch_after_close_is_no_op(self):
        """Events dispatched after close are silently dropped."""
        orch, _, sessions = _make_orchestrator()
        orch.on_plan_received([{"task_id": "t1", "name": "A"}])
        orch.close()

        initial_count = sessions["t1"].event_count
        orch.dispatch_to_task("t1", CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "x", "text": "y"}))
        assert sessions["t1"].event_count == initial_count


class TestConcurrentSubagent:
    def test_concurrent_dispatch_no_race(self):
        """Multiple threads dispatching to different tasks simultaneously — no races."""
        orch, _, sessions = _make_orchestrator()
        tasks = [{"task_id": f"t{i}", "name": f"Task {i}"} for i in range(5)]
        orch.on_plan_received(tasks)

        errors = []
        barrier = threading.Barrier(5)

        def worker(task_id: str):
            try:
                barrier.wait(timeout=2)
                for j in range(20):
                    event = CardEvent(
                        type=CardEventType.TEXT_DELTA,
                        payload={"block_id": f"b_{task_id}_{j}", "text": f"chunk {j}"},
                    )
                    orch.dispatch_to_task(task_id, event)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(f"t{i}",)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors

        # Each session should have received exactly 20 TEXT_DELTAs + initial TASK_LIST_UPDATED
        for i in range(5):
            tid = f"t{i}"
            text_events = sessions[tid].events_of_type(CardEventType.TEXT_DELTA)
            assert len(text_events) == 20

            # Other sessions should NOT have received these text events
            for j in range(5):
                if j != i:
                    other_tid = f"t{j}"
                    other_text = [e for e in sessions[other_tid].events_of_type(CardEventType.TEXT_DELTA)
                                  if f"b_t{i}_" in e.payload.get("block_id", "")]
                    assert len(other_text) == 0


class TestThinkingSession:
    """Tests for thinking-phase session management."""

    def test_set_thinking_session(self):
        """set_thinking_session stores the session."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        assert orch._thinking_session is thinking

    def test_dispatch_to_thinking(self):
        """dispatch_to_thinking routes events to thinking session."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "b1", "text": "hi"})
        orch.dispatch_to_thinking(event)

        assert thinking.event_count == 1
        assert thinking.dispatched_events[0].type == CardEventType.TEXT_DELTA

    def test_dispatch_to_thinking_after_close_is_noop(self):
        """dispatch_to_thinking is noop after close."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch.close()

        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "b1", "text": "hi"})
        orch.dispatch_to_thinking(event)
        assert thinking.event_count == 0

    def test_on_plan_received_archives_thinking_session(self):
        """on_plan_received archives (not completes) the thinking session."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        # Dispatch some events to thinking first
        orch.dispatch_to_thinking(CardEvent.started())

        # Receive plan
        orch.on_plan_received([
            {"task_id": "t1", "name": "Task 1"},
            {"task_id": "t2", "name": "Task 2"},
        ])

        # Thinking session should have received ARCHIVED (not COMPLETED)
        archived = thinking.events_of_type(CardEventType.ARCHIVED)
        assert len(archived) == 1
        # Thinking session reference should be cleared
        assert orch._thinking_session is None

    def test_fallback_uses_thinking_as_fallback_session(self):
        """When entering fallback, thinking session becomes fallback session."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        orch.on_plan_received([])  # Empty -> fallback

        # Should not archive thinking (it becomes the fallback)
        assert orch.is_fallback_mode is True

        # Dispatch to task should go to thinking (now fallback)
        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "b1", "text": "x"})
        orch.dispatch_to_task("any", event)
        assert any(e.type == CardEventType.TEXT_DELTA for e in thinking.dispatched_events)

    def test_has_plan_false_before_plan(self):
        """has_plan is False before on_plan_received."""
        orch, _, _ = _make_orchestrator()
        assert orch.has_plan is False

    def test_has_plan_true_after_plan(self):
        """has_plan is True after on_plan_received."""
        orch, _, _ = _make_orchestrator()
        orch.on_plan_received([{"task_id": "t1", "name": "A"}])
        assert orch.has_plan is True

    def test_has_plan_true_after_fallback(self):
        """has_plan is True after entering fallback."""
        orch, _, _ = _make_orchestrator()
        orch.on_plan_received([])
        assert orch.has_plan is True

    def test_resolver_created_after_plan(self):
        """resolver is created after successful on_plan_received."""
        orch, _, _ = _make_orchestrator()
        assert orch.resolver is None

        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])
        assert orch.resolver is not None
        assert orch.resolver.current_task_id == "t1"


# ---------------------------------------------------------------------------
# New tests for handle_plan_update, reset, close timeout, fallback
# ---------------------------------------------------------------------------


class TestHandlePlanUpdate:
    """Tests for TaskOrchestrator.handle_plan_update()."""

    def _make_plan_event(self, entries):
        """Create a fake ACPEvent with PLAN_UPDATE type."""
        from unittest.mock import MagicMock
        from src.acp.models import ACPEventType

        evt = MagicMock()
        evt.event_type = ACPEventType.PLAN_UPDATE
        plan = MagicMock()
        plan.entries = entries
        evt.plan = plan
        return evt

    def _make_entry(self, content, status="pending"):
        from unittest.mock import MagicMock
        entry = MagicMock()
        entry.content = content
        entry.status = status
        return entry

    def test_creates_sessions_on_sufficient_entries(self):
        """handle_plan_update creates task sessions when entries >= 2."""
        orch, registry, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        entries = [self._make_entry("Task A"), self._make_entry("Task B"), self._make_entry("Task C")]
        event = self._make_plan_event(entries)
        bridge = FakeStreamBridge()

        orch.handle_plan_update(event, bridge)

        assert orch.has_plan is True
        assert registry.count == 3

    def test_no_sessions_on_insufficient_entries(self):
        """handle_plan_update does not create sessions if < 2 entries."""
        orch, registry, _ = _make_orchestrator()

        entries = [self._make_entry("Only one")]
        event = self._make_plan_event(entries)
        bridge = FakeStreamBridge()

        orch.handle_plan_update(event, bridge)

        assert orch.has_plan is False
        assert registry.count == 0

    def test_broadcasts_status_after_plan(self):
        """handle_plan_update broadcasts status changes from entries."""
        orch, _, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        # First call to establish plan
        entries = [self._make_entry("A"), self._make_entry("B")]
        event = self._make_plan_event(entries)
        bridge = FakeStreamBridge()
        orch.handle_plan_update(event, bridge)

        # Second call with status change
        import time
        time.sleep(0.15)  # exceed debounce
        entries2 = [self._make_entry("A", "completed"), self._make_entry("B", "in_progress")]
        event2 = self._make_plan_event(entries2)
        orch.handle_plan_update(event2, bridge)

        # Wait for debounced broadcast
        time.sleep(0.15)
        # Check sessions got TASK_LIST_UPDATED
        for task_id, sess in sessions.items():
            has_update = any(e.type == CardEventType.TASK_LIST_UPDATED for e in sess.dispatched_events)
            assert has_update, f"Session {task_id} did not receive TASK_LIST_UPDATED"

    def test_ignores_non_plan_events(self):
        """handle_plan_update ignores events that aren't PLAN_UPDATE."""
        from unittest.mock import MagicMock
        from src.acp.models import ACPEventType

        orch, _, _ = _make_orchestrator()
        evt = MagicMock()
        evt.event_type = ACPEventType.TEXT_CHUNK
        bridge = FakeStreamBridge()

        orch.handle_plan_update(evt, bridge)
        assert orch.has_plan is False


class TestReset:
    """Tests for TaskOrchestrator.reset()."""

    def test_reset_without_plan_resets_flags(self):
        """reset() without plan just resets internal flags."""
        orch, _, _ = _make_orchestrator()
        assert orch.has_plan is False

        orch.reset()
        assert orch.has_plan is False
        assert not orch.is_fallback_mode

    def test_reset_with_plan_closes_sessions(self):
        """reset() with plan closes all sessions."""
        orch, _, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        orch.reset()

        # Should have dispatched COMPLETED to task sessions (via close)
        for s in sessions.values():
            has_completed = any(e.type == CardEventType.COMPLETED for e in s.dispatched_events)
            assert has_completed

    def test_reset_after_fallback(self):
        """reset() after fallback resets flags without closing."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch.on_plan_received([])  # triggers fallback

        assert orch.is_fallback_mode
        orch.reset()
        assert not orch.is_fallback_mode


class TestCloseTimeout:
    """Tests for close() timeout protection."""

    def test_close_survives_blocking_bridge(self):
        """close() completes even if bridge.close_open_blocks() hangs."""
        import time

        class HangingBridge:
            def on_event(self, evt): pass
            def close_open_blocks(self):
                time.sleep(60)  # hang indefinitely
            def bind(self, d): pass

        orch = TaskOrchestrator(
            chat_id="test",
            session_creator=lambda tid: FakeSession(tid),
            bridge_factory=lambda d: HangingBridge(),
        )
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
        ])

        start = time.monotonic()
        orch.close()
        elapsed = time.monotonic() - start

        # Should complete within ~5s timeout (one bridge), not 60s
        assert elapsed < 7.0

    def test_close_survives_exception_in_dispatch(self):
        """close() handles exceptions in session.dispatch() gracefully."""
        class ExplodingSession:
            session_id = "boom"
            closed = False
            dispatched_events = []
            _armed = False
            def dispatch(self, event):
                if self._armed:
                    raise RuntimeError("boom!")
                self.dispatched_events.append(event)
            def arm(self):
                self._armed = True

        sessions_created = {}

        def _creator(task_id):
            s = ExplodingSession()
            sessions_created[task_id] = s
            return s

        orch = TaskOrchestrator(
            chat_id="test",
            session_creator=_creator,
        )
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        # Arm sessions to explode on next dispatch (during close)
        for s in sessions_created.values():
            s.arm()

        # Should not raise
        orch.close()


class TestFallbackRouting:
    """Tests for fallback routing when task_id is unknown."""

    def test_unknown_task_id_routes_to_active_session(self):
        """dispatch_to_task with unknown task_id routes to active session."""
        orch, _, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        # Mark t1 as active
        orch.resolver.mark_active("t1")

        event = CardEvent(type=CardEventType.TEXT_DELTA, payload={"block_id": "x", "text": "hi"})
        orch.dispatch_to_task("unknown_task", event)

        # Should have routed to t1 (active)
        assert any(e.type == CardEventType.TEXT_DELTA for e in sessions["t1"].dispatched_events)

    def test_fallback_mode_shows_warning(self):
        """Entering fallback mode dispatches visible warning text."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        orch.on_plan_received([])  # triggers fallback

        # Thinking (now fallback) should have received warning
        text_events = thinking.events_of_type(CardEventType.TEXT_DELTA)
        has_warning = any("任务拆分失败" in (e.payload.get("text", "") if e.payload else "") for e in text_events)
        assert has_warning


class TestArchiveThinkingSession:
    """Tests for thinking session archive with task names."""

    def test_archive_includes_task_names(self):
        """Archive summary includes task names list."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        orch.on_plan_received([
            {"task_id": "t1", "name": "分析需求"},
            {"task_id": "t2", "name": "编写代码"},
        ])

        # Check thinking session has text with task names
        text_events = thinking.events_of_type(CardEventType.TEXT_DELTA)
        all_text = " ".join(e.payload.get("text", "") if e.payload else "" for e in text_events)
        assert "分析需求" in all_text
        assert "编写代码" in all_text

    def test_archive_uses_archived_not_completed(self):
        """Archive dispatches ARCHIVED, not COMPLETED."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        archived = thinking.events_of_type(CardEventType.ARCHIVED)
        completed = thinking.events_of_type(CardEventType.COMPLETED)
        assert len(archived) == 1
        assert len(completed) == 0


class FakeStreamBridge:
    """Fake StreamBridge for testing."""

    def __init__(self):
        self.events = []

    def on_event(self, acp_event):
        self.events.append(acp_event)

    def close_open_blocks(self):
        pass

    def bind(self, dispatchable):
        pass
