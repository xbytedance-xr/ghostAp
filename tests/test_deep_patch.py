import json
from unittest.mock import MagicMock, patch

import pytest

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
        # Mock the new API methods used by DirectCardSession mock
        handler.reply_card = MagicMock(return_value="mock_reply_id")
        handler.update_card = MagicMock(return_value=True)
        return handler

    def test_send_deep_message_sends_card_via_session(self, handler):
        """Verify that create_deep_callbacks sends cards through DirectCardSession (reply_card path)."""
        callbacks = handler._create_deep_callbacks(
            message_id="msg_123", chat_id="chat_123", project=None, initial_message_id="init_msg_id"
        )

        # Mock CardBuilder to return a valid V2 card structure
        v2_card = {"schema": "2.0", "header": {"title": "Test"}, "body": {"elements": []}}
        v2_card_json = json.dumps(v2_card)

        # Mock project for the callback
        mock_project = MagicMock()
        mock_project.name = "Test Project"
        mock_project.root_path = "/tmp"
        mock_project.project_id = "proj_123"

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            # Configure CardBuilder to return our V2 card
            mock_builder.build_engine_card.return_value = ("interactive", v2_card_json)

            # Trigger the callback which calls _send_deep_message
            callbacks.on_analyzing_done(mock_project)

            # Verify the card was sent via the session (which delegates to reply_card)
            assert handler.reply_card.called
            args, _ = handler.reply_card.call_args
            sent_content = args[1]

            # The content should contain our v2 card structure
            sent_json = json.loads(sent_content)
            assert "schema" in sent_json
            assert sent_json["schema"] == "2.0"

    def test_send_deep_message_update_path(self, handler):
        """Verify that subsequent sends use update_card path (session has message_id)."""
        handler.reply_card.return_value = "created_msg_id"
        handler.update_card.return_value = True

        callbacks = handler._create_deep_callbacks(
            message_id="msg_123", chat_id="chat_123", project=None, initial_message_id=None
        )

        mock_project = MagicMock()
        mock_project.name = "Test Project"
        mock_project.root_path = "/tmp"
        mock_project.project_id = "proj_123"

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            # First send → reply_card (create path)
            callbacks.on_analyzing_done(mock_project)
            assert handler.reply_card.called

            handler.reply_card.reset_mock()

            # Second send → update_card (session already has message_id)
            handler.ctx.progress_reporter.format_error.return_value = "Error"
            handler.ctx.progress_reporter.get_error_title.return_value = "Error Title"
            callbacks.on_error("Some error")

            # Should use update path since session now has a message_id
            assert handler.update_card.called
            handler.reply_card.assert_not_called()


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

    def test_show_deep_status_patch_success(self, handler):
        # Mock engine and project
        mock_engine = MagicMock()
        mock_project = MagicMock()
        mock_project.status = DeepProjectStatus.IDLE
        mock_project.project_id = "p1"
        mock_engine.project = mock_project
        mock_engine.engine_name = "Coco"
        mock_engine.progress.completed_steps = 0
        mock_engine.progress.total_steps = 0

        handler.ctx.deep_engine_manager.get.return_value = mock_engine

        # Mock settings
        handler.settings.card_deep_compact_default = False

        # Mock update_card to succeed
        handler.update_card = MagicMock(return_value=True)
        handler.reply_card = MagicMock()

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            handler.show_deep_status("msg1", "chat1", project=mock_project, origin_message_id="origin1")

            # Verify update_card called (patch path)
            handler.update_card.assert_called_once()

            # Verify build_engine_card called with compact=False
            _, kwargs = mock_builder.build_engine_card.call_args
            state = kwargs.get("state")
            assert state is not None
            assert state.compact is False

            # Verify reply_card NOT called (patch succeeded)
            handler.reply_card.assert_not_called()

    def test_show_deep_status_patch_failure_fallback(self, handler):
        # Mock engine and project
        mock_engine = MagicMock()
        mock_project = MagicMock()
        mock_project.status = DeepProjectStatus.IDLE
        mock_engine.project = mock_project
        mock_engine.engine_name = "Coco"
        mock_engine.progress.completed_steps = 0
        mock_engine.progress.total_steps = 0

        handler.ctx.deep_engine_manager.get.return_value = mock_engine

        # Mock API client failure - update_card returns False on failure
        handler.update_card = MagicMock(return_value=False)
        handler.reply_card = MagicMock()

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            handler.show_deep_status("msg1", "chat1", project=mock_project, origin_message_id="origin1")

            # Verify update_card called (patch attempt)
            handler.update_card.assert_called_once()
            # Verify fallback to reply_card
            handler.reply_card.assert_called_once()

    def test_show_deep_status_no_origin_id(self, handler):
        # Mock engine and project
        mock_engine = MagicMock()
        mock_project = MagicMock()
        mock_project.status = DeepProjectStatus.IDLE
        mock_engine.project = mock_project
        mock_engine.engine_name = "Coco"
        mock_engine.progress.completed_steps = 0
        mock_engine.progress.total_steps = 0

        handler.ctx.deep_engine_manager.get.return_value = mock_engine

        # Mock new API methods
        handler.update_card = MagicMock(return_value=False)
        handler.reply_card = MagicMock()

        with patch("src.feishu.renderers.deep_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            handler.show_deep_status("msg1", "chat1", project=mock_project, origin_message_id=None)

            # No origin_message_id → update_card should NOT be called
            handler.update_card.assert_not_called()
            # Fallback directly to reply_card
            handler.reply_card.assert_called_once()
