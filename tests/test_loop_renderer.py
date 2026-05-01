from unittest.mock import MagicMock

import pytest

from src.feishu.renderers.loop_renderer import LoopRenderer
from src.loop_engine.models import LoopProject, LoopProjectStatus
from src.project import ProjectContext


class TestLoopRenderer:
    @pytest.fixture
    def mock_handler(self):
        handler = MagicMock()
        handler.ctx = MagicMock()
        handler.settings = MagicMock()
        handler.settings.card_deep_compact_default = False
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

        engine = MagicMock()
        engine.project = LoopProject(name="test_proj", root_path="/tmp/p1", project_id="p1")
        engine.project.status = LoopProjectStatus.RUNNING
        engine.engine_name = "Coco"

        mock_handler.ctx.loop_engine_manager.get.return_value = engine

        # Setup update_card to fail so it calls reply_card
        mock_handler.update_card.return_value = False

        renderer.render_current_view("msg1", "chat1", project=proj)

        # Verify it called reply_card with correct content
        assert mock_handler.reply_card.called
        args, kwargs = mock_handler.reply_card.call_args
        card_content = args[1]
        assert "Status Content" in card_content
        assert "Status Title" in card_content

    def test_callbacks_creation(self, mock_handler):
        renderer = LoopRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        # Mock engine manager for callbacks to find the project
        engine = MagicMock()
        engine.project = LoopProject(name="test_proj", root_path="/tmp/p1", project_id="p1")
        engine.project.duration = MagicMock(return_value=10.0)  # Return float

        mock_handler.ctx.loop_engine_manager.get.return_value = engine

        callbacks = renderer.create_loop_callbacks("msg1", "chat1", proj)

        assert callbacks.on_iteration_start
        assert callbacks.on_iteration_done
        assert callbacks.on_project_done

        # Test callback execution
        # Verify on_iteration_start updates state and sends message
        mock_handler.reply_card.return_value = "msg_thread"
        mock_handler.update_card.return_value = True

        callbacks.on_iteration_start(1, 5)

        state = renderer.get_ui_state("p1")
        assert state["view_mode"] == "status"

        # With DirectCardSession mock: first send goes to reply_card (create path)
        assert mock_handler.reply_card.called
        
        # Test error callback and its retry button logic
        import json
        mock_handler.reply_card.reset_mock()
        mock_handler.update_card.reset_mock()
        
        # Setup mock reporter to return string for format_error
        mock_handler.ctx.loop_reporter.format_error.return_value = "Test Loop Error Content"
        mock_handler.ctx.loop_reporter.get_error_title.return_value = "Test Error Title"
        
        callbacks.on_error("Test Loop Error")
        
        # Error sends via session (update path since session already has message_id)
        assert mock_handler.update_card.called
        args, kwargs = mock_handler.update_card.call_args
        card_content = args[1]
        
        # Parse JSON and verify the extra_buttons (retry button) were added
        card_dict = json.loads(card_content)
        # Look through elements for the "重试" button in the loop_resume action
        card_elements_str = json.dumps(card_dict.get("body", {}).get("elements", []))
        
        assert "Test Loop Error" in card_elements_str or "Test Loop Error" in card_content
        assert "loop_resume" in card_elements_str
        assert ("重试" in card_elements_str) or ("\\u91cd\\u8bd5" in card_elements_str)
        assert "p1" in card_elements_str  # project_id should be included in the action
