"""Tests for NLI integration in SlockHandler.handle_message.

Covers AC14: 5 core NLI routing paths.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.intent_router import IntentResult
from src.slock_engine.slash_commands import SlockCommandAction


@pytest.fixture
def mock_handler():
    """Create a SlockHandler instance with mocked dependencies."""
    with patch("src.feishu.handlers.slock.BaseEngineHandler.__init__", return_value=None):
        from src.feishu.handlers.slock import SlockHandler

        handler = SlockHandler.__new__(SlockHandler)
        handler._rate_limit_tracker = {}

        # Mock context and settings
        handler.ctx = MagicMock()
        handler.ctx.settings.slock_nli_timeout = 2.5
        handler.ctx.settings.slock_nli_confidence_threshold = 0.6

        # Mock IntentRouter
        handler._intent_router = MagicMock()

        # Mock NLI executor
        handler._nli_executor = MagicMock()

        # Mock base handler methods
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.handle_slock_command = MagicMock()
        handler._execute_routed_message = MagicMock()
        handler._dispatch_nli_intent = MagicMock()
        handler.activate_slock = MagicMock()
        handler._send_no_engine_hint = MagicMock()

        # Mock engine manager
        handler._get_engine_manager = MagicMock()

        return handler


class TestNLIHighConfidence:
    """AC14-1: High confidence intent (>=0.85) → direct execution."""

    def test_high_confidence_dispatches_directly(self, mock_handler):
        """When NLI returns action with confidence >= 0.85, dispatch directly."""
        intent = IntentResult(
            action=SlockCommandAction.STATUS,
            confidence=0.90,
            params={},
        )
        mock_handler._intent_router.fast_classify.return_value = intent

        # Mock engine as active
        engine = MagicMock()
        engine.registry.find_by_name.return_value = None
        mock_handler._get_engine_manager().get_activated_engine.return_value = engine

        mock_handler.handle_message("msg1", "chat1", "查看状态", None)

        mock_handler._dispatch_nli_intent.assert_called_once()
        mock_handler._execute_routed_message.assert_not_called()


class TestNLIMediumConfidence:
    """AC14-2: Medium confidence (threshold <= conf < 0.85) → confirmation card."""

    def test_medium_confidence_shows_confirmation_card(self, mock_handler):
        """When NLI returns medium confidence, show confirmation card with 🤔."""
        intent = IntentResult(
            action=SlockCommandAction.NEW_ROLE,
            confidence=0.72,
            params={"name": "coder"},
        )
        mock_handler._intent_router.fast_classify.return_value = intent

        # Mock engine as active
        engine = MagicMock()
        engine.registry.find_by_name.return_value = None
        mock_handler._get_engine_manager().get_activated_engine.return_value = engine

        # Need _NLI_ACTION_DESCRIPTIONS
        mock_handler._NLI_ACTION_DESCRIPTIONS = {"new_role": "创建新角色"}

        mock_handler.handle_message("msg1", "chat1", "建一个coder角色", None)

        mock_handler.reply_card.assert_called_once()
        card_json = mock_handler.reply_card.call_args[0][1]
        card = json.loads(card_json)
        assert "🤔" in card["header"]["title"]["content"]
        mock_handler._dispatch_nli_intent.assert_not_called()


class TestNLIUnknownFallback:
    """AC14-3: UNKNOWN action → fallback to smart routing."""

    def test_unknown_intent_falls_through_to_smart_routing(self, mock_handler):
        """When NLI returns UNKNOWN, fall through to smart routing."""
        intent = IntentResult(
            action=SlockCommandAction.UNKNOWN,
            confidence=0.0,
            params={},
        )
        mock_handler._intent_router.fast_classify.return_value = intent

        # Mock engine as active
        engine = MagicMock()
        engine.registry.find_by_name.return_value = None
        mock_handler._get_engine_manager().get_activated_engine.return_value = engine

        mock_handler.handle_message("msg1", "chat1", "今天天气不错", None)

        mock_handler._execute_routed_message.assert_called_once()
        mock_handler._dispatch_nli_intent.assert_not_called()


class TestNLITimeoutDegradation:
    """AC14-4: NLI timeout → graceful degradation to smart routing."""

    def test_nli_timeout_falls_through(self, mock_handler):
        """When NLI times out, fall through to smart routing."""
        # fast_classify returns None (no fast match)
        mock_handler._intent_router.fast_classify.return_value = None

        # Mock the NLI loop call to raise TimeoutError
        with patch("src.feishu.handlers.slock._get_nli_loop") as mock_loop:
            import concurrent.futures

            mock_future = MagicMock()
            mock_future.result.side_effect = concurrent.futures.TimeoutError()

            with patch("asyncio.run_coroutine_threadsafe", return_value=mock_future):
                # Mock engine as active
                engine = MagicMock()
                engine.registry.find_by_name.return_value = None
                mock_handler._get_engine_manager().get_activated_engine.return_value = engine

                mock_handler.handle_message("msg1", "chat1", "做个复杂的事情", None)

        mock_handler._execute_routed_message.assert_called_once()
        mock_handler._dispatch_nli_intent.assert_not_called()


class TestNLIActivateIntent:
    """AC14-5: ACTIVATE intent + no engine → trigger activation."""

    def test_activate_intent_without_engine_triggers_activation(self, mock_handler):
        """When ACTIVATE intent detected and no engine active, trigger activation."""
        intent = IntentResult(
            action=SlockCommandAction.ACTIVATE,
            confidence=0.92,
            params={},
        )
        mock_handler._intent_router.fast_classify.return_value = intent

        # No engine active
        mock_handler._get_engine_manager().get_activated_engine.return_value = None

        mock_handler.handle_message("msg1", "chat1", "启动slock", None)

        mock_handler.activate_slock.assert_called_once_with("msg1", "chat1", "启动slock", None)
        mock_handler._dispatch_nli_intent.assert_not_called()
