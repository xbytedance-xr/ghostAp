"""Test BaseRenderer.create_session() failure path — AC-17.

When session_factory.create() raises an exception, the user should receive
a degraded text reply and the exception should be re-raised.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestCreateSessionFailure:
    """BaseRenderer.create_session() graceful degradation on factory failure."""

    def _make_renderer(self):
        from src.feishu.renderers.base import BaseRenderer

        handler = MagicMock()
        handler.reply_text = MagicMock()
        handler.get_card_delivery = MagicMock()
        renderer = BaseRenderer(handler)
        return renderer, handler

    def test_factory_error_triggers_text_reply(self):
        """session_factory.create() failure → handler.reply_text called with fallback."""
        renderer, handler = self._make_renderer()

        with patch.object(renderer, "_get_session_factory") as mock_factory:
            mock_factory.return_value.create.side_effect = RuntimeError("pool exhausted")

            with pytest.raises(RuntimeError, match="pool exhausted"):
                renderer.create_session(
                    chat_id="test_chat",
                    message_id="test_msg",
                )

        handler.reply_text.assert_called_once()
        call_text = handler.reply_text.call_args[0][0]
        assert "使用人数较多" in call_text

    def test_factory_error_reraises(self):
        """The exception is re-raised after sending fallback text."""
        renderer, handler = self._make_renderer()

        with patch.object(renderer, "_get_session_factory") as mock_factory:
            mock_factory.return_value.create.side_effect = ValueError("bad config")

            with pytest.raises(ValueError, match="bad config"):
                renderer.create_session(
                    chat_id="c1",
                    message_id="m1",
                )
