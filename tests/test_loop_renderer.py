from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.acp import ACPEventType
from src.card.engine_snapshot import EngineSnapshot
from src.card.events import CardEvent, CardEventType
from src.card.state.models import CardMetadata
from src.feishu.renderers.loop_renderer import LoopRenderer
from src.loop_engine.models import LoopProject, LoopProjectStatus
from src.project import ProjectContext


class TestLoopRenderer:
    @pytest.fixture
    def mock_handler(self):
        handler = MagicMock()
        handler.ctx = MagicMock()
        handler.settings = MagicMock()
        handler.settings.card.deep_compact_default = False
        handler.settings.default_reply_mode = "thread"

        # Mock reporter
        reporter = handler.ctx.loop_reporter
        reporter.format_status.return_value = "Status Content"
        reporter.get_status_title.return_value = "Status Title"
        reporter._make_progress_bar.return_value = "[====]"
        reporter.format_status_line.return_value = "Status Line"
        reporter.format_duration_line.return_value = "Duration Line"
        reporter.format_criteria_section.return_value = "Criteria Section"
        reporter.format_iteration_start.return_value = "Iteration Start Content"
        reporter.get_iteration_start_title.return_value = "Iteration Start Title"
        reporter.get_progress_info.return_value = {"progress_bar": "[=]", "is_running": True, "is_paused": False}

        return handler

    def test_init(self, mock_handler):
        renderer = LoopRenderer(mock_handler)
        assert renderer.handler == mock_handler
        assert renderer.ctx == mock_handler.ctx
        assert renderer.ui_states == {}

    def test_get_ui_state(self, mock_handler):
        renderer = LoopRenderer(mock_handler)
        state = renderer.get_ui_state("proj1")
        assert state["compact"] is False
        assert state["view_mode"] == "status"

        # Test state persistence
        state["compact"] = True
        state2 = renderer.get_ui_state("proj1")
        assert state2["compact"] is True

    def test_render_current_view_status(self, mock_handler):
        renderer = LoopRenderer(mock_handler)

        # Mock Project and Engine
        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"
        proj.project_name = "test_proj"

        mock_handler.project_manager.get_active_project.return_value = proj

        mock_loop_project = MagicMock(spec=LoopProject)
        mock_loop_project.name = "test_proj"
        mock_loop_project.root_path = "/tmp/p1"
        mock_loop_project.project_id = "p1"
        mock_loop_project.status = LoopProjectStatus.RUNNING
        mock_loop_project.satisfied_count = 2
        mock_loop_project.total_criteria = 5
        mock_loop_project.duration.return_value = 10.0
        mock_loop_project.iterations = []

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/p1",
            satisfied_count=2,
            total_criteria=5,
            is_running=True,
            ext={"project": mock_loop_project},
        )
        mock_handler.ctx.loop_engine_manager.snapshot.return_value = snap

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            renderer.render_current_view("msg1", "chat1", project=proj)

            # Verify session was created and events dispatched
            mock_create.assert_called_once()
            dispatched_events = [call[0][0] for call in mock_session.dispatch.call_args_list]
            event_types = [e.type for e in dispatched_events]
            assert CardEventType.STARTED in event_types
            assert CardEventType.TEXT_DELTA in event_types
            assert CardEventType.CRITERIA_UPDATED in event_types

    def test_callbacks_creation(self, mock_handler):
        renderer = LoopRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        # Mock engine manager for callbacks to find the project
        mock_loop_project = MagicMock(spec=LoopProject)
        mock_loop_project.name = "test_proj"
        mock_loop_project.root_path = "/tmp/p1"
        mock_loop_project.project_id = "p1"
        mock_loop_project.satisfied_count = 3
        mock_loop_project.total_criteria = 5
        mock_loop_project.duration.return_value = 10.0

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/p1",
            satisfied_count=3,
            total_criteria=5,
            is_running=True,
            ext={"project": mock_loop_project},
        )
        mock_handler.ctx.loop_engine_manager.snapshot.return_value = snap

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            callbacks = renderer.create_loop_callbacks("msg1", "chat1", proj)

            assert callbacks.on_iteration_start
            assert callbacks.on_iteration_done
            assert callbacks.on_project_done

            # Test on_iteration_start dispatches CYCLE_STARTED + CRITERIA_UPDATED
            callbacks.on_iteration_start(1, 5)

            state = renderer.get_ui_state("p1")
            assert state["view_mode"] == "status"

            # Verify dispatch was called with CYCLE_STARTED
            dispatched_events = [call[0][0] for call in mock_session.dispatch.call_args_list]
            event_types = [e.type for e in dispatched_events]
            assert CardEventType.CYCLE_STARTED in event_types
            assert CardEventType.CRITERIA_UPDATED in event_types

            # Test error callback dispatches FAILED event
            mock_session.reset_mock()
            callbacks.on_error("Test Loop Error")

            dispatched_events = [call[0][0] for call in mock_session.dispatch.call_args_list]
            event_types = [e.type for e in dispatched_events]
            assert CardEventType.FAILED in event_types
            # Verify error message is in the payload
            failed_event = next(e for e in dispatched_events if e.type == CardEventType.FAILED)
            assert failed_event.payload["error"] == "Test Loop Error"

    def test_iteration_events_stream_tool_calls_to_current_iteration_card(self, mock_handler):
        renderer = LoopRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        mock_loop_project = MagicMock(spec=LoopProject)
        mock_loop_project.name = "test_proj"
        mock_loop_project.root_path = "/tmp/p1"
        mock_loop_project.project_id = "p1"
        mock_loop_project.satisfied_count = 1
        mock_loop_project.total_criteria = 3
        mock_loop_project.duration.return_value = 5.0

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/p1",
            satisfied_count=1,
            total_criteria=3,
            is_running=True,
            ext={"project": mock_loop_project},
        )
        mock_handler.ctx.loop_engine_manager.snapshot.return_value = snap

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_session.closed = False
            mock_session.delivered_message_id = "msg-loop-1"
            mock_create.return_value = mock_session

            callbacks = renderer.create_loop_callbacks("msg1", "chat1", proj)

            assert callbacks.on_iteration_event is not None

            tool_event = SimpleNamespace(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=SimpleNamespace(id="tool-1", title="Read", content="README.md", status="running"),
            )

            mock_session.dispatch.reset_mock()
            callbacks.on_iteration_event(1, tool_event)

            dispatched_events = [call.args[0] for call in mock_session.dispatch.call_args_list]
            event_types = [event.type for event in dispatched_events]
            assert CardEventType.TOOL_STARTED in event_types

    def test_first_iteration_rotates_to_iteration_scoped_card(self, mock_handler):
        renderer = LoopRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        mock_loop_project = MagicMock(spec=LoopProject)
        mock_loop_project.name = "test_proj"
        mock_loop_project.root_path = "/tmp/p1"
        mock_loop_project.project_id = "p1"
        mock_loop_project.satisfied_count = 0
        mock_loop_project.total_criteria = 2
        mock_loop_project.duration.return_value = 2.0

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/p1",
            satisfied_count=0,
            total_criteria=2,
            is_running=True,
            ext={"project": mock_loop_project},
        )
        mock_handler.ctx.loop_engine_manager.snapshot.return_value = snap

        created_metadata: list[CardMetadata] = []

        def fake_create_session(chat_id, reply_to, metadata, hooks=(), budget=None):
            session = MagicMock()
            session.closed = False
            session.delivered_message_id = f"delivered-{len(created_metadata)}"
            created_metadata.append(metadata)
            return session

        with patch("src.feishu.renderers.base.BaseRenderer.create_session", side_effect=fake_create_session):
            callbacks = renderer.create_loop_callbacks("msg1", "chat1", proj)
            callbacks.on_iteration_start(1, 3)

        assert len(created_metadata) >= 2
        assert created_metadata[1].unit_label == "第 1 轮"

    def test_error_closes_active_stream_blocks_before_failed(self, mock_handler):
        renderer = LoopRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        mock_loop_project = MagicMock(spec=LoopProject)
        mock_loop_project.name = "test_proj"
        mock_loop_project.root_path = "/tmp/p1"
        mock_loop_project.project_id = "p1"
        mock_loop_project.satisfied_count = 0
        mock_loop_project.total_criteria = 2
        mock_loop_project.duration.return_value = 2.0

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/p1",
            satisfied_count=0,
            total_criteria=2,
            is_running=True,
            ext={"project": mock_loop_project},
        )
        mock_handler.ctx.loop_engine_manager.snapshot.return_value = snap

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_session.closed = False
            mock_session.delivered_message_id = "msg-loop-1"
            mock_create.return_value = mock_session

            callbacks = renderer.create_loop_callbacks("msg1", "chat1", proj)

            text_event = SimpleNamespace(
                event_type=ACPEventType.TEXT_CHUNK,
                text="hello",
                tool_call=None,
            )
            callbacks.on_iteration_event(1, text_event)
            mock_session.dispatch.reset_mock()

            callbacks.on_error("boom")

            dispatched_events = [call.args[0] for call in mock_session.dispatch.call_args_list]
            event_types = [event.type for event in dispatched_events]
            assert event_types[:2] == [CardEventType.TEXT_DONE, CardEventType.FAILED]


