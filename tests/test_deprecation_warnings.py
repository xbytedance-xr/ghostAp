"""Tests verifying deprecation logger.warning fires once and only once."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


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
