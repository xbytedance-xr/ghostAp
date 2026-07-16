"""Integration tests: DeepRenderer task-list behavior.

Deep keeps all live updates on the main Feishu card. Task progress is rendered
through the shared task-list component instead of creating per-task messages.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo, ToolCallInfo
from src.card.events import CardEventType

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
        self.add_reaction = MagicMock()
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeepRendererSingleCard:
    """Verify Deep mode keeps task orchestration on one card."""

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

    def _create_callbacks(
        self,
        renderer,
        *,
        task_level_cards_enabled: bool = False,
        project=None,
        engine_name: str = "Coco",
        requirement_text: str | None = None,
    ):
        mock_settings = MagicMock()
        mock_settings.card.task_level_cards_enabled = task_level_cards_enabled
        mock_settings.card.max_task_cards = 8
        with patch("src.config.get_settings", return_value=mock_settings):
            return renderer.create_deep_callbacks(
                message_id="msg_1",
                chat_id="chat_1",
                project=project,
                engine_name=engine_name,
                requirement_text=requirement_text,
            )

    def test_deep_session_has_question_summary_before_first_dispatch(self):
        renderer, tracker = self._setup_renderer()

        self._create_callbacks(
            renderer,
            requirement_text="  优化Deep模式消息卡片标题并展示用户问题  ",
        )

        metadata = tracker.sessions_created[0]._metadata
        assert metadata.question_title == "优化Deep模式消息卡片标题…"
        assert len(metadata.question_title) <= 15

    def test_deep_session_without_requirement_has_stable_fallback_before_dispatch(self):
        renderer, tracker = self._setup_renderer()

        self._create_callbacks(renderer)

        assert tracker.sessions_created[0]._metadata.question_title == "Deep 任务"

    def test_multi_task_plan_stays_single_card_by_default(self):
        """3 plan steps stay in one Feishu card by default."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        assert tracker.create_card_count == 1

        plan_event = _make_plan_event([
            ("Analyze requirements", "in_progress"),
            ("Write implementation", "in_progress"),
            ("Run tests", "in_progress"),
        ])
        callbacks.on_event(plan_event)

        assert tracker.create_card_count == 1

    def test_deep_start_uses_spec_style_cycle_phase_events(self):
        """Deep cards should use the same cycle/phase structure as Spec cards."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        assert tracker.create_card_count == 1
        main_session = tracker.sessions_created[0]
        events = [
            call.args[0]
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]
        event_types = [event.type for event in events]

        assert CardEventType.CYCLE_STARTED in event_types
        phase_events = [event for event in events if event.type == CardEventType.PHASE_STARTED]
        assert phase_events
        assert phase_events[-1].payload["phase"] == "analyzing"
        assert not any(
            event.type == CardEventType.TEXT_STARTED
            and event.payload.get("block_id") == "_main_text"
            for event in events
        )

    def test_deep_start_card_dispatches_on_analyzing_start(self):
        """Deep should create the first visible card before waiting for model output."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer)
        assert callbacks.on_analyzing_start is not None

        callbacks.on_analyzing_start("investigate startup latency")

        assert tracker.create_card_count == 1
        main_session = tracker.sessions_created[0]
        events = [
            call.args[0]
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]
        event_types = [event.type for event in events]

        assert CardEventType.STARTED in event_types
        assert CardEventType.CYCLE_STARTED in event_types
        assert CardEventType.PHASE_STARTED in event_types

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        events_after_done = [
            call.args[0]
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]
        assert [event.type for event in events_after_done].count(CardEventType.STARTED) == 1
        assert [event.type for event in events_after_done].count(CardEventType.CYCLE_STARTED) == 1
        assert [event.type for event in events_after_done].count(CardEventType.PHASE_STARTED) == 1

    def test_deep_plan_update_transitions_to_spec_style_build_phase(self):
        """Deep plan updates should switch from analyzing to the shared build phase panel."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)
        callbacks.on_event(_make_plan_event([
            ("Analyze requirements", "completed"),
            ("Write implementation", "in_progress"),
        ]))

        main_session = tracker.sessions_created[0]
        events = [
            call.args[0]
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]

        assert any(
            event.type == CardEventType.PHASE_DONE
            and event.payload["phase"] == "analyzing"
            for event in events
        )
        assert any(
            event.type == CardEventType.PHASE_STARTED
            and event.payload["phase"] == "build"
            for event in events
        )

    def test_multi_task_plan_stays_single_card_even_when_task_cards_enabled(self):
        """Deep ignores task-card fanout and renders plan tasks on the main card."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer, task_level_cards_enabled=True)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        assert tracker.create_card_count == 1

        plan_event = _make_plan_event([
            ("Analyze requirements", "in_progress"),
            ("Write implementation", "in_progress"),
            ("Run tests", "in_progress"),
        ])
        callbacks.on_event(plan_event)

        assert tracker.create_card_count == 1
        main_session = tracker.sessions_created[0]
        task_updates = [
            call.args[0]
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type") and call.args[0].type == CardEventType.TASK_LIST_UPDATED
        ]
        assert task_updates
        assert task_updates[-1].payload["tasks"] == [
            {"task_id": "step_0", "name": "Analyze requirements", "status": "in_progress"},
            {"task_id": "step_1", "name": "Write implementation", "status": "in_progress"},
            {"task_id": "step_2", "name": "Run tests", "status": "in_progress"},
        ]

    def test_single_step_plan_stays_single_card(self):
        """A plan with only 1 step stays in single-card mode (no split)."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer)

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

        callbacks = self._create_callbacks(renderer)

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

        callbacks = self._create_callbacks(renderer)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        # Plan with empty content entries
        plan_event = _make_plan_event([("", "pending"), ("  ", "pending"), ("", "pending")])
        callbacks.on_event(plan_event)

        # All entries have empty content → factory converts to 0 valid tasks → no split
        assert tracker.create_card_count == 1

    def test_project_done_completes_main_card(self):
        """on_project_done completes the same Deep card."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer, task_level_cards_enabled=True)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        plan_event = _make_plan_event([
            ("Task A", "in_progress"),
            ("Task B", "in_progress"),
        ])
        callbacks.on_event(plan_event)
        assert tracker.create_card_count == 1

        dp.status = DeepProjectStatus.COMPLETED
        callbacks.on_project_done(dp)

        assert tracker.create_card_count == 1
        main_session = tracker.sessions_created[0]
        completed_calls = [
            call for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type") and call.args[0].type == CardEventType.COMPLETED
        ]
        assert len(completed_calls) == 1

    def test_project_done_adds_done_reaction_to_original_message(self):
        """Deep completion sends the user-visible done reaction immediately."""
        renderer, _ = self._setup_renderer()

        callbacks = self._create_callbacks(renderer, task_level_cards_enabled=True)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        dp.status = DeepProjectStatus.COMPLETED
        callbacks.on_project_done(dp)

        renderer.handler.add_reaction.assert_called_once_with("msg_1", "PARTY")

    def test_project_done_keeps_final_summary_on_main_card(self):
        """Deep completion summary is delivered on the main card, not a new card."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer, task_level_cards_enabled=True)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        callbacks.on_event(_make_plan_event([
            ("Task A", "in_progress"),
            ("Task B", "in_progress"),
        ]))
        assert tracker.create_card_count == 1

        dp.status = DeepProjectStatus.COMPLETED
        callbacks.on_project_done(dp)

        assert tracker.create_card_count == 1
        summary_session = tracker.sessions_created[0]
        summary_events = [
            call.args[0]
            for call in summary_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]
        assert any(
            event.type == CardEventType.COMPLETED and "执行完成" in event.payload.get("summary", "")
            for event in summary_events
        )

    def test_project_done_failed_formats_incomplete_summary(self):
        """Deep incomplete terminal text should not leak format placeholders."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer, task_level_cards_enabled=True)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        callbacks.on_event(_make_plan_event([
            ("Task A", "completed"),
            ("Task B", "in_progress"),
        ]))
        dp.status = DeepProjectStatus.FAILED
        callbacks.on_project_done(dp)

        main_session = tracker.sessions_created[0]
        failed_events = [
            call.args[0]
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type") and call.args[0].type == CardEventType.FAILED
        ]
        assert failed_events
        assert "{completed}" not in failed_events[-1].payload["error"]
        assert "已完成 1/2 步" in failed_events[-1].payload["error"]

    def test_agent_tool_call_updates_main_card_without_child_card(self):
        """Agent/subagent tool calls update the main card task list and tool panels."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer, task_level_cards_enabled=True)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        callbacks.on_event(_make_plan_event([
            ("Task A", "in_progress"),
            ("Task B", "in_progress"),
        ]))
        assert tracker.create_card_count == 1

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

        assert tracker.create_card_count == 1
        main_session = tracker.sessions_created[0]
        main_event_types = [
            call.args[0].type
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]
        assert CardEventType.TOOL_STARTED in main_event_types
        assert CardEventType.TOOL_DELTA in main_event_types
        assert CardEventType.TOOL_DONE in main_event_types
        task_updates = [
            call.args[0]
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type") and call.args[0].type == CardEventType.TASK_LIST_UPDATED
        ]
        assert task_updates[-1].payload["tasks"][-1] == {
            "task_id": "agent_call_1",
            "name": "检查卡片路由",
            "status": "completed",
        }

    def test_agent_tool_events_stay_on_main_deep_card_even_if_task_cards_enabled(self):
        """Deep keeps agent/subagent work on the main card and renders tasks there."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer, task_level_cards_enabled=True)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        callbacks.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="agent_call_1",
                title="agent",
                kind="execute",
                status="in_progress",
                content="梳理 Deep 卡片问题\n子代理：Explore",
            ),
        ))
        callbacks.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_START,
            tool_call=ToolCallInfo(
                id="agent_call_2",
                title="subagent",
                kind="execute",
                status="in_progress",
                content="补充 Deep 回归测试\n子代理：Write",
            ),
        ))
        callbacks.on_event(ACPEvent(
            event_type=ACPEventType.TOOL_CALL_DONE,
            tool_call=ToolCallInfo(
                id="agent_call_1",
                title="agent",
                kind="execute",
                status="completed",
                content="完成卡片路径梳理",
            ),
        ))

        assert tracker.create_card_count == 1
        main_session = tracker.sessions_created[0]
        task_updates = [
            call.args[0]
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type") and call.args[0].type == CardEventType.TASK_LIST_UPDATED
        ]
        assert task_updates
        latest_tasks = task_updates[-1].payload["tasks"]
        assert latest_tasks == [
            {"task_id": "agent_call_1", "name": "梳理 Deep 卡片问题", "status": "completed"},
            {"task_id": "agent_call_2", "name": "补充 Deep 回归测试", "status": "in_progress"},
        ]
        main_event_types = [
            call.args[0].type
            for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type")
        ]
        assert CardEventType.TOOL_STARTED in main_event_types
        assert CardEventType.TOOL_DONE in main_event_types

    def test_error_fails_main_card(self):
        """on_error fails the same Deep card."""
        renderer, tracker = self._setup_renderer()

        callbacks = self._create_callbacks(renderer, task_level_cards_enabled=True)

        from src.deep_engine.models import DeepProjectStatus
        dp = FakeDeepProject(status=DeepProjectStatus.EXECUTING)
        callbacks.on_analyzing_done(dp)

        plan_event = _make_plan_event([
            ("Task A", "pending"),
            ("Task B", "pending"),
        ])
        callbacks.on_event(plan_event)

        callbacks.on_error("Something went wrong")

        assert tracker.create_card_count == 1
        main_session = tracker.sessions_created[0]
        failed_calls = [
            call for call in main_session.dispatch.call_args_list
            if call.args and hasattr(call.args[0], "type") and call.args[0].type == CardEventType.FAILED
        ]
        assert len(failed_calls) == 1


