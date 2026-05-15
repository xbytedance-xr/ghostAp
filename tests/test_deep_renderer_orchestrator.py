"""Integration tests: DeepRenderer + TaskOrchestrator multi-card behavior.

Verifies AC1: create_card called N+1 times (1 thinking + N tasks) when
the Deep engine's plan contains multiple tasks.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo, ToolCallInfo
from src.card.events import CardEvent, CardEventType


# ---------------------------------------------------------------------------
# Fakes and Fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeDeepProject:
    name: str = "test-project"
    root_path: str = "/tmp/test"
    project_id: str = "dp_test"
    status: object = None

    def __post_init__(self):
        if self.status is None:
            from src.deep_engine.models import DeepProjectStatus
            self.status = DeepProjectStatus.COMPLETED


class FakeHandler:
    """Minimal fake handler for DeepRenderer."""

    def __init__(self):
        self.settings = MagicMock()
        self.settings.deep_stream_interval = 0.1
        self.settings.deep_stream_min_chars = 10
        self.project_manager = MagicMock()
        self.context_manager = MagicMock()
        self.ctx = FakeRendererCtx()
        self._request_ids = {}

    def ensure_request_id(self, message_id, **kwargs):
        return f"req_{message_id}"

    def reply_text(self, message_id, text):
        pass

    def get_working_dir(self, chat_id):
        return "/tmp/test"

    def get_engine_name(self, chat_id, **kwargs):
        return "Coco"

    def get_card_delivery(self):
        return MagicMock()


class FakeRendererCtx:
    """Minimal renderer context."""

    def __init__(self):
        self.progress_reporter = MagicMock()
        self.deep_engine_manager = MagicMock()
        self.deep_engine_manager.snapshot.return_value = None
        self.deep_engine_manager.snapshot_active.return_value = []


class SessionTracker:
    """Tracks all sessions created (simulates create_card calls)."""

    def __init__(self):
        self.sessions_created: list[MagicMock] = []
        self._lock = threading.Lock()

    def create_session(self, *args, **kwargs):
        """Each create_session call represents a create_card call."""
        session = MagicMock()
        session.dispatch = MagicMock()
        session.closed = False
        session.sequence = len(self.sessions_created) + 1
        session.session_started_at = time.monotonic()
        if len(args) >= 3:
            session._metadata = args[2]
        session.delivered_message_id = f"msg_{len(self.sessions_created)}"
        with self._lock:
            self.sessions_created.append(session)
        return session

    @property
    def create_card_count(self) -> int:
        with self._lock:
            return len(self.sessions_created)


def _make_plan_event(entries: list[tuple[str, str]]) -> ACPEvent:
    """Create a PLAN_UPDATE ACPEvent with given (content, status) entries."""
    plan_entries = [PlanEntryInfo(content=c, status=s) for c, s in entries]
    return ACPEvent(
        event_type=ACPEventType.PLAN_UPDATE,
        plan=PlanInfo(entries=plan_entries),
    )


def _make_text_event(text: str = "hello") -> ACPEvent:
    return ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=text)


def _make_tool_start_event(name: str = "write_file") -> ACPEvent:
    return ACPEvent(
        event_type=ACPEventType.TOOL_CALL_START,
        tool_call=ToolCallInfo(id=f"tc_{name}", title=name),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeepRendererMultiCard:
    """Verify multi-card creation for Deep mode with task orchestration."""

    def _setup_renderer(self):
        """Create a DeepRenderer with mocked dependencies and session tracking."""
        from src.feishu.renderers.deep_renderer import DeepRenderer

        tracker = SessionTracker()
        handler = FakeHandler()

        renderer = DeepRenderer(handler)
        renderer.ctx = FakeRendererCtx()
        renderer._session_factory = MagicMock()

        # Patch create_session to track card creation
        renderer.create_session = tracker.create_session
        renderer._get_session_factory = lambda: MagicMock()
        renderer._build_hooks = lambda *a, **kw: ()
        renderer.check_warning_banner = lambda *a, **kw: None

        return renderer, tracker

    def test_multi_task_plan_creates_n_plus_1_cards(self):
        """AC1: 3 plan steps (all in_progress) → 4 create_card calls (1 thinking + 3 task cards).

        Lazy mode: tasks with status=in_progress trigger eager card creation.
        """
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_deep_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
            engine_name="Coco",
        )

        # 1) Analyzing done → starts the thinking session card
        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        # At this point: 1 session (thinking)
        assert tracker.create_card_count == 1

        # 2) Emit a PLAN_UPDATE with 3 tasks already executing
        plan_event = _make_plan_event([
            ("Analyze requirements", "in_progress"),
            ("Write implementation", "in_progress"),
            ("Run tests", "in_progress"),
        ])
        callbacks.on_event(plan_event)

        # Should now have 1 (thinking) + 3 (task sessions) = 4
        assert tracker.create_card_count == 4

    def test_single_step_plan_stays_single_card(self):
        """A plan with only 1 step stays in single-card mode (no split)."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_deep_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        # Only 1 step
        plan_event = _make_plan_event([("Single task", "pending")])
        callbacks.on_event(plan_event)

        # Should still be just 1 card (thinking session)
        assert tracker.create_card_count == 1

    def test_no_plan_stays_single_card(self):
        """Without any PLAN_UPDATE, stays in single-card mode."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_deep_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        # Send text events, no plan
        callbacks.on_event(_make_text_event("thinking..."))
        callbacks.on_event(_make_text_event("more thinking..."))

        # Still just 1 card
        assert tracker.create_card_count == 1

    def test_empty_plan_entries_stays_single_card(self):
        """Plan with empty entries (no content) stays single-card (fallback)."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_deep_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        # Plan with empty content entries
        plan_event = _make_plan_event([("", "pending"), ("  ", "pending"), ("", "pending")])
        callbacks.on_event(plan_event)

        # All entries have empty content → factory converts to 0 valid tasks → no split
        assert tracker.create_card_count == 1

    def test_project_done_closes_orchestrator_in_multi_card(self):
        """on_project_done calls orchestrator.close() in multi-card mode."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_deep_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        # Trigger multi-card
        plan_event = _make_plan_event([
            ("Task A", "in_progress"),
            ("Task B", "in_progress"),
        ])
        callbacks.on_event(plan_event)
        assert tracker.create_card_count == 3  # 1 + 2

        # Complete the project
        dp.status = DeepProjectStatus.COMPLETED
        callbacks.on_project_done(dp)

        # Verify task sessions received COMPLETED (via orchestrator.close())
        for session in tracker.sessions_created[1:]:  # Skip thinking session
            completed_calls = [
                call for call in session.dispatch.call_args_list
                if call.args and hasattr(call.args[0], 'type') and call.args[0].type == CardEventType.COMPLETED
            ]
            assert len(completed_calls) >= 1

    def test_project_done_creates_final_summary_card_after_task_cards_stop(self):
        """Deep multi-card completion starts a fresh summary card instead of patching task cards."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_deep_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        callbacks.on_event(_make_plan_event([
            ("Task A", "in_progress"),
            ("Task B", "in_progress"),
        ]))
        assert tracker.create_card_count == 3

        dp.status = DeepProjectStatus.COMPLETED
        callbacks.on_project_done(dp)

        assert tracker.create_card_count == 4
        summary_session = tracker.sessions_created[-1]
        summary_events = [
            call.args[0]
            for call in summary_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]
        assert [event.type for event in summary_events][-2:] == [
            CardEventType.TEXT_DONE,
            CardEventType.COMPLETED,
        ]
        assert any(
            event.type == CardEventType.TEXT_DELTA and "执行完成" in event.payload.get("text", "")
            for event in summary_events
        )

    def test_agent_tool_call_creates_independent_child_card(self):
        """Agent/subagent tool calls create and complete a separate Deep child card."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_deep_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        callbacks.on_event(_make_plan_event([
            ("Task A", "in_progress"),
            ("Task B", "in_progress"),
        ]))
        assert tracker.create_card_count == 3
        for task_session in tracker.sessions_created[1:3]:
            task_session.dispatch.reset_mock()

        callbacks.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="agent_call_1",
                title="agent",
                kind="execute",
                status="in_progress",
                content="检查卡片路由\n子代理：Explore",
            ),
        ))
        callbacks.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_UPDATE,
            tool_call=ToolCallInfo(
                id="agent_call_1",
                title="shell",
                kind="execute",
                status="in_progress",
                content="正在检查 Deep 子任务卡片",
            ),
        ))
        callbacks.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_DONE,
            tool_call=ToolCallInfo(
                id="agent_call_1",
                title="shell",
                kind="execute",
                status="completed",
                content="子任务完成",
            ),
        ))

        assert tracker.create_card_count == 4
        child_session = tracker.sessions_created[-1]
        child_event_types = [
            call.args[0].type
            for call in child_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]
        assert CardEventType.TOOL_STARTED in child_event_types
        assert CardEventType.TOOL_DELTA in child_event_types
        assert CardEventType.TOOL_DONE in child_event_types
        assert CardEventType.COMPLETED in child_event_types

        for parent_session in tracker.sessions_created[1:3]:
            parent_event_types = [
                call.args[0].type
                for call in parent_session.dispatch.call_args_list
                if call.args and hasattr(call.args[0], "type")
            ]
            assert CardEventType.PROGRESS_UPDATED not in parent_event_types
            assert CardEventType.TOOL_MODEL_CHANGED not in parent_event_types

    def test_error_closes_orchestrator_in_multi_card(self):
        """on_error calls orchestrator.close() in multi-card mode."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_deep_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        plan_event = _make_plan_event([
            ("Task A", "pending"),
            ("Task B", "pending"),
        ])
        callbacks.on_event(plan_event)

        # Trigger error
        callbacks.on_error("Something went wrong")

        # Task sessions should have received COMPLETED (close dispatches COMPLETED)
        for session in tracker.sessions_created[1:]:
            completed_calls = [
                call for call in session.dispatch.call_args_list
                if call.args and hasattr(call.args[0], 'type') and call.args[0].type == CardEventType.COMPLETED
            ]
            assert len(completed_calls) >= 1
