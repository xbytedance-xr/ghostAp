import unittest
from unittest.mock import MagicMock, patch

import pytest

from src.acp import ACPEvent, ACPEventType, ToolCallInfo
from src.card.engine_snapshot import EngineSnapshot
from src.deep_engine.models import DeepProject, DeepProjectStatus
from src.feishu.handlers.deep import DeepHandler
from src.feishu.renderers.deep_renderer import DeepRenderer
from src.project import ProjectContext


class TestDeepRenderer:
    @pytest.fixture
    def mock_handler(self):
        handler = MagicMock()
        handler.ctx = MagicMock()
        handler.settings = MagicMock()
        handler.settings.card.deep_compact_default = False
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
            "completed": 1,
            "total": 10,
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

        snap = EngineSnapshot(
            engine_name="Coco",
            root_path="/tmp/p1",
            project_id="p1",
            completed_steps=1,
            total_steps=10,
            duration_seconds=5.0,
            status="executing",
            is_running=True,
            ext={"project": engine.project, "progress": engine.progress},
        )
        mock_handler.ctx.deep_engine_manager.snapshot.return_value = snap
        mock_handler.ctx.deep_engine_manager.get.return_value = engine

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            renderer.render_deep_status("msg1", "chat1", project=proj)

            # Verify CardSession was created and events were dispatched
            mock_create.assert_called_once()
            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            # Should have: started, text_delta (status content), progress_updated
            assert "started" in types
            assert "text_delta" in types
            # Verify status content was included in text_delta
            text_deltas = [c for c in calls if c.type.value == "text_delta"]
            assert any("Status Content" in c.payload.get("text", "") for c in text_deltas)
            assert any("Status Title" in c.payload.get("text", "") for c in text_deltas)

    def test_render_deep_status_no_engine(self, mock_handler):
        """Test render_deep_status when no engine is running."""
        renderer = DeepRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        mock_handler.project_manager.get_active_project.return_value = proj
        mock_handler.ctx.deep_engine_manager.get.return_value = None
        mock_handler.ctx.deep_engine_manager.get_active_engines.return_value = []
        mock_handler.ctx.deep_engine_manager.snapshot.return_value = None
        mock_handler.ctx.deep_engine_manager.snapshot_active.return_value = []
        mock_handler.get_engine_name.return_value = "Coco"

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            renderer.render_deep_status("msg1", "chat1", project=proj)

            # Should dispatch started + text_delta + completed
            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "started" in types
            assert "text_delta" in types
            assert "completed" in types

    def test_callbacks_creation(self, mock_handler):
        renderer = DeepRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        # Mock engine manager for callbacks to find the project
        engine = MagicMock()
        engine.project = DeepProject(name="test_proj", root_path="/tmp/p1", project_id="p1")
        engine.project.duration = MagicMock(return_value=10.0)
        engine.progress.format_summary.return_value = "Progress Summary"
        engine.progress.progress_bar = "[====]"
        engine.progress.total_steps = 0
        engine.progress.tool_calls = ["tc1", "tc2"]
        engine.get_rendered_content.return_value = "Rendered Content"

        mock_handler.ctx.deep_engine_manager.get.return_value = engine

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create:
            mock_session = MagicMock()
            mock_create.return_value = mock_session

            callbacks = renderer.create_deep_callbacks("msg1", "chat1", proj)

            assert callbacks.on_analyzing_done
            assert callbacks.on_event
            assert callbacks.on_project_done
            assert callbacks.on_error

            # Verify hooks were passed to create_session
            call_kwargs = mock_create.call_args
            assert "hooks" in call_kwargs.kwargs or (len(call_kwargs.args) > 4)

            # Test project done callback dispatches completed with summary
            engine.project.status = DeepProjectStatus.COMPLETED
            callbacks.on_project_done(engine.project)

            # Verify dispatch was called with completed event containing summary
            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "completed" in types
            completed_event = next(c for c in calls if c.type.value == "completed")
            assert "summary" in completed_event.payload
            assert "工具调用" in completed_event.payload["summary"]

            # Test error callback dispatches failed with error text
            mock_session.dispatch.reset_mock()
            callbacks.on_error("Test Error")

            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "failed" in types
            failed_event = next(c for c in calls if c.type.value == "failed")
            assert failed_event.payload["error"] == "Test Error"

    def test_callbacks_progress_tracking(self, mock_handler):
        """Test that on_event dispatches progress_updated for tool calls."""
        renderer = DeepRenderer(mock_handler)

        proj = MagicMock(spec=ProjectContext)
        proj.project_id = "p1"
        proj.root_path = "/tmp/p1"

        mock_handler.ctx.deep_engine_manager.get.return_value = None

        with patch("src.feishu.renderers.base.BaseRenderer.create_session") as mock_create, \
             patch("src.feishu.renderers._base_stream_processor.ACPEventRenderer") as mock_renderer_cls:
            mock_session = MagicMock()
            mock_create.return_value = mock_session
            mock_renderer_cls.return_value = MagicMock()

            callbacks = renderer.create_deep_callbacks("msg1", "chat1", proj)

            # Simulate analyzing done
            dp = DeepProject(name="test", root_path="/tmp/p1", project_id="p1")
            callbacks.on_analyzing_done(dp)

            # Simulate a tool call event
            tool_event = ACPEvent(
                event_type=ACPEventType.TOOL_CALL_START,
                tool_call=ToolCallInfo(
                    id="tool-1",
                    title="write_file",
                    kind="execute",
                    status="in_progress",
                    content="{}",
                ),
            )

            mock_session.dispatch.reset_mock()
            callbacks.on_event(tool_event)

            # Should have progress_updated dispatched
            calls = [c.args[0] for c in mock_session.dispatch.call_args_list]
            types = [c.type.value for c in calls]
            assert "progress_updated" in types


# ---------------------------------------------------------------------------
# Tests merged from test_deep_renderer_refactor.py
# ---------------------------------------------------------------------------


class TestDeepRendererRefactor(unittest.TestCase):
    def setUp(self):
        self.mock_handler = MagicMock(spec=DeepHandler)
        self.mock_handler.ctx = MagicMock()
        self.mock_handler.settings = MagicMock()
        self.mock_handler.settings.card.deep_compact_default = False
        self.renderer = DeepRenderer(self.mock_handler)

    def test_inheritance(self):
        """Verify DeepRenderer inherits from BaseRenderer"""
        self.assertTrue(hasattr(self.renderer, "_render_collapsible_section"))

    def test_ui_state_defaults(self):
        """Verify DeepRenderer specific UI defaults"""
        state = self.renderer.get_default_ui_state()
        self.assertIn("expand_ac", state)
        self.assertFalse(state["expand_ac"])

    def test_text_collapsing(self):
        """Verify text collapsing works for Markdown content (typical for Deep mode)"""
        # Long text with multiple paragraphs/lines — must exceed COLLAPSE_LINE_THRESHOLD (30)
        long_content = "\n".join([f"Step {i}: Thinking process..." for i in range(40)])

        # Should be collapsed by default
        collapsed = self.renderer._render_collapsible_section(long_content, total_items=40, expanded=False)
        self.assertIn("📄 内容较长", collapsed)
        self.assertIn("Step 0", collapsed)
        self.assertNotIn("Step 39", collapsed)

        # Should be full when expanded
        expanded_content = self.renderer._render_collapsible_section(long_content, total_items=40, expanded=True)
        self.assertEqual(expanded_content, long_content)