class TestLoopRendererCreateRotator:
    """Integration test for LoopRenderer._create_rotator() path."""

    @pytest.fixture
    def mock_handler(self):
        handler = MagicMock()
        handler.ctx = MagicMock()
        handler.settings = MagicMock()
        handler.settings.card.deep_compact_default = False
        handler.settings.default_reply_mode = "thread"
        handler.get_card_delivery.return_value = MagicMock()
        return handler

    def test_create_rotator_returns_session_rotator(self, mock_handler):
        """_create_rotator returns a SessionRotator wrapping a valid session."""
        renderer = LoopRenderer(mock_handler)
        metadata = CardMetadata(engine_type="loop", mode_name="Loop", mode_emoji="🔁")

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_session.closed = False
            mock_session.session_id = "test-session-id"
            mock_create.return_value = mock_session

            rotator = renderer._create_rotator("chat1", "msg1", metadata)

        from src.card.session.rotator import SessionRotator
        assert isinstance(rotator, SessionRotator)
        mock_create.assert_called_once_with(
            "chat1", "msg1", metadata, hooks=(), budget=None,
        )

    def test_create_rotator_passes_hooks_and_budget(self, mock_handler):
        """_create_rotator correctly passes hooks and budget to create_session."""
        from src.card.render.budget import RenderBudget

        renderer = LoopRenderer(mock_handler)
        metadata = CardMetadata(engine_type="loop", mode_name="Loop", mode_emoji="🔁")
        test_hooks = (MagicMock(),)
        test_budget = RenderBudget(engine_cmd="/loop")

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_session.closed = False
            mock_create.return_value = mock_session

            renderer._create_rotator("chat2", "msg2", metadata, hooks=test_hooks, budget=test_budget)

        mock_create.assert_called_once_with(
            "chat2", "msg2", metadata, hooks=test_hooks, budget=test_budget,
        )


