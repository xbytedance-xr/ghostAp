from unittest.mock import MagicMock, patch

import pytest

from src.card.engine_snapshot import EngineSnapshot
from src.card.events import CardEvent, CardEventType
from src.feishu.handlers.loop import LoopHandler
from src.loop_engine.models import IterationRecord, IterationStatus, LoopProject


class TestLoopHandlerPatch:
    @pytest.fixture
    def handler(self):
        # Mock dependencies
        ctx = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.loop_reporter = MagicMock()
        ctx.loop_reporter.format_criteria_section.return_value = "Mock Criteria"
        ctx.loop_engine_manager = MagicMock()

        # Mock settings in context
        ctx.settings = MagicMock()
        ctx.settings.default_reply_mode = "thread"

        handler = LoopHandler(ctx)

        # Mock new API methods
        handler.reply_card = MagicMock(return_value="msg_initial")
        handler.update_card = MagicMock(return_value=True)
        handler.ensure_request_id = MagicMock(return_value="req_123")
        handler.format_ref_note = MagicMock(return_value="")
        handler.add_reaction = MagicMock()

        return handler

    def test_loop_callbacks_dispatch_started(self, handler):
        """Test that on_analyzing_done dispatches STARTED + TEXT_DELTA events."""
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 0
        mock_project.total_criteria = 0
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            callbacks = handler._create_loop_callbacks(
                message_id="msg_origin", chat_id="chat_123", project=None, engine_name="Coco"
            )

            callbacks.on_analyzing_done(mock_project)

            # Verify STARTED + TEXT_DELTA dispatched
            dispatched = [call[0][0] for call in mock_session.dispatch.call_args_list]
            types = [e.type for e in dispatched]
            assert types == [CardEventType.STARTED, CardEventType.TEXT_DELTA]

    def test_loop_callbacks_iteration_start_dispatches_cycle(self, handler):
        """Test that on_iteration_start dispatches CYCLE_STARTED event."""
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 3
        mock_project.total_criteria = 5
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="",
            satisfied_count=3,
            total_criteria=5,
            is_running=True,
            ext={"project": mock_project},
        )
        handler.ctx.loop_engine_manager.snapshot.return_value = snap

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            callbacks = handler._create_loop_callbacks(
                message_id="msg_origin", chat_id="chat_123", project=None, engine_name="Coco"
            )

            mock_session.reset_mock()
            callbacks.on_iteration_start(1, 10)

            dispatched = [call[0][0] for call in mock_session.dispatch.call_args_list]
            types = [e.type for e in dispatched]
            assert CardEventType.CYCLE_STARTED in types
            assert CardEventType.CRITERIA_UPDATED in types

            # Verify cycle_started payload
            cycle_event = next(e for e in dispatched if e.type == CardEventType.CYCLE_STARTED)
            assert cycle_event.payload["cycle_num"] == 1
            assert cycle_event.payload["max_cycles"] == 10

    def test_loop_iteration_done_creates_new_session(self, handler):
        """Test that on_iteration_done closes session and creates new one."""
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 0
        mock_project.total_criteria = 0
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="",
            satisfied_count=0,
            total_criteria=0,
            is_running=True,
            ext={"project": mock_project},
        )
        handler.ctx.loop_engine_manager.snapshot.return_value = snap

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session_1 = MagicMock()
            mock_session_2 = MagicMock()
            mock_create.side_effect = [mock_session_1, mock_session_2]

            callbacks = handler._create_loop_callbacks("msg_origin", "chat_1", None)

            # Trigger iteration done
            record = MagicMock(spec=IterationRecord)
            record.status = IterationStatus.SUCCESS
            callbacks.on_iteration_done(1, record)

            # Session 1 should receive CYCLE_DONE and then ARCHIVED (stale stub via rotate)
            dispatched_1 = [call[0][0] for call in mock_session_1.dispatch.call_args_list]
            types_1 = [e.type for e in dispatched_1]
            assert CardEventType.CYCLE_DONE in types_1
            assert CardEventType.ARCHIVED in types_1

            # New session created and STARTED + TEXT_DELTA dispatched
            assert mock_create.call_count == 2
            dispatched_2 = [call[0][0] for call in mock_session_2.dispatch.call_args_list]
            types_2 = [e.type for e in dispatched_2]
            assert CardEventType.STARTED in types_2
            assert CardEventType.TEXT_DELTA in types_2

    def test_loop_project_done_dispatches_completed(self, handler):
        """Test that on_project_done dispatches COMPLETED event."""
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 5
        mock_project.total_criteria = 5
        mock_project.status.value = "completed"
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="",
            satisfied_count=5,
            total_criteria=5,
            is_running=False,
            ext={"project": mock_project},
        )
        handler.ctx.loop_engine_manager.snapshot.return_value = snap

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session_1 = MagicMock()
            mock_session_2 = MagicMock()
            mock_create.side_effect = [mock_session_1, mock_session_2]

            callbacks = handler._create_loop_callbacks("msg_origin", "chat_1", None)

            callbacks.on_project_done(mock_project)

            # Session 1 receives ARCHIVED event (stale stub via rotate)
            dispatched_1 = [call[0][0] for call in mock_session_1.dispatch.call_args_list]
            types_1 = [e.type for e in dispatched_1]
            assert CardEventType.ARCHIVED in types_1

            # New session dispatches STARTED + text + criteria + COMPLETED
            dispatched = [call[0][0] for call in mock_session_2.dispatch.call_args_list]
            types = [e.type for e in dispatched]
            assert CardEventType.STARTED in types
            assert CardEventType.COMPLETED in types
            assert CardEventType.CRITERIA_UPDATED in types

            # Verify EmojiHook was registered via hooks kwarg to create_session
            create_kwargs = mock_create.call_args_list[0]
            hooks = create_kwargs.kwargs.get("hooks", ())
            from src.card.hooks import EmojiHook
            assert any(isinstance(h, EmojiHook) for h in hooks), "EmojiHook should be registered"