# ---------------------------------------------------------------------------
# Tests merged from test_deep_renderer_split.py
# ---------------------------------------------------------------------------


def _build_split_renderer():
    """Build a DeepRenderer for card-split tests."""
    from src.feishu.renderers.deep_renderer import DeepRenderer

    handler = MagicMock()
    handler.ctx = MagicMock()
    handler.settings = MagicMock()
    handler.settings.engine_timeout_warning_seconds = 0
    handler.add_reaction = MagicMock()
    handler.send_text_to_chat = MagicMock()
    handler.reply_text = MagicMock()
    handler.context_manager = MagicMock()
    handler.ensure_request_id = MagicMock(return_value="req1")
    handler.get_card_delivery = MagicMock()
    handler.project_manager = MagicMock()
    handler.get_engine_name = MagicMock(return_value="Coco")
    renderer = DeepRenderer(handler)
    renderer.create_session = MagicMock(return_value=MagicMock(closed=False))
    return renderer


def test_deep_renderer_does_not_split_on_task_done_in_single_card_mode():
    """Single-card mode keeps task transitions inside the same Feishu card."""
    renderer = _build_split_renderer()
    captured: list[tuple[str, str | None, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None, bridge_phrase=None: captured.append((reason, hint, bridge_phrase))

    mock_settings = MagicMock()
    mock_settings.card.task_level_cards_enabled = False

    with patch("src.config.get_settings", return_value=mock_settings):
        callbacks = renderer.create_deep_callbacks(
            message_id="m1",
            chat_id="c1",
            project=None,
            engine_name="Coco",
        )

    initial_plan = PlanInfo(entries=[
        PlanEntryInfo(content="task 1", status="in_progress"),
        PlanEntryInfo(content="task 2", status="pending"),
    ])
    callbacks.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=initial_plan))

    updated_plan = PlanInfo(entries=[
        PlanEntryInfo(content="task 1", status="completed"),
        PlanEntryInfo(content="task 2", status="in_progress"),
    ])
    callbacks.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=updated_plan))

    assert captured == []


