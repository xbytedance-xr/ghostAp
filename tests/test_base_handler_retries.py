from unittest.mock import MagicMock

import pytest

from src.feishu.handler_context import HandlerContext
from src.feishu.handlers.base import BaseHandler


class TestBaseHandlerRetries:
    @pytest.fixture
    def mock_context(self):
        ctx = MagicMock(spec=HandlerContext)
        ctx.settings = MagicMock()
        ctx.settings.im_api_max_retries = 3
        ctx.api_client_factory = MagicMock()
        ctx.message_linker = MagicMock()
        return ctx

    @pytest.fixture
    def handler(self, mock_context):
        return BaseHandler(mock_context)

    def test_send_message_passes_max_retries(self, handler):
        # Mock im_client.send_message
        handler.im_client.send_message = MagicMock()
        handler.im_client.send_message.return_value = MagicMock(
            success=lambda: True, data=MagicMock(message_id="msg_123")
        )

        # Case 1: Default (None)
        handler.send_message("chat_123", "hello")
        args, kwargs = handler.im_client.send_message.call_args
        assert kwargs.get("max_retries") is None

        # Case 2: Explicit value
        handler.send_message("chat_123", "hello", max_retries=5)
        args, kwargs = handler.im_client.send_message.call_args
        assert kwargs.get("max_retries") == 5

    def test_reply_message_passes_max_retries(self, handler):
        # Mock im_client.reply_message
        handler.im_client.reply_message = MagicMock()
        handler.im_client.reply_message.return_value = MagicMock(
            success=lambda: True, data=MagicMock(message_id="msg_456")
        )

        # Case 1: Default (None)
        handler.reply_message("msg_123", "hello")
        args, kwargs = handler.im_client.reply_message.call_args
        assert kwargs.get("max_retries") is None

        # Case 2: Explicit value
        handler.reply_message("msg_123", "hello", max_retries=2)
        args, kwargs = handler.im_client.reply_message.call_args
        assert kwargs.get("max_retries") == 2

    def test_reply_message_with_id_passes_max_retries(self, handler):
        # Mock im_client.reply_message
        handler.im_client.reply_message = MagicMock()
        handler.im_client.reply_message.return_value = MagicMock(
            success=lambda: True, data=MagicMock(message_id="msg_789")
        )

        # Case 1: Default (None)
        handler.reply_message_with_id("msg_123", "hello")
        args, kwargs = handler.im_client.reply_message.call_args
        assert kwargs.get("max_retries") is None

        # Case 2: Explicit value
        handler.reply_message_with_id("msg_123", "hello", max_retries=1)
        args, kwargs = handler.im_client.reply_message.call_args
        assert kwargs.get("max_retries") == 1

    def test_execute_with_retry_uses_max_retries(self, handler):
        # This logic is now in im_client, so we test im_client._execute_with_retry
        # Mock the function being retried to always fail
        mock_func = MagicMock(side_effect=Exception("API Error"))

        # Test that it retries the specified number of times
        handler.im_client._execute_with_retry(mock_func, "test_action", max_retries=2)
        assert mock_func.call_count == 2

        mock_func.reset_mock()

        # Test that it falls back to settings default if None passed
        handler.im_client._execute_with_retry(mock_func, "test_action", max_retries=None)
        assert mock_func.call_count == 3  # Based on fixture setup: im_api_max_retries=3
