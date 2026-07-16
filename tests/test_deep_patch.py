from unittest.mock import MagicMock, patch

import pytest

from src.card.engine_snapshot import EngineSnapshot
from src.deep_engine.models import DeepProjectStatus
from src.feishu.handlers.deep import DeepHandler


class TestDeepHandlerPatch:
    @pytest.fixture
    def handler(self):
        # Mock dependencies
        ctx = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.progress_reporter = MagicMock()

        # Mock settings in context
        ctx.settings = MagicMock()
        ctx.settings.default_reply_mode = "thread"

        handler = DeepHandler(ctx)
        # Mock the new API methods used by CardSession delivery
        handler.reply_card = MagicMock(return_value="mock_reply_id")
        handler.update_card = MagicMock(return_value=True)
        return handler

    def test_deep_callbacks_dispatches_started_on_analyzing_done(self, handler):
        """Verify on_analyzing_done dispatches STARTED + Spec-style phase events."""
        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            callbacks = handler._create_deep_callbacks(
                message_id="msg_123", chat_id="chat_123", project=None, initial_message_id="init_msg_id"
            )

            mock_project = MagicMock()
            mock_project.name = "Test Project"
            mock_project.root_path = "/tmp"
            mock_project.project_id = "proj_123"

            callbacks.on_analyzing_done(mock_project)

            # Deep runtime cards reuse the Spec-style cycle/phase structure.
            assert mock_session.dispatch.call_count >= 3
            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "started" in types
            assert "cycle_started" in types
            assert "phase_started" in types
            assert "text_started" not in types
            phase_event = next(c for c in calls if c.type.value == "phase_started")
            assert phase_event.payload["phase"] == "analyzing"

    def test_deep_callbacks_dispatches_completed_on_project_done(self, handler):
        """Verify that on_project_done dispatches COMPLETED to CardSession."""
        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            callbacks = handler._create_deep_callbacks(
                message_id="msg_123", chat_id="chat_123", project=None, initial_message_id=None
            )

            mock_project = MagicMock()
            mock_project.name = "Test Project"
            mock_project.root_path = "/tmp"
            mock_project.project_id = "proj_123"
            mock_project.status = DeepProjectStatus.COMPLETED
            mock_project.duration.return_value = 313.6

            callbacks.on_project_done(mock_project)

            # Should dispatch completed
            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "completed" in types

    def test_deep_callbacks_dispatches_failed_on_error(self, handler):
        """Verify that on_error dispatches FAILED to CardSession."""
        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            callbacks = handler._create_deep_callbacks(
                message_id="msg_123", chat_id="chat_123", project=None, initial_message_id=None
            )

            callbacks.on_error("Some error")

            # Should dispatch failed
            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "failed" in types


class TestDeepStatusPatch:
    @pytest.fixture
    def handler(self):
        # Mock dependencies
        ctx = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.progress_reporter = MagicMock()
        ctx.project_manager = MagicMock()
        ctx.deep_engine_manager = MagicMock()

        # Mock settings
        ctx.settings = MagicMock()

        handler = DeepHandler(ctx)

        # Mock common methods
        handler.reply_text = MagicMock()
        handler.get_working_dir = MagicMock(return_value="/tmp")
        handler.get_engine_name = MagicMock(return_value="Coco")

        return handler

    def test_show_deep_status_dispatches_events(self, handler):
        """Verify render_deep_status dispatches correct events to CardSession."""
        # Mock engine and project
        mock_engine = MagicMock()
        mock_project = MagicMock()
        mock_project.status = DeepProjectStatus.IDLE
        mock_project.project_id = "p1"
        mock_engine.project = mock_project
        mock_engine.engine_name = "Coco"
        mock_engine.progress.completed_steps = 0
        mock_engine.progress.total_steps = 0

        # Setup snapshot mock
        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/p1",
            project_id="p1",
            completed_steps=0,
            total_steps=0,
            duration_seconds=10.0,
            status="idle",
            is_running=False,
            ext={"project": mock_project, "progress": mock_engine.progress},
        )
        handler.ctx.deep_engine_manager.snapshot.return_value = snap
        handler.ctx.deep_engine_manager.get.return_value = mock_engine
        handler.ctx.progress_reporter.get_progress_info.return_value = {
            "progress_bar": None,
            "is_executing": False,
            "is_paused": False,
            "project_id": "p1",
            "completed": 0,
            "total": 0,
        }

        # Mock settings
        handler.settings.card.deep_compact_default = False

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            handler.show_deep_status("msg1", "chat1", project=mock_project, origin_message_id="origin1")

            # Verify CardSession was created
            mock_create.assert_called_once()

            # Verify events dispatched: started + text_delta + completed (not executing)
            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "started" in types
            assert "text_delta" in types
            assert "completed" in types

    def test_show_deep_status_executing_no_terminal(self, handler):
        """Verify render_deep_status does NOT dispatch completed when still executing."""
        mock_engine = MagicMock()
        mock_project = MagicMock()
        mock_project.status = DeepProjectStatus.EXECUTING
        mock_project.project_id = "p1"
        mock_engine.project = mock_project
        mock_engine.engine_name = "Coco"
        mock_engine.progress.completed_steps = 3
        mock_engine.progress.total_steps = 10

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/p1",
            project_id="p1",
            completed_steps=3,
            total_steps=10,
            duration_seconds=30.0,
            status="executing",
            is_running=True,
            ext={"project": mock_project, "progress": mock_engine.progress},
        )
        handler.ctx.deep_engine_manager.snapshot.return_value = snap
        handler.ctx.deep_engine_manager.get.return_value = mock_engine
        handler.ctx.progress_reporter.get_progress_info.return_value = {
            "progress_bar": "[===       ]",
            "is_executing": True,
            "is_paused": False,
            "project_id": "p1",
            "completed": 3,
            "total": 10,
        }

        handler.settings.card.deep_compact_default = False

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            handler.show_deep_status("msg1", "chat1", project=mock_project)

            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "started" in types
            assert "text_delta" in types
            assert "progress_updated" in types
            # Should NOT have completed since still executing
            assert "completed" not in types

    def test_show_deep_status_no_engine(self, handler):
        """Verify render_deep_status shows no-task message when no engine."""
        handler.ctx.deep_engine_manager.get.return_value = None
        handler.ctx.deep_engine_manager.get_active_engines.return_value = []
        handler.ctx.deep_engine_manager.snapshot.return_value = None
        handler.ctx.deep_engine_manager.snapshot_active.return_value = []

        mock_project = MagicMock()
        mock_project.project_id = "p1"
        mock_project.root_path = "/tmp/p1"

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            handler.show_deep_status("msg1", "chat1", project=mock_project)

            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "started" in types
            assert "text_delta" in types
            assert "completed" in types
            # Verify no-task message content
            text_events = [c for c in calls if c.type.value == "text_delta"]
            assert any("没有进行中的任务" in c.payload.get("text", "") for c in text_events)
