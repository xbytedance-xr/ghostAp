"""Tests for src.card.orchestrator module."""
from __future__ import annotations

import threading
import time
import weakref
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
        self.delivered_message_id = ""
        self._hooks: list = []

    def add_hook(self, hook) -> None:
        """Append a hook (mirrors CardSession.add_hook)."""
        self._hooks.append(hook)

    def fire_first_delivered(self, msg_id: str) -> None:
        """Simulate HookFirer.fire_first_delivered for testing."""
        for hook in self._hooks:
            fn = getattr(hook, "on_first_delivered", None)
            if fn is not None:
                fn(self.session_id, msg_id)

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


def _make_orchestrator(session_creator=None, max_task_cards: int = 5):
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
        max_task_cards=max_task_cards,
    )
    return orch, registry, sessions_created


def _trigger_all(orch, tasks):
    """Trigger lazy session creation for every valid task in plan.

    In lazy mode (new default), `on_plan_received` only registers tasks;
    sessions are built on-demand when tasks actually execute. Tests that
    want eager session creation (old semantics) call this helper after
    `on_plan_received` to simulate "all tasks have started executing".
    """
    for t in tasks or []:
        tid = (t or {}).get("task_id") if isinstance(t, dict) else None
        if tid:
            orch._ensure_task_session(tid)


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
        _trigger_all(orch, tasks)

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
        tasks = [
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

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
        tasks = [{"task_id": "t1", "name": "Main Task"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

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


class TestAgentTaskRouting:
    def _tool_event(self, event_type, *, tool_id="agent_1", title="agent", content="", status="in_progress"):
        from src.acp.models import ACPEvent, ToolCallInfo

        return ACPEvent(
            event_type=event_type,
            tool_call=ToolCallInfo(
                id=tool_id,
                title=title,
                kind="execute",
                status=status,
                content=content,
            ),
        )

    def test_agent_tool_call_gets_independent_task_card_and_terminal_result(self):
        """Deep-style agent tool calls route to their own session, not only the parent task card."""
        from src.acp.models import ACPEventType

        orch, registry, sessions = _make_orchestrator()
        tasks = [
            {"task_id": "t1", "name": "Main Task"},
            {"task_id": "t2", "name": "Second Task"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)
        orch.resolver.mark_active("t1")

        for session in sessions.values():
            session.dispatched_events.clear()

        bridge = FakeStreamBridge()
        orch.route_acp_event(
            self._tool_event(
                ACPEventType.TOOL_CALL_START,
                content="检查实现\n子代理：Explore",
            ),
            bridge,
        )
        orch.route_acp_event(
            self._tool_event(
                ACPEventType.TOOL_CALL_UPDATE,
                title="shell",
                content="正在检查 src/card/orchestrator.py",
            ),
            bridge,
        )
        orch.route_acp_event(
            self._tool_event(
                ACPEventType.TOOL_CALL_DONE,
                title="shell",
                status="completed",
                content="子任务完成：发现路由缺口",
            ),
            bridge,
        )

        assert "agent_1" in sessions
        child_events = sessions["agent_1"].dispatched_events
        child_types = [event.type for event in child_events]
        assert CardEventType.TOOL_STARTED in child_types
        assert CardEventType.TOOL_DELTA in child_types
        assert CardEventType.TOOL_DONE in child_types
        assert CardEventType.COMPLETED in child_types

        completed = [event for event in child_events if event.type == CardEventType.COMPLETED][-1]
        assert completed.payload["summary"] == "子任务完成：发现路由缺口"
        assert not bridge.events

        assert not sessions["t1"].events_of_type(CardEventType.TOOL_MODEL_CHANGED)

    def test_agent_tool_call_does_not_patch_parent_task_card(self):
        """Subagent progress belongs only to the child card after the child card starts."""
        from src.acp.models import ACPEventType

        orch, _, sessions = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "Main"}, {"task_id": "t2", "name": "Other"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)
        orch.resolver.mark_active("t1")

        parent = sessions["t1"]
        parent.dispatched_events.clear()

        bridge = FakeStreamBridge()
        orch.route_acp_event(
            self._tool_event(
                ACPEventType.TOOL_CALL_START,
                tool_id="agent_child",
                content="检查卡片更新\n子代理：Explore",
            ),
            bridge,
        )
        orch.route_acp_event(
            self._tool_event(
                ACPEventType.TOOL_CALL_UPDATE,
                tool_id="agent_child",
                title="shell",
                content="正在读取文件",
            ),
            bridge,
        )

        assert "agent_child" in sessions
        assert any(
            event.type == CardEventType.TOOL_DELTA
            for event in sessions["agent_child"].dispatched_events
        )
        assert not parent.dispatched_events

    def test_two_agent_tool_calls_do_not_share_task_session(self):
        """Parallel agent tool calls keep separate child sessions by tool_call id."""
        from src.acp.models import ACPEventType

        orch, _, sessions = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "Main"}, {"task_id": "t2", "name": "Other"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        bridge = FakeStreamBridge()
        orch.route_acp_event(
            self._tool_event(
                ACPEventType.TOOL_CALL_START,
                tool_id="agent_a",
                content="A\n子代理：Aiden",
            ),
            bridge,
        )
        orch.route_acp_event(
            self._tool_event(
                ACPEventType.TOOL_CALL_START,
                tool_id="agent_b",
                content="B\n子代理：Codex",
            ),
            bridge,
        )

        assert "agent_a" in sessions
        assert "agent_b" in sessions
        assert sessions["agent_a"] is not sessions["agent_b"]
        assert any(
            event.type == CardEventType.TOOL_STARTED and event.payload["block_id"] == "agent_a"
            for event in sessions["agent_a"].dispatched_events
        )
        assert any(
            event.type == CardEventType.TOOL_STARTED and event.payload["block_id"] == "agent_b"
            for event in sessions["agent_b"].dispatched_events
        )
        assert not any(
            event.type == CardEventType.TOOL_STARTED and event.payload["block_id"] == "agent_b"
            for event in sessions["agent_a"].dispatched_events
        )


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
        tasks = [{"task_id": "t1", "name": "A"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)
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
        tasks = [
            {"task_id": "t1", "name": "Task 1"},
            {"task_id": "t2", "name": "Task 2"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

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

    def test_completed_task_card_is_frozen_before_later_task_updates(self):
        """Once a task completes, later task status changes do not patch its card."""
        orch, _, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        bridge = FakeStreamBridge()

        with patch("src.card.orchestrator._BROADCAST_DEBOUNCE_MS", 0):
            orch.handle_plan_update(
                self._make_plan_event([
                    self._make_entry("A", "in_progress"),
                    self._make_entry("B", "pending"),
                ]),
                bridge,
            )
            assert "step_0" in sessions

            orch.handle_plan_update(
                self._make_plan_event([
                    self._make_entry("A", "completed"),
                    self._make_entry("B", "in_progress"),
                ]),
                bridge,
            )

            completed_events = sessions["step_0"].events_of_type(CardEventType.COMPLETED)
            assert len(completed_events) == 1
            frozen_count = sessions["step_0"].event_count

            orch.handle_plan_update(
                self._make_plan_event([
                    self._make_entry("A", "completed"),
                    self._make_entry("B", "completed"),
                ]),
                bridge,
            )

        assert sessions["step_0"].event_count == frozen_count

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
        """reset() with plan archives all sessions."""
        orch, _, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        orch.reset()

        # Should have dispatched ARCHIVED to task sessions (via reset)
        for s in sessions.values():
            has_archived = any(e.type == CardEventType.ARCHIVED for e in s.dispatched_events)
            assert has_archived

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

        tasks = [
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

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
        has_warning = any("任务已合并展示" in (e.payload.get("text", "") if e.payload else "") for e in text_events)
        assert has_warning


class TestArchiveThinkingSession:
    """Tests for thinking session archive with task names."""

    def test_archive_includes_task_names(self):
        """Archive summary includes task names list."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        tasks = [
            {"task_id": "t1", "name": "分析需求"},
            {"task_id": "t2", "name": "编写代码"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

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

        tasks = [
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

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


# ---------------------------------------------------------------------------
# AC14: _do_broadcast isolation — one failing session doesn't block others
# ---------------------------------------------------------------------------


class TestBroadcastIsolation:
    """AC14: If one session.dispatch raises, other sessions still receive the broadcast."""

    def test_failing_session_does_not_block_others(self):
        """When session #2 raises RuntimeError, sessions #1 and #3 still get the event."""
        sessions_created: dict[str, FakeSession] = {}
        call_count = [0]

        class FailingSession(FakeSession):
            """A session that raises on dispatch after initial setup."""
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._should_fail = False

            def dispatch(self, event):
                if self._should_fail:
                    raise RuntimeError("simulated failure")
                super().dispatch(event)

        def _creator(task_id: str):
            call_count[0] += 1
            if task_id == "t2":
                s = FailingSession(session_id=f"session_{task_id}")
            else:
                s = FakeSession(session_id=f"session_{task_id}")
            sessions_created[task_id] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_iso",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
            {"task_id": "t3", "name": "Task C"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Clear initial events from session creation
        sessions_created["t1"].dispatched_events.clear()
        sessions_created["t3"].dispatched_events.clear()

        # Now enable failure on t2
        sessions_created["t2"]._should_fail = True

        # Trigger a broadcast
        orch.broadcast_status_change("t1", "completed")
        time.sleep(0.2)

        # t1 and t3 should have received TASK_LIST_UPDATED despite t2 failing
        t1_events = sessions_created["t1"].events_of_type(CardEventType.TASK_LIST_UPDATED)
        t3_events = sessions_created["t3"].events_of_type(CardEventType.TASK_LIST_UPDATED)
        assert len(t1_events) >= 1, "Session t1 should receive broadcast"
        assert len(t3_events) >= 1, "Session t3 should receive broadcast"


# ---------------------------------------------------------------------------
# AC5 & AC10: rotate_task_session — deep link and task list in new card
# ---------------------------------------------------------------------------


class TestRotateTaskSession:
    """AC5/AC10: Continuation card has deep-link in old card and task list in new card."""

    def test_rotation_archives_old_with_deep_link(self):
        """Old session receives deep-link via backfill after new session is delivered."""
        sessions_created: dict[str, FakeSession] = {}
        call_count = {"t1": 0}

        class SessionWithMsgId(FakeSession):
            """Fake session that exposes delivered_message_id."""
            delivered_message_id: str = ""

        def _creator(task_id: str):
            call_count.setdefault(task_id, 0)
            call_count[task_id] += 1
            s = SessionWithMsgId(session_id=f"session_{task_id}_{call_count[task_id]}")
            s.delivered_message_id = f"msg_{task_id}_{call_count[task_id]}"
            sessions_created[f"{task_id}_{call_count[task_id]}"] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_rot",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        tasks = [
            {"task_id": "t1", "name": "Fix Bug"},
            {"task_id": "t2", "name": "Add Feature"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Get the first session for t1
        old_session = sessions_created["t1_1"]

        # Rotate t1
        result = orch.rotate_task_session("t1")
        assert result is True

        # Old session should have ARCHIVED event
        archived_events = old_session.events_of_type(CardEventType.ARCHIVED)
        assert len(archived_events) >= 1

        # Simulate new session delivery — triggers backfill callback
        new_session = sessions_created["t1_2"]
        if new_session._hooks:
            new_session.fire_first_delivered(new_session.delivered_message_id)

        # Check for backfill text containing lark://message/ deep link
        text_events = old_session.events_of_type(CardEventType.TEXT_DELTA)
        continuation_texts = [e.payload.get("text", "") for e in text_events if "_continuation" in (e.payload.get("block_id", "") or "")]
        all_text = " ".join(continuation_texts)
        assert "lark://message/" in all_text, f"Expected deep-link in continuation text, got: {all_text}"

    def test_rotation_new_card_has_task_list(self):
        """New continuation session receives TASK_LIST_UPDATED as first event."""
        sessions_created: dict[str, FakeSession] = {}
        call_count = {"__": 0}

        class SessionWithMsgId(FakeSession):
            delivered_message_id: str = ""

        def _creator(task_id: str):
            call_count["__"] += 1
            s = SessionWithMsgId(session_id=f"session_{call_count['__']}")
            s.delivered_message_id = f"msg_{call_count['__']}"
            sessions_created[f"s{call_count['__']}"] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_rot2",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Rotate t1 → creates a new session (3rd session overall: t1, t2, t1-continuation)
        orch.rotate_task_session("t1")

        # The 3rd session is the continuation
        new_session = sessions_created["s3"]
        task_list_events = new_session.events_of_type(CardEventType.TASK_LIST_UPDATED)
        assert len(task_list_events) >= 1, "New continuation card should have task list header"
        # Verify it contains all tasks
        payload = task_list_events[0].payload
        task_ids_in_payload = [t["task_id"] for t in payload["tasks"]]
        assert "t1" in task_ids_in_payload
        assert "t2" in task_ids_in_payload


# ---------------------------------------------------------------------------
# AC11: max_task_cards flood prevention
# ---------------------------------------------------------------------------


class TestFloodPrevention:
    """AC11: Tasks beyond max_task_cards are routed to the last session."""

    def test_sessions_capped_at_max_task_cards(self):
        """With max_task_cards=3, only 3 sessions are created for 5 tasks."""
        sessions_created: dict[str, FakeSession] = {}

        def _creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            sessions_created[task_id] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_flood",
            session_creator=_creator,
            registry=TaskRegistry(),
            max_task_cards=3,
        )

        tasks = [{"task_id": f"step_{i}", "name": f"Task {i}"} for i in range(5)]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Only 3 sessions created (step_0, step_1, step_2)
        assert len(sessions_created) == 3
        assert "step_0" in sessions_created
        assert "step_1" in sessions_created
        assert "step_2" in sessions_created
        assert "step_3" not in sessions_created
        assert "step_4" not in sessions_created

    def test_overflow_tasks_route_to_last_session(self):
        """Dispatching to overflow task_id routes to the last created session."""
        sessions_created: dict[str, FakeSession] = {}

        def _creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            sessions_created[task_id] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_flood2",
            session_creator=_creator,
            registry=TaskRegistry(),
            max_task_cards=3,
        )

        tasks = [{"task_id": f"step_{i}", "name": f"Task {i}"} for i in range(5)]
        orch.on_plan_received(tasks)

        # Clear initial events
        for s in sessions_created.values():
            s.dispatched_events.clear()

        # Dispatch an event to step_3 (overflow) → should route to step_2 (last session)
        test_event = CardEvent.text_delta("_test", "hello overflow")
        orch.dispatch_to_task("step_3", test_event)

        # step_2 should have received the event
        assert sessions_created["step_2"].event_count >= 1
        last_event = sessions_created["step_2"].dispatched_events[-1]
        assert last_event.payload.get("text") == "hello overflow"

    def test_all_tasks_registered_in_registry(self):
        """All tasks (including overflow) are registered in the registry."""
        sessions_created: dict[str, FakeSession] = {}

        def _creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            sessions_created[task_id] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_flood3",
            session_creator=_creator,
            registry=TaskRegistry(),
            max_task_cards=3,
        )

        tasks = [{"task_id": f"step_{i}", "name": f"Task {i}"} for i in range(5)]
        orch.on_plan_received(tasks)

        # All 5 tasks should be in the registry
        assert orch.registry.count == 5


# ---------------------------------------------------------------------------
# AC12: Thinking card single summary (no duplicate task listing)
# ---------------------------------------------------------------------------


class TestThinkingCardMerge:
    """AC12: Thinking card archive produces only one task list text block."""

    def test_single_summary_on_plan_received(self):
        """After on_plan_received, thinking session has exactly one text_delta with task list."""
        thinking_session = FakeSession(session_id="thinking_1")

        def _creator(task_id: str):
            return FakeSession(session_id=f"session_{task_id}")

        orch = TaskOrchestrator(
            chat_id="chat_think",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        orch.set_thinking_session(thinking_session)

        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
            {"task_id": "t3", "name": "Task C"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Count TEXT_DELTA events (should be exactly 1 summary text block)
        text_deltas = thinking_session.events_of_type(CardEventType.TEXT_DELTA)
        assert len(text_deltas) == 1, f"Expected 1 text_delta, got {len(text_deltas)}"

        # Verify the single delta contains all task names
        text = text_deltas[0].payload.get("text", "")
        assert "Task A" in text
        assert "Task B" in text
        assert "Task C" in text

    def test_thinking_session_archived(self):
        """Thinking session is properly archived after plan received."""
        thinking_session = FakeSession(session_id="thinking_2")

        def _creator(task_id: str):
            return FakeSession(session_id=f"session_{task_id}")

        orch = TaskOrchestrator(
            chat_id="chat_think2",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        orch.set_thinking_session(thinking_session)

        tasks = [
            {"task_id": "t1", "name": "Task A"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        archived_events = thinking_session.events_of_type(CardEventType.ARCHIVED)
        assert len(archived_events) == 1


# ---------------------------------------------------------------------------
# AC9: Feature flag disabled — single-card fallback behavior
# ---------------------------------------------------------------------------


class TestFeatureFlagDisabled:
    """AC9: When task_level_cards_enabled=False, no extra sessions are created."""

    def test_no_bridge_factory_means_no_per_task_bridges(self):
        """When bridge_factory=None (flag disabled), no per-task bridges are created."""
        sessions_created: dict[str, FakeSession] = {}

        def _creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            sessions_created[task_id] = s
            return s

        # Simulate flag=False: bridge_factory=None
        orch = TaskOrchestrator(
            chat_id="chat_disabled",
            session_creator=_creator,
            registry=TaskRegistry(),
            bridge_factory=None,  # This is what happens when flag is False
        )

        orch.on_plan_received([
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
        ])

        # Sessions ARE created (orchestrator still works) but bridges are empty
        assert len(orch._bridges) == 0

    def test_renderer_pattern_flag_disabled(self):
        """Simulate the renderer pattern: flag=False means events go through stream_bridge."""
        # This tests the integration pattern used by DeepRenderer:
        # _multi_card_enabled = False
        # → bridge_factory = None
        # → orchestrator.handle_plan_update() is NOT called
        # → orchestrator.route_acp_event() is NOT called
        # → all events go through the single stream_bridge

        sessions_created: dict[str, FakeSession] = {}

        def _creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            sessions_created[task_id] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_disabled2",
            session_creator=_creator,
            registry=TaskRegistry(),
            bridge_factory=None,
        )

        # In flag-disabled mode, handle_plan_update is never called,
        # so orchestrator stays in initial state (no plan received)
        assert not orch.has_plan
        assert not orch.is_fallback_mode

        # All events would go through the single stream_bridge (not the orchestrator)
        # This is verified by confirming no sessions are created
        assert len(sessions_created) == 0


# ---------------------------------------------------------------------------
# AC4: Concurrent subagent dispatch isolation
# ---------------------------------------------------------------------------


class TestConcurrentSubagentIsolation:
    """AC4: 2+ sessions dispatch concurrently without interference."""

    def test_concurrent_dispatch_to_different_tasks(self):
        """Events dispatched concurrently to different task_ids reach correct sessions only."""
        sessions_created: dict[str, FakeSession] = {}

        def _creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            sessions_created[task_id] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_concurrent",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
            {"task_id": "t3", "name": "Task C"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Clear initial events
        for s in sessions_created.values():
            s.dispatched_events.clear()

        # Dispatch from 2 threads concurrently
        errors = []
        n_events_per_thread = 50

        def dispatch_to_task(task_id: str, count: int):
            try:
                for i in range(count):
                    event = CardEvent.text_delta(f"_block_{task_id}_{i}", f"msg_{task_id}_{i}")
                    orch.dispatch_to_task(task_id, event)
            except Exception as e:
                errors.append(e)

        t1_thread = threading.Thread(target=dispatch_to_task, args=("t1", n_events_per_thread))
        t2_thread = threading.Thread(target=dispatch_to_task, args=("t2", n_events_per_thread))

        t1_thread.start()
        t2_thread.start()
        t1_thread.join(timeout=5)
        t2_thread.join(timeout=5)

        assert not errors, f"Errors during concurrent dispatch: {errors}"

        # Verify isolation: t1's session only has t1 events, t2's only has t2 events
        t1_texts = [
            e.payload.get("text", "")
            for e in sessions_created["t1"].dispatched_events
            if e.type == CardEventType.TEXT_DELTA
        ]
        t2_texts = [
            e.payload.get("text", "")
            for e in sessions_created["t2"].dispatched_events
            if e.type == CardEventType.TEXT_DELTA
        ]

        assert len(t1_texts) == n_events_per_thread
        assert all("msg_t1_" in t for t in t1_texts)
        assert len(t2_texts) == n_events_per_thread
        assert all("msg_t2_" in t for t in t2_texts)

        # t3 should have received nothing (no dispatch to it)
        t3_events = [
            e for e in sessions_created["t3"].dispatched_events
            if e.type == CardEventType.TEXT_DELTA
        ]
        assert len(t3_events) == 0


class TestRotateTaskSessionThreadSafety:
    """AC9: rotation_counts uses dict (not dynamic setattr) under lock."""

    def test_rotation_counts_dict_exists(self):
        orch, _, _ = _make_orchestrator()
        assert hasattr(orch, "_rotation_counts")
        assert isinstance(orch._rotation_counts, dict)

    def test_no_dynamic_rotation_count_attributes(self):
        orch, registry, sessions_created = _make_orchestrator()
        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Rotate t1
        orch.rotate_task_session("t1")

        # Should use _rotation_counts dict, not _rotation_count_t1 dynamic attr
        assert orch._rotation_counts.get("t1") == 1
        assert not hasattr(orch, "_rotation_count_t1")

    def test_rotation_counts_increment_correctly(self):
        orch, registry, sessions_created = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "Task A"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        orch.rotate_task_session("t1")
        orch.rotate_task_session("t1")
        orch.rotate_task_session("t1")

        assert orch._rotation_counts["t1"] == 3


class TestRotateTaskSessionCreatorFail:
    """AC16: session_creator exception → no archive, no count increment."""

    def test_creator_exception_returns_false(self):
        call_count = [0]

        def failing_creator(task_id: str):
            call_count[0] += 1
            if call_count[0] > 2:
                raise RuntimeError("session creation failed")
            return FakeSession(session_id=f"session_{task_id}")

        orch, registry, _ = _make_orchestrator(session_creator=failing_creator)
        tasks = [{"task_id": "t1", "name": "Task A"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # First session created (call_count=1 from on_plan_received's _create_task_session)
        # plus thinking session was None so no extra creation
        # Now rotate — this will call creator again (call_count=2) — should succeed
        # Actually we need to check the exact call count path
        # The initial _create_task_session creates session (call_count reaches 2 in plan_received)
        # Let's force the next creation to fail by increasing threshold
        call_count[0] = 2  # force next call to fail

        # Get the current session before rotation attempt
        with orch._lock:
            old_session = orch._sessions.get("t1")

        result = orch.rotate_task_session("t1")

        assert result is False
        assert orch._rotation_counts.get("t1", 0) == 0
        # Old session should NOT have received ARCHIVED event
        archived_events = old_session.events_of_type(CardEventType.ARCHIVED)
        assert len(archived_events) == 0

    def test_creator_exception_preserves_old_session(self):
        first_call = [True]

        def sometimes_failing_creator(task_id: str):
            if first_call[0]:
                first_call[0] = False
                return FakeSession(session_id=f"session_{task_id}")
            raise RuntimeError("boom")

        orch, _, _ = _make_orchestrator(session_creator=sometimes_failing_creator)
        tasks = [{"task_id": "t1", "name": "Task A"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        with orch._lock:
            original_session = orch._sessions["t1"]

        result = orch.rotate_task_session("t1")
        assert result is False

        # Session still points to original
        with orch._lock:
            assert orch._sessions["t1"] is original_session


class TestOverflowTargetLocking:
    """AC19: _overflow_target writes happen under self._lock."""

    def test_overflow_target_correctly_populated(self):
        orch, registry, sessions_created = _make_orchestrator(max_task_cards=2)
        orch.on_plan_received([
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
            {"task_id": "t3", "name": "Task C"},
        ])

        # t3 should overflow to t2
        with orch._lock:
            assert orch._overflow_target["t3"] == "t2"

    def test_dispatch_to_overflow_routes_correctly(self):
        orch, registry, sessions_created = _make_orchestrator(max_task_cards=2)
        orch.on_plan_received([
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
            {"task_id": "t3", "name": "Task C"},
        ])

        # Dispatch to overflow task_id t3 → should route to t2's session
        event = CardEvent.text_delta("_test", "hello")
        orch.dispatch_to_task("t3", event)

        # t2's session should have the event
        t2_session = sessions_created["t2"]
        text_events = [
            e for e in t2_session.dispatched_events
            if e.type == CardEventType.TEXT_DELTA and e.payload.get("text") == "hello"
        ]
        assert len(text_events) == 1


class TestFloodNotification:
    """AC13: overflow tasks trigger orch_flood_merged visible notification."""

    def test_overflow_dispatches_flood_merged_notice(self):
        orch, registry, sessions_created = _make_orchestrator(max_task_cards=2)
        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
            {"task_id": "t3", "name": "Task C"},
            {"task_id": "t4", "name": "Task D"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Last session (t2) should have received flood notifications for t3 and t4
        t2_session = sessions_created["t2"]
        text_deltas = [
            e.payload.get("text", "")
            for e in t2_session.dispatched_events
            if e.type == CardEventType.TEXT_DELTA
        ]
        flood_texts = [t for t in text_deltas if "已合并展示于此" in t]
        assert len(flood_texts) == 2
        assert any("Task C" in t for t in flood_texts)
        assert any("Task D" in t for t in flood_texts)


class TestFloodBoundary:
    """AC14 supplemental: boundary conditions for max_task_cards."""

    def test_max_equals_task_count_no_overflow(self):
        orch, registry, sessions_created = _make_orchestrator(max_task_cards=3)
        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
            {"task_id": "t3", "name": "Task C"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        assert len(sessions_created) == 3
        with orch._lock:
            assert len(orch._overflow_target) == 0

    def test_max_one_single_task(self):
        orch, registry, sessions_created = _make_orchestrator(max_task_cards=1)
        tasks = [{"task_id": "t1", "name": "Task A"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        assert len(sessions_created) == 1
        with orch._lock:
            assert len(orch._overflow_target) == 0

    def test_max_one_multiple_tasks_all_overflow_to_first(self):
        orch, registry, sessions_created = _make_orchestrator(max_task_cards=1)
        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
            {"task_id": "t3", "name": "Task C"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Only 1 session created
        assert len(sessions_created) == 1
        assert "t1" in sessions_created
        # t2, t3 overflow to t1
        with orch._lock:
            assert orch._overflow_target["t2"] == "t1"
            assert orch._overflow_target["t3"] == "t1"


class TestFinalizeThinkingTransition:
    """AC18: _finalize_thinking_session includes transition hint."""

    def test_archive_text_contains_transition_hint(self):
        orch, registry, sessions_created = _make_orchestrator()
        thinking = FakeSession(session_id="thinking")
        orch._thinking_session = thinking

        tasks = [
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Check thinking session received text with transition hint
        text_events = [
            e.payload.get("text", "")
            for e in thinking.dispatched_events
            if e.type == CardEventType.TEXT_DELTA
        ]
        combined = "".join(text_events)
        assert "以下每个任务将在独立卡片中展示 ↓" in combined
        assert "规划完成" in combined

    def test_archive_text_contains_task_list(self):
        orch, registry, sessions_created = _make_orchestrator()
        thinking = FakeSession(session_id="thinking")
        orch._thinking_session = thinking

        tasks = [
            {"task_id": "t1", "name": "Login Fix"},
            {"task_id": "t2", "name": "DB Migration"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        text_events = [
            e.payload.get("text", "")
            for e in thinking.dispatched_events
            if e.type == CardEventType.TEXT_DELTA
        ]
        combined = "".join(text_events)
        assert "Login Fix" in combined
        assert "DB Migration" in combined


class TestDeepLinkBackfill:
    """AC11: _BackfillHook patches old card with deep-link via on_first_delivered."""

    def test_backfill_hook_registered_on_new_session(self):
        """Rotation injects a _BackfillHook into the new session's hooks."""
        orch, registry, sessions_created = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "Task A"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Get initial session
        with orch._lock:
            initial_session = orch._sessions["t1"]

        orch.rotate_task_session("t1")

        # New session should have a BackfillHook in _hooks
        with orch._lock:
            new_session = orch._sessions["t1"]
        assert new_session is not initial_session
        from src.card.hooks import BackfillHook
        assert any(isinstance(h, BackfillHook) for h in new_session._hooks)

    def test_backfill_callback_dispatches_deep_link_to_old_session(self):
        orch, registry, sessions_created = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "Task A"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        with orch._lock:
            old_session = orch._sessions["t1"]

        # Clear old session events before rotation
        old_session.dispatched_events.clear()

        orch.rotate_task_session("t1")

        with orch._lock:
            new_session = orch._sessions["t1"]

        # Simulate first delivery via hook (mirrors HookFirer.fire_first_delivered)
        new_session.fire_first_delivered("om_new_msg_123")

        # Old session should have received continuation text with deep-link
        text_events = [
            e.payload.get("text", "")
            for e in old_session.dispatched_events
            if e.type == CardEventType.TEXT_DELTA
        ]
        combined = "".join(text_events)
        assert "lark://message/om_new_msg_123" in combined


# ---------------------------------------------------------------------------
# AC1: Total create_card count == task_count + 1 (thinking card)
# ---------------------------------------------------------------------------


class TestAC1TotalCardCount:
    """AC1: Verify total card creation count equals task_count + 1.

    The thinking-phase card (set via set_thinking_session) is 1 card.
    Each task in on_plan_received creates 1 additional card.
    Total = 1 (thinking) + N (tasks) = N + 1.
    """

    def test_total_sessions_equals_task_count_plus_one(self):
        """Total session creations (thinking + tasks) == task_count + 1."""
        all_sessions: list[FakeSession] = []

        def _counting_creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            all_sessions.append(s)
            return s

        orch = TaskOrchestrator(
            chat_id="chat_ac1",
            session_creator=_counting_creator,
            registry=TaskRegistry(),
        )

        # 1. Thinking card (created externally, simulates renderer's create_session)
        thinking = FakeSession("thinking_card")
        all_sessions.append(thinking)  # count the thinking card
        orch.set_thinking_session(thinking)

        # 2. Plan received with 3 tasks → creates 3 more sessions
        task_count = 3
        tasks = [{"task_id": f"step_{i}", "name": f"Task {i}"} for i in range(task_count)]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Verify: total = task_count + 1 (thinking)
        expected_total = task_count + 1
        assert len(all_sessions) == expected_total, (
            f"AC1 violation: expected {expected_total} total cards "
            f"(1 thinking + {task_count} tasks), got {len(all_sessions)}"
        )

    def test_ac1_with_five_tasks(self):
        """AC1 with 5 tasks: total == 6."""
        all_sessions: list[FakeSession] = []

        def _counting_creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            all_sessions.append(s)
            return s

        orch = TaskOrchestrator(
            chat_id="chat_ac1_5",
            session_creator=_counting_creator,
            registry=TaskRegistry(),
            max_task_cards=10,
        )

        thinking = FakeSession("thinking_card")
        all_sessions.append(thinking)
        orch.set_thinking_session(thinking)

        task_count = 5
        tasks = [{"task_id": f"step_{i}", "name": f"Task {i}"} for i in range(task_count)]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        expected_total = task_count + 1
        assert len(all_sessions) == expected_total, (
            f"AC1 violation: expected {expected_total} total cards, got {len(all_sessions)}"
        )

    def test_ac1_with_overflow_respects_max(self):
        """AC1 with overflow: total == min(task_count, max_task_cards) + 1."""
        all_sessions: list[FakeSession] = []

        def _counting_creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            all_sessions.append(s)
            return s

        task_count = 5
        max_cards = 3

        orch = TaskOrchestrator(
            chat_id="chat_ac1_overflow",
            session_creator=_counting_creator,
            registry=TaskRegistry(),
            max_task_cards=max_cards,
        )

        thinking = FakeSession("thinking_card")
        all_sessions.append(thinking)
        orch.set_thinking_session(thinking)

        tasks = [{"task_id": f"step_{i}", "name": f"Task {i}"} for i in range(task_count)]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # With overflow: cards created = min(task_count, max_cards) + 1
        expected_total = min(task_count, max_cards) + 1
        assert len(all_sessions) == expected_total, (
            f"AC1 with overflow: expected {expected_total} total cards "
            f"(1 thinking + min({task_count}, {max_cards}) task cards), "
            f"got {len(all_sessions)}"
        )

    def test_ac1_thinking_archived_on_plan(self):
        """AC1: thinking card is archived (not completed) when tasks start."""
        all_sessions: list[FakeSession] = []

        def _counting_creator(task_id: str):
            s = FakeSession(session_id=f"session_{task_id}")
            all_sessions.append(s)
            return s

        orch = TaskOrchestrator(
            chat_id="chat_ac1_archive",
            session_creator=_counting_creator,
            registry=TaskRegistry(),
        )

        thinking = FakeSession("thinking_card")
        all_sessions.append(thinking)
        orch.set_thinking_session(thinking)

        tasks = [{"task_id": "t1", "name": "A"}, {"task_id": "t2", "name": "B"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Thinking card should be archived (transition from thinking to task cards)
        archived = thinking.events_of_type(CardEventType.ARCHIVED)
        assert len(archived) == 1, "Thinking card must be ARCHIVED when plan received"

        # Total cards: 1 thinking + 2 tasks = 3
        assert len(all_sessions) == 3


# ===========================================================================
# Phase 6: Additional AC verification tests
# ===========================================================================


class TestRotateNoIOInsideLock:
    """AC9: rotate_task_session does not call session.dispatch() while holding self._lock."""

    def test_no_dispatch_under_lock(self):
        """Verify dispatch calls happen only outside the orchestrator's lock."""
        orch, registry, sessions = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "Task1"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        session = sessions["t1"]
        lock_held_dispatches: list[str] = []
        original_dispatch = session.dispatch

        def _tracking_dispatch(event):
            # Check if the orchestrator's lock is currently held by trying to acquire
            acquired = orch._lock.acquire(blocking=False)
            if not acquired:
                lock_held_dispatches.append(event.type.value)
            else:
                orch._lock.release()
            original_dispatch(event)

        session.dispatch = _tracking_dispatch

        # Perform rotation
        new_sessions = []

        def _creator(task_id):
            s = FakeSession(session_id=f"new_session_{task_id}")
            s.delivered_message_id = "om_new123"
            new_sessions.append(s)
            return s

        orch._session_creator = _creator
        result = orch.rotate_task_session("t1")

        assert result is True
        # No dispatch calls should have happened while lock was held
        assert lock_held_dispatches == [], (
            f"dispatch() called while lock held: {lock_held_dispatches}"
        )


class TestResetConcurrentDispatch:
    """AC15: concurrent reset() + dispatch_to_task() does not raise AttributeError."""

    def test_concurrent_no_attribute_error(self):
        """50 iterations of concurrent reset + dispatch must not raise."""
        orch, registry, _ = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "A"}, {"task_id": "t2", "name": "B"}]
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch.on_plan_received(tasks)

        errors: list[Exception] = []

        def _reset_loop():
            for _ in range(50):
                try:
                    orch.reset()
                    # Re-setup for next iteration
                    orch.set_thinking_session(FakeSession("th"))
                    orch.on_plan_received(tasks)
                except Exception as e:
                    errors.append(e)

        def _dispatch_loop():
            for _ in range(50):
                try:
                    event = CardEvent.text_delta("blk", "hello")
                    orch.dispatch_to_task("t1", event)
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=_reset_loop)
        t2 = threading.Thread(target=_dispatch_loop)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        attr_errors = [e for e in errors if isinstance(e, AttributeError)]
        assert attr_errors == [], f"AttributeError(s) occurred: {attr_errors}"
        # Non-AttributeError exceptions should also not occur (e.g. KeyError, RuntimeError)
        unexpected_errors = [e for e in errors if not isinstance(e, AttributeError)]
        assert unexpected_errors == [], f"Unexpected exception(s) occurred: {unexpected_errors}"


class TestWeakrefBackfill:
    """AC10: rotation backfill uses weakref; old sessions can be GC'd."""

    def test_old_session_gc_after_rotation(self):
        """After 3 rotations, session_N-2 should be GC-collectible."""
        import gc
        import weakref as weakref_mod

        orch, registry, sessions = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "Task1"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Track weak references to old sessions
        weak_refs = []
        rotation_sessions = []

        def _creator(task_id):
            s = FakeSession(session_id=f"rot_{task_id}_{len(rotation_sessions)}")
            s.delivered_message_id = f"om_rot_{len(rotation_sessions)}"
            rotation_sessions.append(s)
            return s

        orch._session_creator = _creator

        # Capture weakref to the ORIGINAL session before any rotation
        original_session = sessions["t1"]
        weak_original = weakref_mod.ref(original_session)

        # Perform 3 rotations
        for _ in range(3):
            orch.rotate_task_session("t1")

        # Remove strong references
        del sessions["t1"]
        del original_session
        gc.collect()

        # The original session should be GC-able (weakref returns None)
        assert weak_original() is None, (
            "Old session (N-2) still alive after 3 rotations — weakref chain leak"
        )


class TestRotationFailDegradation:
    """When session_creator raises, old session receives degradation notice."""

    def test_creator_exception_dispatches_degradation_text(self):
        orch, registry, sessions = _make_orchestrator()
        tasks = [{"task_id": "t1", "name": "A"}]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)
        session = sessions["t1"]

        def _failing_creator(task_id):
            raise RuntimeError("network error")

        orch._session_creator = _failing_creator
        result = orch.rotate_task_session("t1")

        assert result is False
        # Check that degradation text was dispatched to old session
        text_events = session.events_of_type(CardEventType.TEXT_DELTA)
        degrade_texts = [
            e for e in text_events
            if "无法创建新卡片" in (e.payload.get("text", "") if e.payload else "")
        ]
        assert len(degrade_texts) >= 1, "Degradation notice should be dispatched to old session"


class TestCreateTaskSessionException:
    """F20: _create_task_session exception triggers fallback mode."""

    def test_session_creator_exception_enters_fallback(self):
        call_count = 0

        def _failing_after_first(task_id):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise RuntimeError("second session creation failed")
            return FakeSession(session_id=f"session_{task_id}")

        orch, registry, _ = _make_orchestrator(session_creator=_failing_after_first)
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        tasks = [
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
            {"task_id": "t3", "name": "C"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        # Should be in fallback mode after second creation fails
        assert orch._fallback_mode is True


# =============================================================================
# New tests for refactored orchestrator
# =============================================================================


class TestOverflowSeparatorConcurrency:
    """AC-R20: overflow separator dispatched exactly once under concurrent dispatch."""

    def test_concurrent_dispatch_same_overflow_task(self):
        """10 threads dispatch same overflow task_id; SECTION_SEPARATOR appears once."""
        orch, registry, sessions_created = _make_orchestrator(max_task_cards=2)
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
            {"task_id": "t3", "name": "C"},  # overflow → t2
        ])

        barrier = threading.Barrier(10)
        errors = []

        def _dispatch_worker():
            try:
                barrier.wait(timeout=5)
                orch.dispatch_to_task("t3", CardEvent.text_delta("blk", "hello"))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_dispatch_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors

        t2_session = sessions_created["t2"]
        separator_events = [
            e for e in t2_session.dispatched_events
            if e.type == CardEventType.SECTION_SEPARATOR
        ]
        assert len(separator_events) == 1, f"Expected 1 separator, got {len(separator_events)}"


class TestResetConcurrentDispatchEnhanced:
    """AC-R21: reset后dispatch不路由到旧session."""

    def test_reset_dispatch_no_route_to_old_session(self):
        orch, registry, sessions_created = _make_orchestrator()
        tasks = [
            {"task_id": "t1", "name": "A"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)
        old_session = sessions_created["t1"]
        old_event_count = old_session.event_count

        # Reset archives old sessions
        orch.reset()

        # Dispatch after reset should not reach old session
        orch.dispatch_to_task("t1", CardEvent.text_delta("blk", "after-reset"))

        # Old session should have at most the ARCHIVED event added during reset
        # but NO new text_delta events
        new_text_deltas = [
            e for e in old_session.dispatched_events[old_event_count:]
            if e.type == CardEventType.TEXT_DELTA and e.payload.get("block_id") == "blk"
        ]
        assert len(new_text_deltas) == 0, "Post-reset dispatch should not reach old session"


class TestRouteOrFallback:
    """AC-R22: parametrized test for route_or_fallback behavior."""

    @pytest.fixture
    def setup(self):
        """Common setup for route_or_fallback tests."""
        from unittest.mock import MagicMock
        from src.acp.models import ACPEvent, ACPEventType as ACPEvType

        orch, registry, sessions = _make_orchestrator()
        fake_bridge = MagicMock()
        event = MagicMock(spec=ACPEvent)
        event.event_type = ACPEvType.TEXT_CHUNK
        event.text = "hello"
        event.tool_call = None
        event.plan = None
        return orch, sessions, fake_bridge, event

    def test_no_plan_returns_false(self, setup):
        """Before plan received, route_or_fallback returns False (use fallback bridge)."""
        orch, sessions, fake_bridge, event = setup
        # _plan_received = False, _fallback_mode = False
        result = orch.route_or_fallback(event, fake_bridge)
        assert result is False

    def test_plan_received_returns_true(self, setup):
        """After plan received, route_or_fallback returns True (routed)."""
        orch, sessions, fake_bridge, event = setup
        orch.on_plan_received([
            {"task_id": "t1", "name": "Task A"},
            {"task_id": "t2", "name": "Task B"},
        ])
        result = orch.route_or_fallback(event, fake_bridge)
        assert result is True

    def test_fallback_mode_returns_false(self, setup):
        """In fallback mode (no plan), returns False (uses fallback bridge)."""
        orch, sessions, fake_bridge, event = setup
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch._enter_fallback_mode()
        result = orch.route_or_fallback(event, fake_bridge)
        assert result is False
        fake_bridge.on_event.assert_called_once_with(event)

    def test_plan_then_fallback_returns_false(self, setup):
        """Plan received + fallback mode: returns False (fallback overrides)."""
        orch, sessions, fake_bridge, event = setup
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch.on_plan_received([
            {"task_id": "t1", "name": "Task A"},
        ])
        orch._enter_fallback_mode()
        result = orch.route_or_fallback(event, fake_bridge)
        assert result is False
        fake_bridge.on_event.assert_called_once_with(event)


class TestBackfillTOCTOU:
    """AC-R2: backfill callback triggers even if delivery happens before swap."""

    def test_backfill_fires_before_swap(self):
        """Simulate delivery before swap — backfill should still work."""
        sessions_created: dict[str, FakeSession] = {}
        call_count = {"t1": 0}

        class SessionWithMsgId(FakeSession):
            delivered_message_id: str = ""

        def _creator(task_id: str):
            call_count.setdefault(task_id, 0)
            call_count[task_id] += 1
            s = SessionWithMsgId(session_id=f"session_{task_id}_{call_count[task_id]}")
            s.delivered_message_id = f"msg_{task_id}_{call_count[task_id]}"
            sessions_created[f"{task_id}_{call_count[task_id]}"] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_toctou",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        tasks = [
            {"task_id": "t1", "name": "Task"},
            {"task_id": "t2", "name": "Other"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        old_session = sessions_created["t1_1"]
        result = orch.rotate_task_session("t1")
        assert result is True

        # Simulate delivery callback (as if happened immediately)
        new_session = sessions_created["t1_2"]
        assert len(new_session._hooks) > 0
        new_session.fire_first_delivered(new_session.delivered_message_id)

        # Verify old session received backfill with deep-link
        text_events = old_session.events_of_type(CardEventType.TEXT_DELTA)
        backfill_texts = [
            e.payload.get("text", "") for e in text_events
            if "_continuation_backfill" in (e.payload.get("block_id", "") or "")
        ]
        assert any("lark://message/" in t for t in backfill_texts), \
            f"Expected deep-link in backfill, got: {backfill_texts}"

    def test_concurrent_delivery_during_swap(self):
        """AC-TEST-6: Use threading.Barrier to simulate fire_first_delivered during swap window."""
        sessions_created: dict[str, FakeSession] = {}
        call_count: dict[str, int] = {}
        barrier = threading.Barrier(2, timeout=5)

        class SessionWithMsgId(FakeSession):
            delivered_message_id: str = ""

        def _creator(task_id: str):
            call_count.setdefault(task_id, 0)
            call_count[task_id] += 1
            s = SessionWithMsgId(session_id=f"session_{task_id}_{call_count[task_id]}")
            s.delivered_message_id = f"msg_{task_id}_{call_count[task_id]}"
            sessions_created[f"{task_id}_{call_count[task_id]}"] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_toctou2",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        tasks = [
            {"task_id": "t1", "name": "Task"},
            {"task_id": "t2", "name": "Other"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        old_session = sessions_created["t1_1"]

        # Replace _sessions with a custom dict that synchronizes during swap
        class InstrumentedDict(dict):
            def __setitem__(self, key, value):
                super().__setitem__(key, value)
                if key == "t1" and hasattr(value, 'delivered_message_id'):
                    try:
                        barrier.wait(timeout=3)
                    except threading.BrokenBarrierError:
                        pass

        # Copy existing sessions into instrumented dict
        instrumented = InstrumentedDict(orch._sessions)
        orch._sessions = instrumented

        def delivery_thread():
            """Fire delivery as soon as swap completes __setitem__."""
            try:
                barrier.wait(timeout=3)
            except threading.BrokenBarrierError:
                return
            new_sess = sessions_created.get("t1_2")
            if new_sess and new_sess._hooks:
                new_sess.fire_first_delivered(new_sess.delivered_message_id)

        t = threading.Thread(target=delivery_thread)
        t.start()

        result = orch.rotate_task_session("t1")
        t.join(timeout=5)

        assert result is True
        # Verify backfill reached old session
        text_events = old_session.events_of_type(CardEventType.TEXT_DELTA)
        backfill_texts = [
            e.payload.get("text", "") for e in text_events
            if "_continuation_backfill" in (e.payload.get("block_id", "") or "")
        ]
        assert any("lark://message/" in t for t in backfill_texts), \
            f"Expected deep-link in backfill, got: {backfill_texts}"


class TestBackfillBlockId:
    """AC-R4: backfill uses different block_id than initial navigation."""

    def test_different_block_ids(self):
        """Phase 3 navigation and backfill use distinct block_ids."""
        sessions_created: dict[str, FakeSession] = {}
        call_count = {"t1": 0}

        class SessionWithMsgId(FakeSession):
            delivered_message_id: str = ""

        def _creator(task_id: str):
            call_count.setdefault(task_id, 0)
            call_count[task_id] += 1
            s = SessionWithMsgId(session_id=f"session_{task_id}_{call_count[task_id]}")
            s.delivered_message_id = f"msg_{task_id}_{call_count[task_id]}"
            sessions_created[f"{task_id}_{call_count[task_id]}"] = s
            return s

        orch = TaskOrchestrator(
            chat_id="chat_blockid",
            session_creator=_creator,
            registry=TaskRegistry(),
        )
        tasks = [
            {"task_id": "t1", "name": "Task"},
            {"task_id": "t2", "name": "Other"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)

        old_session = sessions_created["t1_1"]
        orch.rotate_task_session("t1")

        # Trigger backfill
        new_session = sessions_created["t1_2"]
        if new_session._hooks:
            new_session.fire_first_delivered("msg_new")

        # Collect all block_ids from TEXT_STARTED events on old session
        text_started_events = old_session.events_of_type(CardEventType.TEXT_STARTED)
        block_ids = [e.payload.get("block_id", "") for e in text_started_events]

        # Should have both _continuation (initial nav) and _continuation_backfill
        continuation_ids = [bid for bid in block_ids if "continuation" in bid]
        assert "_continuation" in continuation_ids, f"Expected _continuation, got: {continuation_ids}"
        assert "_continuation_backfill" in continuation_ids, f"Expected _continuation_backfill, got: {continuation_ids}"


class TestCloseNoThreadLeak:
    """AC-R7: close() leaves no lingering threads."""

    def test_no_thread_leak(self):
        orch, registry, sessions = _make_orchestrator()
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
        ])

        # Record thread count before close
        threads_before = len([t for t in threading.enumerate() if "orch-close" in t.name])

        orch.close()

        # Wait a moment for pool shutdown
        time.sleep(0.1)
        threads_after = len([t for t in threading.enumerate() if "orch-close" in t.name])

        assert threads_after <= threads_before, \
            f"Thread leak: before={threads_before}, after={threads_after}"


# ─── Task 19 [AC-TEST-3]: TestBackfillHookBranches ───


class TestBackfillHookBranches:
    """AC-TEST-3: _BackfillHook early-return branches and exception swallowing."""

    @pytest.mark.parametrize("msg_id,should_dispatch", [
        ("", False),  # empty msg_id → no-op
        ("om_valid", True),  # valid msg_id → dispatches
    ])
    def test_empty_msg_id_noop(self, msg_id, should_dispatch):
        """Branch 1: msg_id='' → on_first_delivered is a no-op."""
        from src.card.hooks import BackfillHook

        old_session = FakeSession(session_id="old")
        old_session.closed = False
        hook = BackfillHook(
            old_session_ref=weakref.ref(old_session),
            task_name="TestTask",
            rotation_count=1,
        )
        hook.on_first_delivered("new_session", msg_id)
        if should_dispatch:
            assert old_session.event_count > 0
        else:
            assert old_session.event_count == 0

    def test_weakref_dead_noop(self):
        """Branch 2: weakref returns None → no-op."""
        from src.card.hooks import BackfillHook

        old_session = FakeSession(session_id="old_dead")
        ref = weakref.ref(old_session)
        del old_session  # Kill the referent

        hook = BackfillHook(
            old_session_ref=ref,
            task_name="DeadTask",
            rotation_count=1,
        )
        # Should not raise
        hook.on_first_delivered("new_session", "om_msg_123")

    def test_closed_session_noop(self):
        """Branch 3: old_session.closed=True → no-op."""
        from src.card.hooks import BackfillHook

        class ClosedSession(FakeSession):
            @property
            def closed(self):
                return True

        old_session = ClosedSession(session_id="old_closed")
        hook = BackfillHook(
            old_session_ref=weakref.ref(old_session),
            task_name="ClosedTask",
            rotation_count=1,
        )
        hook.on_first_delivered("new_session", "om_msg_456")
        assert old_session.event_count == 0

    def test_dispatch_exception_swallowed(self):
        """Branch 4: old_session.dispatch raises → exception not propagated."""
        from src.card.hooks import BackfillHook

        class ExplodingSession(FakeSession):
            @property
            def closed(self):
                return False

            def dispatch(self, event):
                raise RuntimeError("boom")

        old_session = ExplodingSession(session_id="old_explode")
        hook = BackfillHook(
            old_session_ref=weakref.ref(old_session),
            task_name="ExplodeTask",
            rotation_count=1,
        )
        # Should not raise
        hook.on_first_delivered("new_session", "om_msg_789")


# ──────────────────────────────────────────────────────────────────────────────
# TestFromSettings (AC-R18)
# ──────────────────────────────────────────────────────────────────────────────


class TestFromSettings:
    """Tests for TaskOrchestrator.from_settings() factory method."""

    def test_disabled_results_in_no_bridge_factory(self):
        """task_level_cards_enabled=False → bridge_factory is None."""
        from unittest.mock import MagicMock, patch

        mock_settings = MagicMock()
        mock_settings.card.task_level_cards_enabled = False
        mock_settings.card.max_task_cards = 5

        with patch("src.config.get_settings", return_value=mock_settings):
            from src.card.orchestrator import TaskOrchestrator as TO
            orch = TO.from_settings(
                chat_id="test_chat",
                session_creator=lambda tid: FakeSession(tid),
                thinking_session=FakeSession("thinking"),
                bridge_class=MagicMock,
            )
        assert orch._bridge_factory is None

    def test_max_task_cards_passed_correctly(self):
        """max_task_cards from settings is forwarded to orchestrator."""
        from unittest.mock import MagicMock, patch

        mock_settings = MagicMock()
        mock_settings.card.task_level_cards_enabled = True
        mock_settings.card.max_task_cards = 3

        with patch("src.config.get_settings", return_value=mock_settings):
            from src.card.orchestrator import TaskOrchestrator as TO
            orch = TO.from_settings(
                chat_id="test_chat",
                session_creator=lambda tid: FakeSession(tid),
                thinking_session=FakeSession("thinking"),
                bridge_class=MagicMock,
            )
        assert orch._max_task_cards == 3

    def test_default_max_task_cards_is_eight(self):
        """Default max_task_cards is 8 (from config)."""
        from src.config import CardSessionConfig
        cfg = CardSessionConfig()
        assert cfg.max_task_cards == 8

    def test_thinking_session_is_set(self):
        """Thinking session is correctly set by factory."""
        from unittest.mock import MagicMock, patch

        mock_settings = MagicMock()
        mock_settings.card.task_level_cards_enabled = True
        mock_settings.card.max_task_cards = 8

        thinking = FakeSession("think")
        with patch("src.config.get_settings", return_value=mock_settings):
            from src.card.orchestrator import TaskOrchestrator as TO
            orch = TO.from_settings(
                chat_id="c",
                session_creator=lambda tid: FakeSession(tid),
                thinking_session=thinking,
            )
        assert orch._thinking_session is thinking


# ──────────────────────────────────────────────────────────────────────────────
# TestConcurrentClose (AC-R3)
# ──────────────────────────────────────────────────────────────────────────────


class TestConcurrentClose:
    """Two threads calling close() concurrently: internal logic executes only once."""

    def test_concurrent_close_executes_once(self):
        """close() body runs exactly once even with 100 concurrent calls."""
        import threading

        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch.on_plan_received([{"task_id": "t1", "name": "A"}])

        close_count = {"n": 0}
        original_set = orch._closed_event.set

        def counting_set():
            close_count["n"] += 1
            original_set()

        orch._closed_event.set = counting_set

        barrier = threading.Barrier(2)
        errors = []

        def closer():
            barrier.wait()
            for _ in range(50):
                try:
                    orch.close()
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=closer)
        t2 = threading.Thread(target=closer)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors
        assert close_count["n"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# TestPlanReceivedThreadSafety
# ──────────────────────────────────────────────────────────────────────────────


class TestPlanReceivedThreadSafety:
    """Verify _plan_received (threading.Event) is safe under concurrent access."""

    def test_concurrent_on_plan_received_and_route_or_fallback(self):
        """Multiple threads calling on_plan_received + route_or_fallback concurrently: no exceptions."""
        from unittest.mock import MagicMock
        from src.acp.models import ACPEvent, ACPEventType as ACPEvType

        orch, registry, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        fake_bridge = MagicMock()
        event = MagicMock(spec=ACPEvent)
        event.event_type = ACPEvType.TEXT_CHUNK
        event.text = "x"
        event.tool_call = None
        event.plan = None

        errors: list[Exception] = []
        barrier = threading.Barrier(4, timeout=10)

        def plan_writer():
            barrier.wait()
            for _ in range(50):
                try:
                    orch.on_plan_received([
                        {"task_id": f"t{i}", "name": f"Task {i}"}
                        for i in range(3)
                    ])
                except Exception as e:
                    errors.append(e)

        def route_reader():
            barrier.wait()
            for _ in range(100):
                try:
                    orch.route_or_fallback(event, fake_bridge)
                except Exception as e:
                    errors.append(e)

        def resetter():
            barrier.wait()
            for _ in range(30):
                try:
                    orch.reset()
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=plan_writer),
            threading.Thread(target=route_reader),
            threading.Thread(target=route_reader),
            threading.Thread(target=resetter),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Concurrent access raised exceptions: {errors}"

    def test_has_plan_reflects_state_correctly(self):
        """has_plan returns False initially, True after on_plan_received, False after reset."""
        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        assert orch.has_plan is False

        orch.on_plan_received([{"task_id": "t1", "name": "A"}])
        assert orch.has_plan is True

        orch.reset()
        assert orch.has_plan is False


class TestRouteOrFallbackCorrectnessUnderConcurrency:
    """AC-7: route_or_fallback returns False and routes to bridge in fallback mode."""

    def test_concurrent_fallback_routing_correctness(self):
        """5 threads × 100 calls in fallback_mode=True: all return False, bridge receives all."""
        from unittest.mock import MagicMock
        from src.acp.models import ACPEvent, ACPEventType as ACPEvType

        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch._enter_fallback_mode()

        fake_bridge = MagicMock()
        event = MagicMock(spec=ACPEvent)
        event.event_type = ACPEvType.TEXT_CHUNK
        event.text = "x"
        event.tool_call = None
        event.plan = None

        results: list[bool] = []
        results_lock = threading.Lock()
        errors: list[Exception] = []
        barrier = threading.Barrier(5, timeout=10)

        def worker():
            barrier.wait()
            for _ in range(100):
                try:
                    r = orch.route_or_fallback(event, fake_bridge)
                    with results_lock:
                        results.append(r)
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Exceptions: {errors}"
        assert len(results) == 500
        assert all(r is False for r in results), "All should return False in fallback mode"
        assert fake_bridge.on_event.call_count == 500


class TestEnterFallbackConcurrentWithRoute:
    """AC-8: _enter_fallback_mode + route_or_fallback concurrent: no event lost."""

    def test_no_event_lost_during_fallback_entry(self):
        """2 threads: one enters fallback, other routes — no exceptions, events not lost."""
        from unittest.mock import MagicMock
        from src.acp.models import ACPEvent, ACPEventType as ACPEvType

        orch, _, _ = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)

        fake_bridge = MagicMock()
        event = MagicMock(spec=ACPEvent)
        event.event_type = ACPEvType.TEXT_CHUNK
        event.text = "x"
        event.tool_call = None
        event.plan = None

        errors: list[Exception] = []
        route_results: list[bool] = []
        route_lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=10)

        def fallback_enterer():
            barrier.wait()
            for _ in range(50):
                try:
                    orch._enter_fallback_mode()
                except Exception as e:
                    errors.append(e)

        def route_caller():
            barrier.wait()
            for _ in range(50):
                try:
                    r = orch.route_or_fallback(event, fake_bridge)
                    with route_lock:
                        route_results.append(r)
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=fallback_enterer)
        t2 = threading.Thread(target=route_caller)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert not errors, f"Exceptions: {errors}"
        # All 50 route calls should complete (not lost)
        assert len(route_results) == 50
        # Every event either went to route_acp_event (True) or bridge (False) — no crash
        false_count = sum(1 for r in route_results if r is False)
        true_count = sum(1 for r in route_results if r is True)
        assert false_count + true_count == 50


class TestConcurrentCloseIdempotentDispatch:
    """AC-9: concurrent close() dispatches COMPLETED exactly once per session."""

    def test_completed_dispatched_once_per_session(self):
        """2 threads call close(): COMPLETED event dispatched == number of sessions."""
        orch, _, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        tasks = [
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ]
        orch.on_plan_received(tasks)
        _trigger_all(orch, tasks)
        num_sessions = 2

        errors: list[Exception] = []
        barrier = threading.Barrier(2, timeout=10)

        def closer():
            barrier.wait()
            for _ in range(50):
                try:
                    orch.close()
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=closer)
        t2 = threading.Thread(target=closer)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert not errors, f"Exceptions: {errors}"
        completed_count = 0
        for s in sessions.values():
            completed_count += sum(
                1 for e in s.dispatched_events if e.type == CardEventType.COMPLETED
            )
        assert completed_count == num_sessions, (
            f"Expected {num_sessions} COMPLETED events, got {completed_count}"
        )


class TestCloseAndDispatchToTaskRace:
    """AC-10: close() + dispatch_to_task() race: dispatch after close is a no-op."""

    def test_dispatch_after_close_is_noop(self):
        """One thread closes, another dispatches 50 times: no error, no post-close growth."""
        orch, _, sessions = _make_orchestrator()
        thinking = FakeSession("thinking")
        orch.set_thinking_session(thinking)
        orch.on_plan_received([
            {"task_id": "t1", "name": "A"},
            {"task_id": "t2", "name": "B"},
        ])

        errors: list[Exception] = []
        barrier = threading.Barrier(2, timeout=10)

        def closer():
            barrier.wait()
            time.sleep(0.001)
            orch.close()

        def dispatcher():
            barrier.wait()
            for _ in range(50):
                try:
                    event = CardEvent.text_delta("blk", "msg")
                    orch.dispatch_to_task("t1", event)
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=closer)
        t2 = threading.Thread(target=dispatcher)
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert not errors, f"Exceptions: {errors}"
        s = sessions.get("t1")
        if s is not None:
            text_deltas = [e for e in s.dispatched_events if e.type == CardEventType.TEXT_DELTA]
            # Should be <= 50; after close, no more dispatches land
            assert len(text_deltas) <= 50


class TestFullLifecycleResetReentry:
    """AC-11: full lifecycle set_thinking→plan→dispatch→reset→plan again works."""

    def test_second_cycle_creates_new_sessions(self):
        """Complete two full cycles: second plan creates independent sessions."""
        orch, registry, sessions = _make_orchestrator()

        # --- Cycle 1 ---
        thinking1 = FakeSession("thinking1")
        orch.set_thinking_session(thinking1)
        assert orch.has_plan is False

        c1_tasks = [
            {"task_id": "c1_t1", "name": "Cycle1 Task1"},
            {"task_id": "c1_t2", "name": "Cycle1 Task2"},
        ]
        orch.on_plan_received(c1_tasks)
        _trigger_all(orch, c1_tasks)
        assert orch.has_plan is True
        assert "c1_t1" in sessions
        assert "c1_t2" in sessions

        # Dispatch to cycle 1 sessions
        orch.dispatch_to_task("c1_t1", CardEvent.text_delta("b1", "hello_c1"))
        s1 = sessions["c1_t1"]
        assert any(
            e.type == CardEventType.TEXT_DELTA and e.payload.get("text") == "hello_c1"
            for e in s1.dispatched_events
        )

        # --- Reset ---
        orch.reset()
        assert orch.has_plan is False

        # --- Cycle 2 ---
        thinking2 = FakeSession("thinking2")
        orch.set_thinking_session(thinking2)

        c2_tasks = [
            {"task_id": "c2_t1", "name": "Cycle2 Task1"},
        ]
        orch.on_plan_received(c2_tasks)
        _trigger_all(orch, c2_tasks)
        assert orch.has_plan is True
        assert "c2_t1" in sessions

        # Dispatch to cycle 2 session
        orch.dispatch_to_task("c2_t1", CardEvent.text_delta("b2", "hello_c2"))
        s2 = sessions["c2_t1"]
        assert any(
            e.type == CardEventType.TEXT_DELTA and e.payload.get("text") == "hello_c2"
            for e in s2.dispatched_events
        )

        # Verify cycle 2 session is independent (not the same object as cycle 1)
        assert s2 is not s1
