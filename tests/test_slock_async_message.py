"""Tests for SlockHandler.handle_message async card pattern.

Verifies:
1. Placeholder card is sent immediately via send_card_to_chat
2. Executor.submit is called for async execution
3. On success, update_card is called with the result card
4. On failure, update_card is called with an error card
"""

from __future__ import annotations

import sys
from concurrent.futures import Future
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Mock external dependencies that are not installed in the test environment.
# This must happen before any import of src.feishu.* modules.
# ---------------------------------------------------------------------------

_EXTERNAL_MODULES = [
    "lark_oapi", "lark_oapi.event", "lark_oapi.event.callback",
    "lark_oapi.event.callback.model", "lark_oapi.event.callback.model.p2_card_action_trigger",
    "lark_oapi.event.callback.model.p2_im_message_receive_v1",
    "lark_oapi.api", "lark_oapi.api.core", "lark_oapi.api.core.request",
    "lark_oapi.api.im", "lark_oapi.api.im.v1",
    "lark_oapi.ws", "lark_oapi.ws.const", "lark_oapi.ws.enum",
    "lark_oapi.ws.client",
    "acp", "acp.client", "acp.interfaces", "acp.schema", "acp.helpers",
    "acp.stdio",
]


class _FakeModule(MagicMock):
    """MagicMock subclass accepted by importlib machinery."""
    __spec__ = None
    __path__ = []
    __all__ = []


for _mod_name in _EXTERNAL_MODULES:
    sys.modules.setdefault(_mod_name, _FakeModule(name=_mod_name))


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_handler():
    """Build a SlockHandler with all dependencies mocked."""
    from src.feishu.handlers.slock import SlockHandler

    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.slock_engine_manager = MagicMock()

    handler = SlockHandler(ctx)
    handler.update_card = MagicMock(return_value=True)
    handler.send_text_to_chat = MagicMock()
    handler.send_card_to_chat = MagicMock(return_value="placeholder-msg-001")
    handler.reply_text = MagicMock()
    handler.reply_card = MagicMock()
    return handler


def _make_engine(execute_result="Agent result text", execute_side_effect=None):
    """Create a mock engine with executor that runs submitted tasks synchronously."""
    engine = MagicMock()
    engine.engine_name = "test-engine"
    engine.root_path = "/tmp/test"
    engine.channel = MagicMock()
    engine.channel.channel_id = "chat-001"
    engine.channel.team_name = "TestTeam"

    # Registry returns no agent for @mention matching
    engine.registry.find_by_name.return_value = None
    engine.registry.list_agents.return_value = []

    # Mock the executor to run submitted callables synchronously
    def _sync_submit(fn, *args, **kwargs):
        future = Future()
        try:
            result = fn(*args, **kwargs)
            future.set_result(result)
        except Exception as exc:
            future.set_exception(exc)
        return future

    executor = MagicMock()
    executor.submit.side_effect = _sync_submit
    engine._get_executor.return_value = executor

    # Mock execution behavior
    if execute_side_effect:
        engine._execute_agent.side_effect = execute_side_effect
    else:
        mock_agent = MagicMock()
        mock_agent.agent_id = "agent-default"
        mock_agent.name = "Default"
        mock_agent.emoji = "🤖"
        mock_agent.agent_type = "coco"
        mock_agent.model_name = ""
        engine.registry.list_agents.return_value = [mock_agent]
        engine.router.route_message.return_value = mock_agent
        engine._execute_agent.return_value = execute_result
    engine.execute.return_value = execute_result

    # Mock mouthpiece format_card
    engine._mouthpiece = MagicMock()
    def _format_card(_agent, content, **_kwargs):
        return {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "Result"}},
            "body": {"elements": [{"tag": "markdown", "content": content or ""}]},
        }

    engine._mouthpiece.format_card.side_effect = _format_card

    return engine


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlaceholderCardSentImmediately:
    """Verify that send_card_to_chat is called with a placeholder containing processing text."""

    def test_placeholder_card_sent_immediately(self):
        handler = _make_handler()
        engine = _make_engine(execute_result="done")
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "hello world")

        # send_card_to_chat should be called at least once for the placeholder
        handler.send_card_to_chat.assert_called()
        call_args = handler.send_card_to_chat.call_args
        card_json_str = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("card_json", "")
        # The placeholder card content should contain processing indicator
        assert "处理" in card_json_str or "思考" in card_json_str

    def test_placeholder_sent_to_correct_chat(self):
        handler = _make_handler()
        engine = _make_engine(execute_result="done")
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "hello world")

        first_call = handler.send_card_to_chat.call_args_list[0]
        assert first_call[0][0] == "chat-001"


class TestExecutorSubmitCalled:
    """Verify that the engine's executor.submit is invoked for async execution."""

    def test_executor_submit_called(self):
        handler = _make_handler()
        engine = _make_engine(execute_result="result")
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "do something")

        executor = engine._get_executor.return_value
        executor.submit.assert_called_once()

    def test_no_engine_means_no_submit(self):
        handler = _make_handler()
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = None

        handler.handle_message("msg-001", "chat-001", "hello")

        # No engine activated — should not attempt to send placeholder or submit
        handler.send_card_to_chat.assert_not_called()


