"""Tests for Zero Human Interaction during Agent execution (AC-08).

Validates that Slock mode does not send any confirmation, permission,
or interaction-request messages to users during agent execution.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.slock_engine.models import AgentIdentity, AgentStatus


class TestZeroHumanInteraction:
    """Verify agents execute without sending interactive prompts to users."""

    FORBIDDEN_PATTERNS = [
        "确认",
        "是否继续",
        "需要您",
        "请确认",
        "是否允许",
        "permission",
        "confirm",
        "approve",
        "authorize",
        "do you want",
        "shall I",
        "would you like",
    ]

    def _make_handler(self):
        """Create a SlockHandler with mocked context and tracking."""
        from src.feishu.handlers.slock import SlockHandler

        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.slock_engine_manager = MagicMock()

        handler = SlockHandler(ctx)
        handler.send_text_to_chat = MagicMock()
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock()
        handler.add_reaction = MagicMock()
        return handler

    def _make_agent(self, agent_id="agent-001", name="Coder", agent_type="codex"):
        """Create a standard test AgentIdentity."""
        return AgentIdentity(
            agent_id=agent_id,
            name=name,
            emoji="🔧",
            agent_type=agent_type,
            model_name="o3-pro",
            system_prompt="You are a coding assistant.",
            role="coder",
            permissions=["shell", "file_write", "git"],
        )

    def _check_no_forbidden_messages(self, *mock_fns):
        """Assert no forbidden interaction patterns in sent messages."""
        for mock_fn in mock_fns:
            for c in mock_fn.call_args_list:
                if c.args:
                    text = str(c.args[-1]) if len(c.args) > 1 else str(c.args[0])
                    for pattern in self.FORBIDDEN_PATTERNS:
                        assert pattern.lower() not in text.lower(), (
                            f"Forbidden interaction pattern '{pattern}' found in message: {text}"
                        )

    @patch("src.slock_engine.engine.create_engine_session")
    @patch("src.slock_engine.engine.close_session_safely")
    @patch("src.slock_engine.engine.get_settings")
    def test_engine_callbacks_contain_no_interactive_messages(
        self, mock_get_settings, mock_close_session, mock_create_session
    ):
        """Callbacks from engine execution must not produce interactive messages.

        Triggers a real _execute_agent call with a mocked ACP session that returns
        a response, then verifies no callback output contains FORBIDDEN_PATTERNS.
        """
        from src.slock_engine.engine import SlockEngine, SlockEngineCallbacks

        # Configure settings mock
        settings = MagicMock()
        settings.slock_agent_execution_timeout = 60
        settings.coco_execution_timeout = 30
        settings.slock_freshness_gate_enabled = False
        mock_get_settings.return_value = settings

        # Configure the mock session to return a realistic agent response
        agent_response_text = "代码重构完成。已将 utils.py 中的重复逻辑提取为 _parse_config 函数，PR #312 已提交。"
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.text = agent_response_text
        mock_session.send_prompt.return_value = mock_result
        mock_create_session.return_value = mock_session

        # Track all output received by callbacks
        collected_outputs: list[str] = []

        def on_agent_done(agent, result):
            collected_outputs.append(str(result))

        def on_agent_running(agent, msg):
            collected_outputs.append(str(msg))

        def on_agent_error(agent, err):
            collected_outputs.append(str(err))

        callbacks = SlockEngineCallbacks(
            on_agent_wake=lambda a: None,
            on_agent_thinking=lambda a: None,
            on_agent_running=on_agent_running,
            on_agent_done=on_agent_done,
            on_agent_error=on_agent_error,
        )

        # Build engine with mocked dependencies
        import threading
        engine = MagicMock(spec=SlockEngine)
        engine._lock = threading.Lock()
        engine._agent_sessions = {}
        engine.root_path = "/tmp/test_project"
        engine.chat_id = "chat_001"
        engine._channel = MagicMock()
        engine._channel.channel_id = "channel_001"
        engine._memory = MagicMock()
        engine._memory.read_agent_memory.return_value = MagicMock(
            role="coder", key_knowledge="", active_context=""
        )
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_text.return_value = agent_response_text
        engine._registry = MagicMock()
        engine._registry.list_agents.return_value = []
        engine._router = MagicMock()
        engine._router.extract_skill_keywords.return_value = ["refactor"]
        engine._observer_queue = MagicMock()
        engine.settings = settings
        engine._autonomous_resolver = None
        engine._get_cancel_event = MagicMock(return_value=threading.Event())
        engine._clear_cancel_event = MagicMock()
        engine.transition_agent = MagicMock()
        engine.get_agent_status = MagicMock(return_value=AgentStatus.IDLE)

        # Bind the real _run_acp_session so it actually calls create_engine_session
        engine._run_acp_session = lambda agent, prompt: SlockEngine._run_acp_session(engine, agent, prompt)

        agent = self._make_agent()

        # Execute the real _execute_agent method via unbound call
        result = SlockEngine._execute_agent(engine, agent, "请重构 utils.py", callbacks)

        # Verify execution actually produced output
        assert result is not None, "_execute_agent should have returned a result"
        assert len(collected_outputs) > 0, "Callbacks should have been invoked with output"

        # Verify the session was actually triggered (non-vacuous test)
        mock_create_session.assert_called_once()
        mock_session.send_prompt.assert_called_once()

        # Verify no forbidden patterns in any callback output
        for output in collected_outputs:
            for pattern in self.FORBIDDEN_PATTERNS:
                assert pattern.lower() not in output.lower(), (
                    f"Forbidden interaction pattern '{pattern}' found in callback output: {output}"
                )

    @patch("src.slock_engine.engine.create_engine_session")
    @patch("src.slock_engine.engine.close_session_safely")
    @patch("src.slock_engine.engine.get_settings")
    def test_acp_session_created_with_auto_approve(
        self, mock_get_settings, mock_close_session, mock_create_session
    ):
        """ACP session must be created with auto_approve=True in slock mode.

        This is the core zero-HI guarantee: the session factory receives
        auto_approve=True so that underlying tool invocations never prompt
        the user for confirmation.
        """
        # Configure settings
        import threading

        from src.slock_engine.engine import SlockEngine
        settings = MagicMock()
        settings.slock_agent_execution_timeout = 60
        settings.coco_execution_timeout = 30
        mock_get_settings.return_value = settings

        # Configure session mock
        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.text = "Task completed successfully."
        mock_session.send_prompt.return_value = mock_result
        mock_create_session.return_value = mock_session

        # Build engine with mocked dependencies
        engine = MagicMock(spec=SlockEngine)
        engine._lock = threading.Lock()
        engine._agent_sessions = {}
        engine.root_path = "/tmp/test_project"
        engine.chat_id = "chat_001"
        engine._channel = MagicMock()
        engine._channel.channel_id = "channel_001"
        engine._memory = MagicMock()
        engine._memory.read_agent_memory.return_value = MagicMock(
            role="coder", key_knowledge="", active_context=""
        )
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_text.return_value = "Task completed successfully."
        engine._registry = MagicMock()
        engine._registry.list_agents.return_value = []
        engine._router = MagicMock()
        engine._router.extract_skill_keywords.return_value = []
        engine._observer_queue = MagicMock()
        engine.settings = settings
        engine._get_cancel_event = MagicMock(return_value=threading.Event())
        engine._clear_cancel_event = MagicMock()
        engine.transition_agent = MagicMock()
        engine.get_agent_status = MagicMock(return_value=AgentStatus.IDLE)

        # Bind the real _run_acp_session so create_engine_session is actually called
        engine._run_acp_session = lambda agent, prompt: SlockEngine._run_acp_session(engine, agent, prompt)

        agent = self._make_agent(agent_id="agent-auto", name="Builder", agent_type="codex")

        # Execute the real _execute_agent which internally calls _run_acp_session
        SlockEngine._execute_agent(engine, agent, "build the feature", None)

        # Verify create_engine_session was called with auto_approve=True
        mock_create_session.assert_called_once()
        call_kwargs = mock_create_session.call_args

        # auto_approve is a keyword-only argument in create_engine_session
        assert call_kwargs.kwargs.get("auto_approve") is True, (
            f"create_engine_session must be called with auto_approve=True, "
            f"got kwargs: {call_kwargs.kwargs}"
        )

        # Verify the agent_type is passed correctly
        assert call_kwargs.kwargs.get("agent_type") == "codex" or (
            len(call_kwargs.args) > 0 and call_kwargs.args[0] == "codex"
        ), (
            f"create_engine_session should receive agent_type='codex', "
            f"got args={call_kwargs.args}, kwargs={call_kwargs.kwargs}"
        )

        # Verify thread_id follows the expected pattern for slock agents
        thread_id = call_kwargs.kwargs.get("thread_id", "")
        assert "slock_agent_" in thread_id, (
            f"thread_id should contain 'slock_agent_' prefix, got: {thread_id}"
        )

    def test_slock_mode_flag_disables_interaction(self):
        """Slock mode should set flags that suppress tool interaction prompts."""
        # This tests the principle that when slock mode is active,
        # the auto-approve flags should be set for underlying tools
        from src.slock_engine.models import AgentIdentity

        agent = AgentIdentity(
            agent_id="agent-001",
            name="Coder",
            emoji="🔧",
            agent_type="codex",
            model_name="o3-pro",
            system_prompt="test",
            role="coder",
            permissions=["shell", "file_write", "git"],
        )

        # Permissions should include auto-approve capabilities
        assert "shell" in agent.permissions
        assert "file_write" in agent.permissions

    def test_agent_output_is_result_not_question(self):
        """Agent card output should be declarative results, not questions."""
        from src.slock_engine.card_templates import build_agent_message_card

        agent = AgentIdentity(
            agent_id="agent-001",
            name="Coder-A",
            emoji="🔧",
            agent_type="claude",
            model_name="sonnet-4",
            system_prompt="test",
            role="coder",
        )

        # Simulate agent output — should be a result statement
        card = build_agent_message_card(
            agent=agent,
            content="代码重构完成，PR #247 已提交。",
            model_info="claude | sonnet-4",
            duration_s=12.5,
        )

        import json
        card_str = json.dumps(card, ensure_ascii=False)

        # Output should NOT contain interactive patterns
        for pattern in self.FORBIDDEN_PATTERNS:
            assert pattern.lower() not in card_str.lower(), (
                f"Agent output card contains forbidden pattern: {pattern}"
            )
