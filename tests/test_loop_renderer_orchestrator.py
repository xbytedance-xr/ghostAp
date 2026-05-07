"""Integration tests: LoopRenderer + TaskOrchestrator multi-card behavior.

Verifies that when a Loop iteration's ACP agent produces a plan with
multiple tasks, per-task cards are created.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo
from src.card.events import CardEvent, CardEventType


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRendererCtx:
    def __init__(self):
        self.loop_reporter = MagicMock()
        self.loop_reporter.format_analyzing_done.return_value = "analyzing done"
        self.loop_reporter.format_criteria_section.return_value = ""
        self.loop_reporter.format_iteration_done.return_value = ""
        self.loop_reporter.format_review_result.return_value = ""
        self.loop_reporter.format_project_done.return_value = ""
        self.loop_engine_manager = MagicMock()
        self.loop_engine_manager.snapshot.return_value = None
        self.loop_engine_manager.snapshot_active.return_value = []


class FakeHandler:
    def __init__(self):
        self.settings = MagicMock()
        self.settings.deep_stream_interval = 0.1
        self.settings.deep_stream_min_chars = 10
        self.settings.card = MagicMock()
        self.settings.card.session_max_rotations = 20
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


class SessionTracker:
    """Tracks session/card creation calls."""

    def __init__(self):
        self.sessions_created: list[MagicMock] = []
        self._lock = threading.Lock()

    def create_session(self, *args, **kwargs):
        session = MagicMock()
        session.dispatch = MagicMock()
        session.delivered_message_id = f"msg_{len(self.sessions_created)}"
        with self._lock:
            self.sessions_created.append(session)
        return session

    @property
    def create_card_count(self) -> int:
        with self._lock:
            return len(self.sessions_created)


@dataclass
class FakeLoopProject:
    name: str = "test-loop"
    root_path: str = "/tmp/loop"
    status: object = None
    satisfied_count: int = 0
    total_criteria: int = 3

    def __post_init__(self):
        if self.status is None:
            self.status = MagicMock(value="executing")

    def duration(self):
        return 10.0


def _make_plan_event(entries: list[tuple[str, str]]) -> ACPEvent:
    plan_entries = [PlanEntryInfo(content=c, status=s) for c, s in entries]
    return ACPEvent(
        event_type=ACPEventType.PLAN_UPDATE,
        plan=PlanInfo(entries=plan_entries),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoopRendererMultiCard:
    """Verify multi-card creation for Loop mode with task orchestration."""

    def _setup_renderer(self):
        from src.feishu.renderers.loop_renderer import LoopRenderer

        tracker = SessionTracker()
        handler = FakeHandler()

        renderer = LoopRenderer(handler)
        renderer.ctx = FakeRendererCtx()
        renderer._session_factory = MagicMock()
        renderer.create_session = tracker.create_session
        renderer._get_session_factory = lambda: MagicMock()
        renderer._build_hooks = lambda *a, **kw: ()
        renderer.check_warning_banner = lambda *a, **kw: None

        # Mock _create_rotator to return a session from tracker
        def _fake_create_rotator(*args, **kwargs):
            session = tracker.create_session()
            session.rotation_count = 0
            session.current = session
            return session
        renderer._create_rotator = _fake_create_rotator

        return renderer, tracker

    def test_multi_task_plan_in_iteration_creates_extra_cards(self):
        """PLAN_UPDATE with 3 tasks within iteration → 3 extra cards created."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_loop_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        # Initial: 1 card (rotator)
        assert tracker.create_card_count == 1

        # Simulate iteration event with multi-task plan
        plan_event = _make_plan_event([
            ("Analyze codebase", "pending"),
            ("Fix bug", "pending"),
            ("Add tests", "pending"),
        ])
        callbacks.on_iteration_event(1, plan_event)

        # Should have 1 (rotator) + 3 (task sessions) = 4
        assert tracker.create_card_count == 4

    def test_no_plan_stays_single_card(self):
        """Without PLAN_UPDATE, stays in single-card mode (just the rotator)."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_loop_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        # Send text events (no plan)
        text_event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="hello")
        callbacks.on_iteration_event(1, text_event)

        # Still just 1 card (rotator)
        assert tracker.create_card_count == 1

    def test_single_step_plan_no_split(self):
        """Single-step plan doesn't trigger multi-card."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_loop_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        plan_event = _make_plan_event([("Single task", "pending")])
        callbacks.on_iteration_event(1, plan_event)

        # No split
        assert tracker.create_card_count == 1
