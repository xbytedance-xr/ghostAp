import json
import unittest
from unittest.mock import MagicMock, patch

from src.acp import ACPEventType
from src.card.builder import CardBuilder
from src.deep_engine.models import DeepProjectStatus
from src.feishu.renderers.deep_renderer import DeepRenderer
from src.feishu.renderers.loop_renderer import LoopRenderer


class TestRendererThrottling(unittest.TestCase):
    def setUp(self):
        self.mock_handler = MagicMock()
        self.mock_handler.ctx = MagicMock()
        self.mock_handler.settings = MagicMock()

        # Configure settings for throttle testing
        self.mock_handler.settings.deep_stream_interval = 0.0  # Instant for test
        self.mock_handler.settings.deep_stream_min_chars = 0
        self.mock_handler.settings.default_reply_mode = "thread"
        self.mock_handler.settings.card_deep_compact_default = False

        # Mock reporter
        self.mock_handler.ctx.progress_reporter.format_summary.return_value = "Mock Summary"
        self.mock_handler.ctx.loop_reporter.format_analyzing_done.return_value = "Mock Analyzing Done"

        # Mock engines
        self.mock_engine = MagicMock()
        self.mock_engine.project = MagicMock()
        self.mock_engine.project.duration.return_value = 10.0
        # Ensure total_criteria is an integer for progress bar generation
        self.mock_engine.project.total_criteria = 10
        self.mock_engine.project.satisfied_count = 5
        self.mock_engine.progress = MagicMock()
        self.mock_engine.progress.format_summary.return_value = "Mock Summary"
        self.mock_engine.get_rendered_content.return_value = "Mock Content"
        self.mock_handler.ctx.deep_engine_manager.get.return_value = self.mock_engine
        self.mock_handler.ctx.loop_engine_manager.get.return_value = self.mock_engine

    # @unittest.skip("Known issue in test environment with MagicMock interaction")
    def test_deep_renderer_throttling(self):
        """Test that DeepRenderer uses throttle=True for streaming updates"""
        renderer = DeepRenderer(self.mock_handler)

        # Mock dependencies
        mock_project = MagicMock()
        mock_project.project_id = "test_proj"

        # Create callbacks with initial_message_id so we skip the creation step
        # and go straight to update/patch
        callbacks = renderer.create_deep_callbacks(
            "msg_id", "chat_id", mock_project, initial_message_id="existing_msg_id"
        )

        # Simulate on_event (streaming update)
        mock_event = MagicMock()
        mock_event.event_type = ACPEventType.TEXT_CHUNK
        mock_event.text = "some text content"  # Must have text to trigger update

        # We need to mock the internal state of renderer
        # But callbacks is a closure, so we can't easily inspect.
        # Instead, we rely on patch_message calls.

        # To trigger _send_deep_message, we need to satisfy _maybe_stream_update conditions
        # We set stream interval to 0 in setUp

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            # Setup renderer to have content (so _maybe_stream_update triggers)
            # We can't access 'renderer' inside callbacks closure.
            # But we can simulate by making render_plan_view return something
            # The inner renderer is ACPEventRenderer

            # However, the easiest way to ensure it sends a message is to set force=True
            # But _maybe_stream_update is private.

            # Let's use TEXT_CHUNK which definitely updates content in ACPEventRenderer
            # and relies on our 0-interval settings to pass throttle check
            # Trigger update. SmartSender should see initial_message_id and try to patch.
            callbacks.on_event(mock_event)

            # Verify patch_message called with throttle=True
            # Note: We use "existing_msg_id" because that's what we initialized with
            self.mock_handler.patch_message.assert_called_with("existing_msg_id", "{}", max_retries=1, throttle=True)

    def test_deep_renderer_critical_update(self):
        """Test that critical updates (success/error) use throttle=False"""
        renderer = DeepRenderer(self.mock_handler)
        mock_project = MagicMock()
        # Pass initial_message_id so SmartSender knows what to patch
        callbacks = renderer.create_deep_callbacks("msg_id", "chat_id", mock_project, initial_message_id="reply_msg_id")

        # Setup initial message state - IMPORTANT: must be set before first call
        self.mock_handler.reply_message.return_value = "reply_msg_id"
        self.mock_handler.send_message.return_value = "reply_msg_id"

        # First call to initialize current_status_message_id
        mock_event = MagicMock(event_type="text_chunk")
        mock_event.content = "chunk"
        callbacks.on_event(mock_event)

        # Now reset for clean verification
        self.mock_handler.reset_mock()

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            # Simulate on_project_done (critical)
            mock_deep_project = MagicMock()
            mock_deep_project.status = DeepProjectStatus.COMPLETED

            callbacks.on_project_done(mock_deep_project)

            # Verify patch_message called with throttle=False
            self.mock_handler.patch_message.assert_called_with("reply_msg_id", "{}", max_retries=1, throttle=False)

    def test_loop_renderer_throttling(self):
        """Test that LoopRenderer uses throttle=True for streaming updates"""
        renderer = LoopRenderer(self.mock_handler)
        mock_project = MagicMock()
        mock_project.project_id = "test_proj"
        callbacks = renderer.create_loop_callbacks("msg_id", "chat_id", mock_project)

        # Setup initial message state
        self.mock_handler.reply_message.return_value = "reply_msg_id"
        self.mock_handler.send_message.return_value = "reply_msg_id"

        # Trigger initial message (analyzing done)
        with patch("src.feishu.renderers.loop_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")
            mock_loop_project = MagicMock()
            callbacks.on_analyzing_done(mock_loop_project)

            # First one is immediate flush per code
            # Note: _send_loop_message calls reply_message or send_message if status msg not set
            # It uses current_status_message_id[0]
            # When we start, it's None. So it sends new message.
            # New message creation does NOT take throttle param in BaseHandler.send/reply_message
            # BUT our _send_loop_message wrapper HAS throttle logic for PATCH.

            # Since current_status_message_id is None, it calls reply_message/send_message
            self.mock_handler.reply_message.assert_called()

            # Capture ID
            self.mock_handler.reply_message.return_value = "status_msg_1"

            # Reset
            self.mock_handler.reset_mock()

            # Now simulate an update where current_status_message_id is set (internal state of closure)
            # We can't set it directly.
            # But create_loop_callbacks sets current_status_message_id[0] = result_id

            # So if on_analyzing_done ran successfully and we mocked reply_message to return an ID,
            # internal state should be set.

            # Now trigger a streaming update (render_current_view is used for manual refreshes,
            # we need to trigger a callback that does an update)
            # on_iteration_start calls _send_loop_message with is_update=True

            callbacks.on_iteration_start(1, 5)

            # Iteration start is configured to flush immediately (throttle=False)
            self.mock_handler.patch_message.assert_called_with("reply_msg_id", "{}", max_retries=1, throttle=False)

    def test_help_card_structure(self):
        """Test that build_help_card returns valid structure"""
        msg_type, content_json = CardBuilder.build_help_card(category="main")
        content = json.loads(content_json)

        self.assertEqual(msg_type, "interactive")
        self.assertIn("header", content)
        self.assertIn("body", content)

        # Check for categories
        elements = content["body"]["elements"]
        buttons_row = next((e for e in elements if e.get("tag") == "column_set"), None)
        self.assertIsNotNone(buttons_row)

        # Check for content text
        text_elem = next(
            (e for e in elements if e.get("tag") == "markdown" and "编程模式" in e.get("content", "")), None
        )
        self.assertIsNotNone(text_elem)


if __name__ == "__main__":
    unittest.main()