class TestLoopRendererContinuationSeq:
    """Verify continuation_seq increments on session rotation."""

    @pytest.fixture
    def mock_handler(self):
        handler = MagicMock()
        handler.ctx = MagicMock()
        handler.settings = MagicMock()
        handler.settings.card.deep_compact_default = False
        handler.settings.default_reply_mode = "thread"
        handler.get_card_delivery.return_value = MagicMock()
        return handler

    def test_continuation_seq_increments_on_rotation(self, mock_handler):
        """After rotating twice, continuation_seq should be 2 on the second rotation."""
        from src.card.session.rotator import SessionRotator

        renderer = LoopRenderer(mock_handler)

        created_sessions = []

        def fake_create_session(chat_id, reply_to, metadata, hooks=(), budget=None):
            session = MagicMock()
            session.closed = False
            session.session_id = f"session-{len(created_sessions)}"
            session.delivered_message_id = f"msg-{len(created_sessions)}"
            created_sessions.append((session, metadata))
            return session

        with patch("src.feishu.renderers.base.BaseRenderer.create_session", side_effect=fake_create_session):
            metadata = CardMetadata(engine_type="loop", mode_name="Loop · Coco", mode_emoji="🔁")
            rotator = renderer._create_rotator("chat1", "msg1", metadata)

            # Initial session (continuation_seq defaults to 0)
            assert rotator.rotation_count == 0

            # Simulate iteration boundary rotation via rotator.rotate
            rotator.rotate(lambda: fake_create_session(
                "chat1", "msg-0",
                CardMetadata(engine_type="loop", mode_name="Loop · Coco", mode_emoji="🔁", continuation_seq=rotator.rotation_count + 1),
            ))
            assert rotator.rotation_count == 1

            # Second rotation
            rotator.rotate(lambda: fake_create_session(
                "chat1", "msg-1",
                CardMetadata(engine_type="loop", mode_name="Loop · Coco", mode_emoji="🔁", continuation_seq=rotator.rotation_count + 1),
            ))
            assert rotator.rotation_count == 2

            # Verify the metadata passed in second rotation had continuation_seq=2
            _, meta2 = created_sessions[-1]
            assert meta2.continuation_seq == 2
