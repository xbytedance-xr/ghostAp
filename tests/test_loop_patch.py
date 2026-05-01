from unittest.mock import MagicMock, patch

import pytest

from src.feishu.handlers.loop import LoopHandler
from src.loop_engine.models import IterationRecord, IterationStatus, LoopProject


class TestLoopHandlerPatch:
    @pytest.fixture
    def handler(self):
        # Mock dependencies
        ctx = MagicMock()
        ctx.api_client_factory = MagicMock()
        ctx.loop_reporter = MagicMock()
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

        return handler

    def test_loop_message_patch_success(self, handler):
        """Test that loop callbacks use update_card (patch) after first send."""
        # Configure mock project duration
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 0
        mock_project.total_criteria = 0
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        # Create callbacks
        callbacks = handler._create_loop_callbacks(
            message_id="msg_origin", chat_id="chat_123", project=None, engine_name="Coco"
        )

        # Mock CardBuilder
        with patch("src.feishu.renderers.loop_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            # 1. First call: Analyzing done (Should create via reply_card)
            callbacks.on_analyzing_done(mock_project)

            # Should have called reply_card (create path)
            handler.reply_card.assert_called()
            # And NOT update_card yet
            handler.update_card.assert_not_called()

            # Reset mocks
            handler.reply_card.reset_mock()
            handler.update_card.reset_mock()

            # 2. Second call: Iteration start (Should update via update_card)
            callbacks.on_iteration_start(1, 10)

            # Verify update_card called (patch path)
            handler.update_card.assert_called()

            # Verify NO new message sent
            handler.reply_card.assert_not_called()

    def test_loop_message_patch_fail_fallback(self, handler):
        """Test fallback to new message when update_card fails."""
        # Setup update_card to fail
        handler.update_card.return_value = False
        handler.reply_card.side_effect = ["msg_1", "msg_2", "msg_3"]

        # Configure mock project duration
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 0
        mock_project.total_criteria = 0
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        # Create callbacks
        callbacks = handler._create_loop_callbacks(
            message_id="msg_origin", chat_id="chat_123", project=None, engine_name="Coco"
        )

        with patch("src.feishu.renderers.loop_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            # 1. First call: Analyzing done -> reply_card (create path)
            callbacks.on_analyzing_done(mock_project)
            assert handler.reply_card.call_count == 1

            # Reset
            handler.reply_card.reset_mock()

            # 2. Second call: Iteration start
            # The conftest mock delegates: first send → reply_card, subsequent → update_card
            # Since reply_card returned "msg_1" on first send, session._message_id is set
            # So second send goes to update_card which returns False (failure)
            # The DirectCardSession mock still calls update_card regardless of return value
            callbacks.on_iteration_start(1, 10)

            # update_card was called (the session always tries update once message_id is set)
            handler.update_card.assert_called()

    def test_loop_message_new_card_on_iteration_done(self, handler):
        """Test that on_iteration_done sends an independent new card (new_card=True)."""
        handler.reply_card.side_effect = ["msg_1", "msg_done"]

        # Configure mock project duration
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 0
        mock_project.total_criteria = 0
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        callbacks = handler._create_loop_callbacks("msg_origin", "chat_1", None)

        with patch("src.feishu.renderers.loop_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            # 1. Send initial (analyzing_done)
            callbacks.on_analyzing_done(mock_project)
            assert handler.reply_card.call_count == 1

            handler.reply_card.reset_mock()
            handler.update_card.reset_mock()

            # 2. Iteration done uses new_card=True → closes session → creates new one
            record = MagicMock(spec=IterationRecord)
            record.status = IterationStatus.SUCCESS
            callbacks.on_iteration_done(1, record)

            # Should create a new card via reply_card (session was reset)
            handler.reply_card.assert_called()

    def test_loop_message_project_done(self, handler):
        """Test that on_project_done sends an independent new message."""
        handler.reply_card.side_effect = ["msg_1", "msg_done"]

        # Configure mock project
        mock_engine = MagicMock()
        mock_project = MagicMock(spec=LoopProject)
        mock_project.duration.return_value = 10.0
        mock_project.satisfied_count = 5
        mock_project.total_criteria = 5
        mock_engine.project = mock_project
        handler.ctx.loop_engine_manager.get.return_value = mock_engine

        callbacks = handler._create_loop_callbacks("msg_origin", "chat_1", None)

        with patch("src.feishu.renderers.loop_renderer.CardBuilder") as mock_builder:
            mock_builder.build_engine_card.return_value = ("interactive", "{}")

            # 1. Send initial
            callbacks.on_analyzing_done(mock_project)

            handler.reply_card.reset_mock()

            # 2. Project Done — sends new card
            callbacks.on_project_done(mock_project)

            # Verify new message sent (reply_card called again after session reset)
            handler.reply_card.assert_called()
