"""Tests verifying deprecation logger.warning fires once and only once."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


class TestBaseHandlerDeprecation:
    """reply_message/patch_message emit logger.warning once per method."""

    def setup_method(self):
        """Reset the module-level _DEPRECATION_WARNED set before each test."""
        import src.feishu.handlers.base as base_mod
        base_mod._DEPRECATION_WARNED.clear()

    def _make_handler(self):
        """Create a minimal BaseHandler instance with mocked dependencies."""
        from src.feishu.handlers.base import BaseHandler

        handler = BaseHandler.__new__(BaseHandler)
        handler.im_client = MagicMock()
        handler.im_client.patch_message.return_value = MagicMock(success=lambda: True)

        # Mock ctx for reply_message
        handler.ctx = MagicMock()
        handler.ctx.message_linker.resolve_origin.return_value = "origin_msg"
        handler.reply_message_with_id = MagicMock(return_value=None)

        # Throttled patches
        handler._pending_patches = {}
        handler._patch_tasks = {}

        return handler

    def test_reply_message_warns_once(self, caplog):
        """First call to reply_message logs warning; second call does not."""
        handler = self._make_handler()

        with caplog.at_level(logging.WARNING, logger="src.feishu.handlers.base"):
            handler.reply_message("msg_1", "content1")
            first_count = sum(1 for r in caplog.records if "已废弃" in r.message)

            handler.reply_message("msg_2", "content2")
            second_count = sum(1 for r in caplog.records if "已废弃" in r.message)

        assert first_count == 1, "First call should emit one warning"
        assert second_count == 1, "Second call should NOT emit additional warning"

    def test_patch_message_warns_once(self, caplog):
        """First call to patch_message logs warning; second call does not."""
        handler = self._make_handler()

        with caplog.at_level(logging.WARNING, logger="src.feishu.handlers.base"):
            handler.patch_message("msg_1", "{}")
            first_count = sum(1 for r in caplog.records if "已废弃" in r.message)

            handler.patch_message("msg_2", "{}")
            second_count = sum(1 for r in caplog.records if "已废弃" in r.message)

        assert first_count == 1
        assert second_count == 1

    def test_deprecation_message_contains_migration_guidance(self, caplog):
        """Warning message should contain migration guidance in Chinese."""
        handler = self._make_handler()

        with caplog.at_level(logging.WARNING, logger="src.feishu.handlers.base"):
            handler.reply_message("msg_1", "content")

        warning_msgs = [r.message for r in caplog.records if "已废弃" in r.message]
        assert len(warning_msgs) == 1
        assert "CardSession" in warning_msgs[0] or "CardDelivery" in warning_msgs[0]
        assert "B-016" not in warning_msgs[0], "Should not contain internal ticket reference"


class TestEngineCardSenderDeprecation:
    """EngineCardSender emits logger.warning once on first instantiation."""

    def setup_method(self):
        """Reset the module-level _DEPRECATION_WARNED set before each test."""
        import src.card.delivery.engine_sender as sender_mod
        sender_mod._DEPRECATION_WARNED.clear()

    def test_engine_card_sender_warns_once(self, caplog):
        """First instantiation logs warning; second does not."""
        from src.card.delivery.engine_sender import EngineCardSender

        mock_client = MagicMock()
        mock_client.create_card.return_value = ("msg_1", "card_1")

        with caplog.at_level(logging.WARNING, logger="src.card.delivery.engine_sender"):
            EngineCardSender(mock_client, "chat_1", "reply_msg_1")
            first_count = sum(1 for r in caplog.records if "已废弃" in r.message)

            EngineCardSender(mock_client, "chat_2", "reply_msg_2")
            second_count = sum(1 for r in caplog.records if "已废弃" in r.message)

        assert first_count == 1, "First instantiation should emit one warning"
        assert second_count == 1, "Second instantiation should NOT emit additional warning"
