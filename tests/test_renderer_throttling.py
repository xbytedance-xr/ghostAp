import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.acp import ACPEventType
from src.card.builder import CardBuilder
from src.deep_engine.models import DeepProjectStatus
from src.feishu.renderers.deep_renderer import DeepRenderer
from src.feishu.renderers.loop_renderer import LoopRenderer


class TestRendererThrottling:
    @pytest.fixture
    def mock_handler(self):
        handler = MagicMock()
        handler.ctx = MagicMock()
        handler.settings = MagicMock()

        # Configure settings for throttle testing
        handler.settings.deep_stream_interval = 0.0  # Instant for test
        handler.settings.deep_stream_min_chars = 0
        handler.settings.default_reply_mode = "thread"
        handler.settings.card_deep_compact_default = False

        # Mock reporter
        handler.ctx.progress_reporter.format_summary.return_value = "Mock Summary"
        handler.ctx.loop_reporter.format_analyzing_done.return_value = "Mock Analyzing Done"

        # Mock engines
        mock_engine = MagicMock()
        mock_engine.project = MagicMock()
        mock_engine.project.duration.return_value = 10.0
        mock_engine.project.total_criteria = 10
        mock_engine.project.satisfied_count = 5
        mock_engine.progress = MagicMock()
        mock_engine.progress.format_summary.return_value = "Mock Summary"
        mock_engine.get_rendered_content.return_value = "Mock Content"
        handler.ctx.deep_engine_manager.get.return_value = mock_engine
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        # Mock new card API methods
        handler.reply_card = MagicMock(return_value="reply_msg_id")
        handler.update_card = MagicMock(return_value=True)

        return handler

    def test_deep_renderer_throttling(self, mock_handler):
        """Test that DeepRenderer uses _StreamThrottle for streaming updates"""
        mock_handler.settings.engine_collapsible_enabled = False
        renderer = DeepRenderer(mock_handler)

        mock_project = MagicMock()
        mock_project.project_id = "test_proj"

        callbacks = renderer.create_deep_callbacks(
            "msg_id", "chat_id", mock_project, initial_message_id="existing_msg_id"
        )

        mock_event = MagicMock()
        mock_event.event_type = ACPEventType.TEXT_CHUNK
        mock_event.text = "some text content"

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            # First event creates the card via reply_card (session starts with no message_id)
            callbacks.on_event(mock_event)
            mock_handler.reply_card.assert_called()

            # Reset and send second event — should go through update_card
            mock_handler.reset_mock()
            mock_handler.reply_card = MagicMock(return_value="new_msg")
            mock_handler.update_card = MagicMock(return_value=True)

            callbacks.on_event(mock_event)
            mock_handler.update_card.assert_called()

    def test_deep_renderer_critical_update(self, mock_handler):
        """Test that critical updates (success/error) always send via session"""
        mock_handler.settings.engine_collapsible_enabled = False
        renderer = DeepRenderer(mock_handler)
        mock_project = MagicMock()
        mock_project.project_id = "test_proj"

        callbacks = renderer.create_deep_callbacks(
            "msg_id", "chat_id", mock_project, initial_message_id="reply_msg_id"
        )

        # First call to initialize session state (sets _message_id via reply_card)
        mock_event = MagicMock()
        mock_event.event_type = ACPEventType.TEXT_CHUNK
        mock_event.text = "chunk"

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")
            callbacks.on_event(mock_event)

        # Now reset for clean verification
        mock_handler.reset_mock()
        mock_handler.reply_card = MagicMock(return_value="new_msg_id")
        mock_handler.update_card = MagicMock(return_value=True)

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            mock_deep_project = MagicMock()
            mock_deep_project.status = DeepProjectStatus.COMPLETED

            callbacks.on_project_done(mock_deep_project)

            # Critical updates go through session.send() → update_card
            mock_handler.update_card.assert_called()

    def test_loop_renderer_throttling(self, mock_handler):
        """Test that LoopRenderer uses DirectCardSession for message delivery"""
        renderer = LoopRenderer(mock_handler)
        mock_project = MagicMock()
        mock_project.project_id = "test_proj"
        callbacks = renderer.create_loop_callbacks("msg_id", "chat_id", mock_project)

        with patch("src.feishu.renderers.loop_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")
            mock_loop_project = MagicMock()
            callbacks.on_analyzing_done(mock_loop_project)

            # First send goes through reply_card (create path)
            mock_handler.reply_card.assert_called()

            # Reset
            mock_handler.reset_mock()
            mock_handler.reply_card = MagicMock(return_value="new_msg")
            mock_handler.update_card = MagicMock(return_value=True)

            # on_iteration_start should update existing card via update_card
            callbacks.on_iteration_start(1, 5)

            # Subsequent sends go through update_card
            mock_handler.update_card.assert_called()

    def test_help_card_structure(self):
        """Test that build_help_card returns valid structure"""
        msg_type, content_json = CardBuilder.build_help_card(category="main")
        content = json.loads(content_json)

        assert msg_type == "interactive"
        assert "header" in content
        assert "body" in content

        # Check for categories
        elements = content["body"]["elements"]
        buttons_row = next((e for e in elements if e.get("tag") == "column_set"), None)
        assert buttons_row is not None

        # Check for content text
        text_elem = next(
            (e for e in elements if e.get("tag") == "markdown" and "编程模式" in e.get("content", "")), None
        )
        assert text_elem is not None