def test_deep_renderer_no_split_in_multi_card_mode():
    """Multi-card mode must not use task_done card_split either."""
    renderer = _build_split_renderer()
    captured: list[tuple[str, str | None, str | None]] = []
    renderer._dispatch_card_split = lambda sess, *, reason, hint=None, bridge_phrase=None: captured.append((reason, hint, bridge_phrase))

    mock_settings = MagicMock()
    mock_settings.card.task_level_cards_enabled = True

    with patch("src.config.get_settings", return_value=mock_settings):
        callbacks = renderer.create_deep_callbacks(
            message_id="m1",
            chat_id="c1",
            project=None,
            engine_name="Coco",
        )

    initial_plan = PlanInfo(entries=[
        PlanEntryInfo(content="task 1", status="in_progress"),
        PlanEntryInfo(content="task 2", status="pending"),
    ])
    callbacks.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=initial_plan))

    updated_plan = PlanInfo(entries=[
        PlanEntryInfo(content="task 1", status="completed"),
        PlanEntryInfo(content="task 2", status="in_progress"),
    ])
    callbacks.on_event(ACPEvent(event_type=ACPEventType.PLAN_UPDATE, plan=updated_plan))

    # No card_split should have been dispatched
    assert not any(reason == "task_done" for reason, _, _ in captured)