class TestSuccessUpdatesCard:
    """After async execution completes successfully, update_card should be called."""

    def test_success_updates_card_with_result(self):
        handler = _make_handler()
        engine = _make_engine(execute_result="The answer is 42")
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "what is the answer?")

        # update_card should be called with the placeholder message_id
        handler.update_card.assert_called()
        update_call = handler.update_card.call_args
        assert update_call[0][0] == "placeholder-msg-001"

    def test_success_empty_result_updates_done_card(self):
        handler = _make_handler()
        engine = _make_engine(execute_result="")
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "do nothing")

        handler.update_card.assert_called()
        update_call = handler.update_card.call_args
        card_json_str = update_call[0][1]
        # Empty result should show a "done" indicator
        assert "完成" in card_json_str or "处理" in card_json_str

    def test_success_none_result_updates_done_card(self):
        handler = _make_handler()
        engine = _make_engine(execute_result=None)
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "silent task")

        handler.update_card.assert_called()
        update_call = handler.update_card.call_args
        assert update_call[0][0] == "placeholder-msg-001"


class TestFailureUpdatesCardWithError:
    """If execution raises an exception, update_card should show error content."""

    def test_failure_updates_card_with_error(self):
        handler = _make_handler()
        engine = _make_engine(execute_side_effect=RuntimeError("connection timeout"))
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "risky operation")

        handler.update_card.assert_called()
        update_call = handler.update_card.call_args
        card_json_str = update_call[0][1]
        # Error card should contain error indicator
        assert "出错" in card_json_str or "错误" in card_json_str or "Error" in card_json_str

    def test_failure_card_targets_placeholder_message(self):
        handler = _make_handler()
        engine = _make_engine(execute_side_effect=ValueError("bad input"))
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "bad request")

        update_call = handler.update_card.call_args
        assert update_call[0][0] == "placeholder-msg-001"

    def test_failure_with_no_card_message_id(self):
        """If send_card_to_chat returns None, update_card should not be called."""
        handler = _make_handler()
        handler.send_card_to_chat.return_value = None  # simulate send failure
        engine = _make_engine(execute_side_effect=RuntimeError("boom"))
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        handler.handle_message("msg-001", "chat-001", "will fail")

        # update_card should NOT be called when card_message_id is None
        handler.update_card.assert_not_called()


class TestCommandRedirect:
    """Messages starting with /task should redirect to handle_slock_command."""

    def test_task_command_redirects(self):
        handler = _make_handler()
        handler.handle_slock_command = MagicMock()

        handler.handle_message("msg-001", "chat-001", "/task list")

        handler.handle_slock_command.assert_called_once_with("msg-001", "chat-001", "/task list", None)
        # Should not send placeholder card for command redirects
        handler.send_card_to_chat.assert_not_called()


class TestAtMentionRouting:
    """Messages with @AgentName should try to route to a specific agent."""

    def test_at_mention_routes_to_agent(self):
        handler = _make_handler()
        engine = _make_engine(execute_result="agent response")
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        # Mock that the agent is found via @mention
        mock_agent = MagicMock()
        mock_agent.name = "Coder"
        mock_agent.agent_type = "codex"
        engine.registry.find_by_name.return_value = mock_agent

        handler.handle_message("msg-001", "chat-001", "@Coder fix the bug")

        # Should use _execute_agent for precise routing
        engine._execute_agent.assert_called_once()
        # First arg should be the agent
        assert engine._execute_agent.call_args[0][0] == mock_agent

    def test_at_mention_lookup_is_scoped_to_current_chat(self):
        """@mention pre-routing must not find same-name agents from other Slock teams."""
        handler = _make_handler()
        engine = _make_engine(execute_result="agent response")
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        engine.registry.find_by_name.return_value = None
        engine.registry.list_agents.return_value = []

        handler.handle_message("msg-001", "chat-001", "@Coder fix the bug")

        engine.registry.find_by_name.assert_called_once_with("Coder", channel_id="chat-001")

    def test_smart_routing_result_card_uses_selected_agent_identity(self):
        """Smart-routed replies must render the actual routed agent, not the first registry agent."""
        handler = _make_handler()
        engine = _make_engine(execute_result="ignored")
        handler.ctx.slock_engine_manager.get_activated_engine.return_value = engine

        first_agent = MagicMock()
        first_agent.agent_id = "agent-first"
        first_agent.name = "First"
        first_agent.emoji = "1"
        first_agent.agent_type = "codex"
        selected_agent = MagicMock()
        selected_agent.agent_id = "agent-selected"
        selected_agent.name = "Selected"
        selected_agent.emoji = "2"
        selected_agent.agent_type = "claude"
        engine.registry.find_by_name.return_value = None
        engine.registry.list_agents.return_value = [first_agent, selected_agent]
        engine.router.route_message.return_value = selected_agent
        engine._execute_agent.return_value = "[2 Selected] done"

        handler.handle_message("msg-001", "chat-001", "please review this")

        engine._execute_agent.assert_called_once()
        assert engine._execute_agent.call_args[0][0] == selected_agent
        engine._mouthpiece.format_card.assert_called_once()
        assert engine._mouthpiece.format_card.call_args.args[0] == selected_agent
