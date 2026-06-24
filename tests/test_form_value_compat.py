"""Tests for form_value compatibility in slock handler.

Verifies:
- slock_plan_supplement reads from _form_value nested dict (Feishu WS protocol)
- slock_plan_supplement falls back to top-level value (legacy card protocol)
- Empty content returns error toast
- Content >2000 chars returns error toast
- Sensitive credential patterns are rejected
"""

from __future__ import annotations

from unittest.mock import MagicMock

from src.feishu.handlers.slock import SlockHandler

CHAT_ID = "oc_form_test_001"


def _make_handler() -> MagicMock:
    """Create a mock handler with real handle_card_action bound."""
    handler = MagicMock(spec=SlockHandler)
    handler.handle_card_action = lambda open_message_id, open_chat_id, action_type, value: (
        SlockHandler.handle_card_action(handler, open_message_id, open_chat_id, action_type, value)
    )
    return handler


def _make_engine_with_plan(plan_id: str = "plan-001") -> MagicMock:
    """Create a mock engine with a plan that has one assigned step."""
    engine = MagicMock()
    plan = MagicMock()
    plan.steps = [MagicMock(agent_id="agent-coder")]
    engine.collaboration_orchestrator.get_plan.return_value = plan
    engine.memory = MagicMock()
    return engine


def _setup_handler_with_engine(handler: MagicMock, engine: MagicMock):
    """Wire up the handler to return the mock engine."""
    manager = MagicMock()
    manager.get_activated_engine.return_value = engine
    handler._get_engine_manager.return_value = manager
    handler._require_slock_permission.return_value = None
    handler.send_text_to_chat = MagicMock()


class TestFormValueNestedRead:
    """slock_plan_supplement reads supplement_content from _form_value first."""

    def test_reads_from_form_value_nested_dict(self):
        handler = _make_handler()
        engine = _make_engine_with_plan()
        _setup_handler_with_engine(handler, engine)

        value = {
            "plan_id": "plan-001",
            "_form_value": {"supplement_content": "从嵌套读取的补充信息"},
        }

        handler.handle_card_action("om_msg", CHAT_ID, "slock_plan_supplement", value)

        # Should have injected context (not returned error toast)
        engine.memory.update_agent_context.assert_called()
        call_args = engine.memory.update_agent_context.call_args[0]
        assert "从嵌套读取的补充信息" in call_args[1]

    def test_falls_back_to_top_level(self):
        handler = _make_handler()
        engine = _make_engine_with_plan()
        _setup_handler_with_engine(handler, engine)

        value = {
            "plan_id": "plan-001",
            "supplement_content": "顶层补充内容",
            # No _form_value key
        }

        handler.handle_card_action("om_msg", CHAT_ID, "slock_plan_supplement", value)

        engine.memory.update_agent_context.assert_called()
        call_args = engine.memory.update_agent_context.call_args[0]
        assert "顶层补充内容" in call_args[1]

    def test_nested_takes_priority_over_top_level(self):
        handler = _make_handler()
        engine = _make_engine_with_plan()
        _setup_handler_with_engine(handler, engine)

        value = {
            "plan_id": "plan-001",
            "_form_value": {"supplement_content": "嵌套优先"},
            "supplement_content": "顶层被忽略",
        }

        handler.handle_card_action("om_msg", CHAT_ID, "slock_plan_supplement", value)

        engine.memory.update_agent_context.assert_called()
        call_args = engine.memory.update_agent_context.call_args[0]
        assert "嵌套优先" in call_args[1]
        assert "顶层被忽略" not in call_args[1]


class TestFormValueValidation:
    """Content validation rules for slock_plan_supplement."""

    def test_empty_content_returns_error_toast(self):
        handler = _make_handler()
        engine = _make_engine_with_plan()
        _setup_handler_with_engine(handler, engine)

        value = {
            "plan_id": "plan-001",
            "_form_value": {"supplement_content": "   "},  # whitespace only
        }

        result = handler.handle_card_action("om_msg", CHAT_ID, "slock_plan_supplement", value)

        assert result is not None
        assert result["toast"]["type"] == "error"
        assert "请输入" in result["toast"]["content"]

    def test_content_exceeds_2000_returns_error(self):
        handler = _make_handler()
        engine = _make_engine_with_plan()
        _setup_handler_with_engine(handler, engine)

        value = {
            "plan_id": "plan-001",
            "_form_value": {"supplement_content": "A" * 2001},
        }

        result = handler.handle_card_action("om_msg", CHAT_ID, "slock_plan_supplement", value)

        assert result is not None
        assert result["toast"]["type"] == "error"
        assert "2000" in result["toast"]["content"]

    def test_sensitive_credential_rejected(self):
        handler = _make_handler()
        engine = _make_engine_with_plan()
        _setup_handler_with_engine(handler, engine)

        value = {
            "plan_id": "plan-001",
            "_form_value": {"supplement_content": "my token=sk-abcdefghijklmn1234"},
        }

        result = handler.handle_card_action("om_msg", CHAT_ID, "slock_plan_supplement", value)

        assert result is not None
        assert result["toast"]["type"] == "error"
        assert "敏感凭据" in result["toast"]["content"]

    def test_content_at_limit_accepted(self):
        handler = _make_handler()
        engine = _make_engine_with_plan()
        _setup_handler_with_engine(handler, engine)

        value = {
            "plan_id": "plan-001",
            "_form_value": {"supplement_content": "B" * 2000},  # exactly at limit
        }

        handler.handle_card_action("om_msg", CHAT_ID, "slock_plan_supplement", value)

        # Should succeed (no error toast returned; context injected)
        engine.memory.update_agent_context.assert_called()
