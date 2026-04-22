"""Verify BaseEngine accepts injectable settings parameter."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.engine_base import BaseEngine


class TestBaseEngineDI:

    def test_injected_settings_used(self):
        mock_settings = MagicMock(name="injected_settings")
        engine = BaseEngine(
            chat_id="c1",
            root_path="/tmp/test",
            settings=mock_settings,
        )
        assert engine.settings is mock_settings

    def test_default_falls_back_to_get_settings(self):
        sentinel = MagicMock(name="global_settings")
        with patch("src.engine_base.get_settings", return_value=sentinel):
            engine = BaseEngine(chat_id="c1", root_path="/tmp/test")
        assert engine.settings is sentinel

    def test_subclass_inherits_injection(self):
        class DummyEngine(BaseEngine):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)

        mock_settings = MagicMock(name="sub_settings")
        engine = DummyEngine(
            chat_id="c1",
            root_path="/tmp/test",
            settings=mock_settings,
        )
        assert engine.settings is mock_settings
