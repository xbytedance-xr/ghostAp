"""Integration tests: SpecRenderer + TaskOrchestrator multi-card behavior.

Verifies that when the Spec engine's BUILD phase produces a plan with
multiple tasks via PLAN_UPDATE, per-task cards are created.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from src.acp.models import ACPEvent, ACPEventType, PlanEntryInfo, PlanInfo
from src.card.events import CardEvent, CardEventType
from src.spec_engine.models import SpecPhase


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRendererCtx:
    def __init__(self):
        self.spec_reporter = MagicMock()
        self.spec_reporter.format_analyzing_done.return_value = "analyzing done"
        self.spec_reporter.format_criteria_section.return_value = ""
        self.spec_reporter.format_phase_subtitle.return_value = ""
        self.spec_reporter.format_phase_done_content.return_value = ""
        self.spec_reporter.format_cycle_done.return_value = ""
        self.spec_reporter.format_review_result.return_value = ""
        self.spec_reporter.format_project_done.return_value = ""
        self.spec_reporter._extract_phase_summary.return_value = ""
        self.spec_engine_manager = MagicMock()
        self.spec_engine_manager.snapshot.return_value = None
        self.spec_engine_manager.snapshot_active.return_value = []


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


def _make_plan_event(entries: list[tuple[str, str]]) -> ACPEvent:
    plan_entries = [PlanEntryInfo(content=c, status=s) for c, s in entries]
    return ACPEvent(
        event_type=ACPEventType.PLAN_UPDATE,
        plan=PlanInfo(entries=plan_entries),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSpecRendererMultiCard:
    """Verify multi-card creation for Spec mode with task orchestration."""

    def _setup_renderer(self):
        from src.feishu.renderers.spec_renderer import SpecRenderer

        tracker = SessionTracker()
        handler = FakeHandler()

        renderer = SpecRenderer(handler)
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

    def test_multi_task_plan_in_build_creates_extra_cards(self):
        """PLAN_UPDATE with 3 tasks in BUILD phase → 3 extra cards created."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_spec_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        # Initial: 1 card (rotator)
        assert tracker.create_card_count == 1

        # Simulate BUILD phase event with multi-task plan
        plan_event = _make_plan_event([
            ("Implement feature A", "pending"),
            ("Implement feature B", "pending"),
            ("Add error handling", "pending"),
        ])
        callbacks.on_phase_event(1, SpecPhase.BUILD, plan_event)

        # Should have 1 (rotator) + 3 (task sessions) = 4
        assert tracker.create_card_count == 4

    def test_plan_in_non_build_phase_no_split(self):
        """PLAN_UPDATE in SPEC/PLAN phase doesn't trigger multi-card."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_spec_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        plan_event = _make_plan_event([
            ("Step A", "pending"),
            ("Step B", "pending"),
            ("Step C", "pending"),
        ])
        # SPEC phase, not BUILD
        callbacks.on_phase_event(1, SpecPhase.SPEC, plan_event)

        # No split (only in BUILD phase)
        assert tracker.create_card_count == 1

    def test_single_step_plan_in_build_no_split(self):
        """Single-step plan in BUILD doesn't trigger multi-card."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_spec_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        plan_event = _make_plan_event([("Single task", "pending")])
        callbacks.on_phase_event(1, SpecPhase.BUILD, plan_event)

        # No split
        assert tracker.create_card_count == 1

    def test_no_plan_stays_single_card(self):
        """Without PLAN_UPDATE events, stays in single card."""
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_spec_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        # Send text event in BUILD phase
        text_event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="coding...")
        callbacks.on_phase_event(1, SpecPhase.BUILD, text_event)

        # Still just 1 card
        assert tracker.create_card_count == 1

    def test_ac1_create_card_equals_task_count_plus_one(self):
        """AC1: create_card calls == task_count + 1 (thinking/rotator card).

        For 4 subtasks in BUILD phase, total create_card calls should be exactly 5:
        1 (thinking/rotator) + 4 (per-task cards).
        """
        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_spec_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        task_count = 4
        plan_event = _make_plan_event([
            ("Implement auth module", "pending"),
            ("Add database migration", "pending"),
            ("Write integration tests", "pending"),
            ("Update API documentation", "pending"),
        ])
        callbacks.on_phase_event(1, SpecPhase.BUILD, plan_event)

        expected_card_count = task_count + 1  # +1 for thinking/rotator card
        assert tracker.create_card_count == expected_card_count, (
            f"AC1 violation: expected {expected_card_count} create_card calls "
            f"(task_count={task_count} + 1 thinking), got {tracker.create_card_count}"
        )

    def test_route_or_fallback_called_in_build(self):
        """After plan reception in BUILD phase, events are routed through route_acp_event."""
        from unittest.mock import patch as mock_patch
        from src.card.orchestrator import TaskOrchestrator

        renderer, tracker = self._setup_renderer()

        callbacks = renderer.create_spec_callbacks(
            message_id="msg_1",
            chat_id="chat_1",
            project=None,
        )

        # Send a multi-task plan first
        plan_event = _make_plan_event([
            ("Task A", "pending"),
            ("Task B", "pending"),
        ])
        callbacks.on_phase_event(1, SpecPhase.BUILD, plan_event)

        # Now send a text event in BUILD — should go through route_acp_event
        text_event = ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="building...")
        original_route = TaskOrchestrator.route_acp_event
        call_count = []

        def _spy(self_orch, *args, **kwargs):
            call_count.append(1)
            return original_route(self_orch, *args, **kwargs)

        with mock_patch.object(TaskOrchestrator, "route_acp_event", _spy):
            callbacks.on_phase_event(1, SpecPhase.BUILD, text_event)
            assert len(call_count) > 0, "route_acp_event was not called"

