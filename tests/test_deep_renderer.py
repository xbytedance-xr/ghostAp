from unittest.mock import MagicMock

import pytest

from src.deep_engine.models import DeepProject, DeepProjectStatus
from src.feishu.renderers.deep_renderer import DeepRenderer
from src.project import ProjectContext


class TestDeepRenderer:
    @pytest.fixture
    def mock_handler(self):
        handler = MagicMock()
        handler.ctx = MagicMock()
        handler.settings = MagicMock()
        handler.settings.card_deep_compact_default = False
        handler.settings.default_reply_mode = "thread"
        handler.settings.deep_stream_interval = 0.1
        handler.settings.deep_stream_min_chars = 1

        # Mock reporter
        reporter = handler.ctx.progress_reporter
        reporter.format_status.return_value = "Status Content"
        reporter.get_status_title.return_value = "Status Title"
        reporter.get_progress_info.return_value = {
            "progress_bar": "[=]",
            "is_executing": True,
            "is_paused": False,
            "project_id": "p1",
        }

        return handler

    def test_init(self, mock_handler):
        renderer = DeepRenderer(mock_handler)
        assert renderer.handler == mock_handler
        assert renderer.ctx == mock_handler.ctx
        assert renderer.ui_states == {}

    def test_get_ui_state(self, mock_handler):
        renderer = DeepRenderer(mock_handler)
        state = renderer.get_ui_state("proj1")
        assert state["compact"] is False
        assert state["expanded"] is False

        # Test state persistence
        state["compact"] = True
        state2 = renderer.get_ui_state("proj1")
        assert state2["compact"] is True

    def test_render_deep_status(self, mock_handler):
        renderer = DeepRenderer(mock_handler)

        # Mock Project and Engine
        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"
        proj.project_name = "test_proj"

        mock_handler.project_manager.get_active_project.return_value = proj

        engine = MagicMock()
        engine.project = DeepProject(name="test_proj", root_path="/tmp/p1", project_id="p1")
        engine.project.status = DeepProjectStatus.EXECUTING
        engine.engine_name = "Coco"
        engine.progress.completed_steps = 1
        engine.progress.total_steps = 10

        mock_handler.ctx.deep_engine_manager.get.return_value = engine

        # Setup patch_message to fail so it calls reply_message
        mock_handler.patch_message.return_value = False

        renderer.render_deep_status("msg1", "chat1", project=proj)

        # Verify it called reply_message with correct content
        assert mock_handler.reply_message.called
        args, kwargs = mock_handler.reply_message.call_args
        card_content = args[1]
        assert "Status Content" in card_content
        assert "Status Title" in card_content

    def test_callbacks_creation(self, mock_handler):
        renderer = DeepRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        # Mock engine manager for callbacks to find the project
        engine = MagicMock()
        engine.project = DeepProject(name="test_proj", root_path="/tmp/p1", project_id="p1")
        engine.project.duration = MagicMock(return_value=10.0)
        # Fix: ensure format_summary returns a string
        engine.progress.format_summary.return_value = "Progress Summary"
        engine.progress.progress_bar = "[====]"  # Fix: progress_bar should be string
        engine.get_rendered_content.return_value = "Rendered Content"

        mock_handler.ctx.deep_engine_manager.get.return_value = engine

        callbacks = renderer.create_deep_callbacks("msg1", "chat1", proj)

        assert callbacks.on_planning_done
        assert callbacks.on_event
        assert callbacks.on_project_done
        assert callbacks.on_error

        # Test project done callback
        mock_handler.reply_message.return_value = "msg_thread"
        mock_handler.patch_message.return_value = True

        callbacks.on_project_done(engine.project)

        assert mock_handler.add_reaction.called
        
        # Test error callback and its retry button logic
        import json
        mock_handler.reply_message.reset_mock()
        mock_handler.patch_message.reset_mock()
        mock_handler.patch_message.return_value = False
        
        # Setup mock reporter to return string for format_error
        mock_handler.ctx.progress_reporter.format_error.return_value = "Test Error Content"
        mock_handler.ctx.progress_reporter.get_error_title.return_value = "Test Error Title"
        
        callbacks.on_error("Test Error")
        
        # When error occurs, we should format the error card with retry button
        assert mock_handler.reply_message.called
        args, kwargs = mock_handler.reply_message.call_args
        card_content = args[1]
        
        # Parse JSON and verify the extra_buttons (retry button) were added
        card_dict = json.loads(card_content)
        # Look through elements for the "重试" button in the deep_resume action
        card_elements_str = json.dumps(card_dict.get("elements", []))
        
        assert "Test Error" in card_elements_str or "Test Error" in card_content
        assert "deep_resume" in card_elements_str
        assert "重试" in card_elements_str
        assert "p1" in card_elements_str  # project_id should be included in the action
