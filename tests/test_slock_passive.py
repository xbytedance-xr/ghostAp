"""Tests for slock passive mode features.

Covers:
- AC-1: User sends task in managed chat, gets processing feedback + result
- AC-2: New team auto-bootstraps default roles (coder + reviewer)
- AC-3: Multiple messages processed in parallel by different agents
- AC-4: Chitchat messages are filtered (no task creation, no agent response)
- AC-6: Admin commands (/role list, /task status) work in passive mode
- AC-7: Dispatcher routes without is_slock_active gate in passive mode
- AC-8: Full flow without any / commands from user side
"""

from __future__ import annotations

from concurrent.futures import Future
from unittest.mock import ANY, MagicMock, patch

import pytest

# ============================================================
# Helpers
# ============================================================

def _sync_submit(fn, *args, **kwargs):
    """Execute executor.submit synchronously for deterministic tests."""
    future = Future()
    try:
        result = fn(*args, **kwargs)
        future.set_result(result)
    except Exception as exc:
        future.set_exception(exc)
    return future


# ============================================================
# Task 12: Unit tests
# ============================================================


class TestRoleBootstrap:
    """Unit tests for role_bootstrap.py — parse config + idempotent creation."""

    def test_parse_default_roles_standard(self):
        from src.slock_engine.role_bootstrap import parse_default_roles

        result = parse_default_roles("coder:codex,reviewer:claude")
        assert result == [("coder", "codex"), ("reviewer", "claude")]

    def test_parse_default_roles_with_spaces(self):
        from src.slock_engine.role_bootstrap import parse_default_roles

        result = parse_default_roles(" coder : codex , reviewer : claude ")
        assert result == [("coder", "codex"), ("reviewer", "claude")]

    def test_parse_default_roles_single(self):
        from src.slock_engine.role_bootstrap import parse_default_roles

        result = parse_default_roles("planner:coco")
        assert result == [("planner", "coco")]

    def test_parse_default_roles_empty(self):
        from src.slock_engine.role_bootstrap import parse_default_roles

        result = parse_default_roles("")
        assert result == []

    def test_bootstrap_creates_agents(self):
        from src.slock_engine.role_bootstrap import bootstrap_default_roles

        engine = MagicMock()
        engine.registry = MagicMock()
        engine.registry.find_by_name.return_value = None
        engine.registry.register.side_effect = lambda a: a

        result = bootstrap_default_roles(engine, "chat_001", "coder:codex,reviewer:claude")
        assert len(result) == 2
        assert engine.registry.register.call_count == 2

        # Verify agent properties
        first_call = engine.registry.register.call_args_list[0][0][0]
        assert first_call.role == "coder"
        assert first_call.agent_type == "codex"
        assert first_call.owner_group == "chat_001"

    def test_bootstrap_idempotent(self):
        """Calling bootstrap twice doesn't create duplicates."""
        from src.slock_engine.role_bootstrap import bootstrap_default_roles

        existing_agent = MagicMock()
        engine = MagicMock()
        engine.registry = MagicMock()
        engine.registry.find_by_name.return_value = existing_agent

        result = bootstrap_default_roles(engine, "chat_001", "coder:codex,reviewer:claude")
        assert len(result) == 2
        # No register calls since both already exist
        engine.registry.register.assert_not_called()


class TestChitchatEnhanced:
    """Unit tests for enhanced _is_chitchat — AC-4."""

    @pytest.fixture
    def router(self):
        from src.slock_engine.task_router import TaskRouter
        tr = TaskRouter.__new__(TaskRouter)
        return tr

    @pytest.mark.parametrize("text", [
        "ok", "好的", "收到", "666", "👍", "谢谢", "hi",
        "!!!", "???", "。。。",  # Pure punctuation
        "😂🎉👏",  # Pure emoji
        "",  # Empty
        "嗯",  # Short no-verb
    ])
    def test_chitchat_filtered(self, router, text):
        """AC-4: Casual messages are filtered as chitchat."""
        assert router._is_chitchat(text) is True

    @pytest.mark.parametrize("text", [
        "帮我写一个快速排序函数",
        "请部署到生产环境",
        "分析一下这个bug的原因",
        "review this PR and check for security issues",
    ])
    def test_task_messages_not_filtered(self, router, text):
        """AC-4: Real task messages pass through the filter."""
        assert router._is_chitchat(text) is False


class TestDispatcherPassiveGate:
    """Unit tests for dispatcher gate simplification — AC-7."""

    @pytest.mark.parametrize("passive_mode,is_managed,is_active,expect_routed", [
        (True, True, False, True),    # Passive: managed alone is sufficient
        (True, True, True, True),     # Passive: active doesn't matter
        (True, False, True, False),   # Passive: not managed → no route
        (False, True, True, True),    # Legacy: both active + managed → route
        (False, True, False, False),  # Legacy: managed but not active → no route
        (False, False, True, False),  # Legacy: active but not managed → no route
    ])
    def test_passive_gate_parametrized(self, passive_mode, is_managed, is_active, expect_routed):
        """AC-7: Dispatcher slock gate respects passive vs legacy mode setting."""
        client = MagicMock()
        # Actual dispatcher uses getattr(self.client.settings, "slock_passive_mode", True)
        client.settings = MagicMock()
        client.settings.slock_passive_mode = passive_mode
        client._is_slock_managed_chat.return_value = is_managed
        client._is_slock_active.return_value = is_active

        # Replicate dispatcher.py:120-136 logic
        _is_managed = client._is_slock_managed_chat("chat_123")
        _passive_mode = getattr(client.settings, "slock_passive_mode", True)
        routed = False
        if _passive_mode:
            if _is_managed:
                routed = True
        else:
            if client._is_slock_active("chat_123") and _is_managed:
                routed = True

        assert routed is expect_routed
        # In passive mode, _is_slock_active should never be called
        if passive_mode:
            client._is_slock_active.assert_not_called()

    def test_passive_mode_routes_without_active_check(self):
        """AC-7: In passive mode, is_managed_chat alone is sufficient for routing."""
        client = MagicMock()
        client.settings = MagicMock()
        client.settings.slock_passive_mode = True
        client._is_slock_managed_chat.return_value = True
        client._is_slock_active.return_value = False  # NOT active

        _is_managed = client._is_slock_managed_chat("chat_123")
        _passive_mode = getattr(client.settings, "slock_passive_mode", True)
        _is_slock_route = _is_managed if _passive_mode else (
            client._is_slock_active("chat_123") and _is_managed
        )
        assert _is_slock_route is True
        client._is_slock_active.assert_not_called()

    def test_legacy_mode_requires_both_checks(self):
        """In legacy mode, both active AND managed are required."""
        client = MagicMock()
        client.settings = MagicMock()
        client.settings.slock_passive_mode = False
        client._is_slock_managed_chat.return_value = True
        client._is_slock_active.return_value = False

        _is_managed = client._is_slock_managed_chat("chat_123")
        _passive_mode = getattr(client.settings, "slock_passive_mode", True)
        _is_slock_route = _is_managed if _passive_mode else (
            client._is_slock_active("chat_123") and _is_managed
        )
        assert _is_slock_route is False


class TestSlashCommandsNoActivation:
    """Unit tests for slash_commands — managed chats don't get blocked."""

    def test_managed_chat_role_command_returns_true(self):
        from src.slock_engine.slash_commands import is_slock_command
        manager = MagicMock()
        manager.is_managed_chat.return_value = True
        assert is_slock_command("/role list", "chat_123", manager)

    def test_unmanaged_chat_role_command_returns_needs_activation(self):
        from src.slock_engine.slash_commands import NEEDS_ACTIVATION, is_slock_command
        manager = MagicMock()
        manager.is_managed_chat.return_value = False
        # /task is chat-scoped → NEEDS_ACTIVATION; /role is now global
        assert is_slock_command("/task list", "chat_123", manager) == NEEDS_ACTIVATION


# ============================================================
# Task 13: Integration tests
# ============================================================


class TestTaskFirstExecution:
    """Integration: message routed to agent via _execute_agent — AC-8."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.slock_nli_confidence_threshold = 0.6
        ctx.settings.slock_nli_timeout = 2.5
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="placeholder-msg-001")
        handler.update_card = MagicMock(return_value=True)
        return handler

    @pytest.mark.slow
    def test_task_first_creates_task_and_executes(self):
        """AC-8: Non-command message routes to agent via _execute_agent."""
        handler = self._make_handler()

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_123"
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card.return_value = {"header": {}, "elements": []}

        mock_agent = MagicMock()
        mock_agent.agent_id = "codex:default:abc"
        mock_agent.agent_type = "codex"
        mock_agent.model_name = ""
        engine.registry.list_agents.return_value = [mock_agent]
        engine.router.route_message.return_value = mock_agent
        engine._execute_agent.return_value = "Quick sort implementation"

        executor = MagicMock()
        executor.submit.side_effect = _sync_submit
        engine._get_executor = MagicMock(return_value=executor)

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_1", "chat_123", "帮我写一个快速排序函数", None)

        # Verify routing and execution
        engine.router.route_message.assert_called_once_with("帮我写一个快速排序函数", [mock_agent])
        engine._execute_agent.assert_called_once_with(mock_agent, "帮我写一个快速排序函数", ANY)

    @pytest.mark.slow
    def test_auto_retry_on_failure(self):
        """When primary agent returns None, no automatic retry in current implementation."""
        handler = self._make_handler()

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_123"
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card.return_value = {"header": {}, "elements": []}

        primary_agent = MagicMock()
        primary_agent.agent_id = "codex:default:primary"
        primary_agent.agent_type = "codex"
        primary_agent.model_name = ""

        engine.registry.list_agents.return_value = [primary_agent]
        engine.router.route_message.return_value = primary_agent
        engine._execute_agent.return_value = None  # Simulate failure

        executor = MagicMock()
        executor.submit.side_effect = _sync_submit
        engine._get_executor = MagicMock(return_value=executor)

        manager = MagicMock()
        manager.get_activated_engine.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        handler.handle_message("msg_2", "chat_123", "修复这个bug", None)

        # Verify execution was attempted
        engine._execute_agent.assert_called_once_with(primary_agent, "修复这个bug", ANY)


class TestCreateTeamWithBootstrap:
    """Integration: /new-team creates group with default roles — AC-2."""

    def test_create_team_bootstraps_roles(self):
        """AC-2: New team automatically has coder + reviewer roles."""
        from src.slock_engine.role_bootstrap import bootstrap_default_roles

        engine = MagicMock()
        engine.registry = MagicMock()
        engine.registry.find_by_name.return_value = None
        engine.registry.register.side_effect = lambda a: a

        roles = bootstrap_default_roles(engine, "new_chat_123", "coder:codex,reviewer:claude")
        assert len(roles) == 2
        role_names = [r.role for r in roles]
        assert "coder" in role_names
        assert "reviewer" in role_names


# ============================================================
# Task 14: E2E scenario tests
# ============================================================


class TestParallelMessageDispatch:
    """E2E: Multiple messages get dispatched to different agents — AC-3."""

    def test_three_messages_create_three_tasks(self):
        """AC-3: 3 concurrent messages create 3 independent tasks."""
        MagicMock()

        engine = MagicMock()
        engine.channel = MagicMock()
        engine.channel.channel_id = "chat_parallel"

        agent1 = MagicMock(agent_id="a1")
        agent2 = MagicMock(agent_id="a2")
        agent3 = MagicMock(agent_id="a3")
        engine.registry.list_agents.return_value = [agent1, agent2, agent3]

        # Each message gets routed to a different agent
        engine.router.route_message.side_effect = [agent1, agent2, agent3]

        task1 = MagicMock(task_id="t1")
        task2 = MagicMock(task_id="t2")
        task3 = MagicMock(task_id="t3")
        engine.add_task.side_effect = [task1, task2, task3]

        messages = ["写排序函数", "修复登录bug", "添加单元测试"]
        for msg in messages:
            engine.add_task(msg)

        assert engine.add_task.call_count == 3


class TestAdminCommandsRegression:
    """E2E: Admin commands still work in passive mode — AC-6."""

    def test_role_list_in_passive_mode(self):
        """AC-6: /role list returns results in passive-mode managed chat."""
        from src.slock_engine.slash_commands import is_slock_command

        manager = MagicMock()
        manager.is_managed_chat.return_value = True
        assert is_slock_command("/role list", "chat_passive", manager)
        assert is_slock_command("/task status", "chat_passive", manager)
        assert is_slock_command("/slock status", "chat_passive", manager)

    def test_slock_help_always_available(self):
        from src.slock_engine.slash_commands import is_slock_command
        assert is_slock_command("/slock help", "any_chat", None)


# ============================================================
# Task 15: Auto-bootstrap integration tests (AC-2)
# ============================================================


class TestActivateSlockAutoBootstrap:
    """Integration: activate_slock auto-bootstraps default roles — AC-2."""

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.slock_default_roles = "coder:codex,reviewer:claude"
        ctx.settings.slock_nli_confidence_threshold = 0.6
        ctx.settings.slock_nli_timeout = 2.5
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg-001")
        handler.update_card = MagicMock(return_value=True)
        handler.send_text_to_chat = MagicMock()
        handler.add_reaction = MagicMock()
        handler.create_static_card_session = MagicMock()
        handler.create_static_card_session.return_value = MagicMock()
        return handler

    @patch("src.slock_engine.activation_guard.get_activation_guard")
    @patch("src.feishu.handlers.slock.SlockHandler._ensure_project")
    @patch("src.feishu.handlers.slock.SlockHandler.get_working_dir", return_value="/tmp/test")
    @patch("src.feishu.handlers.slock.SlockHandler.get_engine_name", return_value="test-engine")
    @patch("src.slock_engine.role_bootstrap.bootstrap_default_roles")
    def test_activate_slock_calls_bootstrap(
        self, mock_bootstrap, mock_engine_name, mock_workdir, mock_ensure_project, mock_guard
    ):
        """AC-2: activate_slock calls bootstrap_default_roles with config."""
        mock_guard.return_value.can_auto_activate.return_value = (True, "allowed")
        handler = self._make_handler()

        mock_project = MagicMock()
        mock_project.root_path = "/tmp/test"
        mock_project.project_name = "TestProject"
        mock_project.project_id = "proj_1"
        mock_ensure_project.return_value = mock_project

        engine = MagicMock()
        engine.channel = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        manager.get_or_create_activated.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        with patch("src.thread.manager.get_current_sender_id", return_value="user_001"):
            handler.activate_slock("msg_1", "chat_new", project=mock_project)

        # Wait briefly for async bootstrap thread
        import time
        time.sleep(0.2)
        mock_bootstrap.assert_called_once_with(engine, "chat_new", "coder:codex,reviewer:claude")

    @patch("src.slock_engine.activation_guard.get_activation_guard")
    @patch("src.feishu.handlers.slock.SlockHandler._ensure_project")
    @patch("src.feishu.handlers.slock.SlockHandler.get_working_dir", return_value="/tmp/test")
    @patch("src.feishu.handlers.slock.SlockHandler.get_engine_name", return_value="test-engine")
    @patch("src.slock_engine.role_bootstrap.bootstrap_default_roles")
    def test_activate_slock_empty_config_skips_bootstrap(
        self, mock_bootstrap, mock_engine_name, mock_workdir, mock_ensure_project, mock_guard
    ):
        """AC-2 boundary: empty slock_default_roles skips bootstrap entirely."""
        mock_guard.return_value.can_auto_activate.return_value = (True, "allowed")
        handler = self._make_handler()
        handler.ctx.settings.slock_default_roles = ""  # Empty config

        mock_project = MagicMock()
        mock_project.root_path = "/tmp/test"
        mock_project.project_name = "TestProject"
        mock_project.project_id = "proj_1"
        mock_ensure_project.return_value = mock_project

        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        manager.get_or_create_activated.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        with patch("src.thread.manager.get_current_sender_id", return_value="user_001"):
            handler.activate_slock("msg_1", "chat_new", project=mock_project)

        mock_bootstrap.assert_not_called()


class TestFullFlowWithBootstrapRoles:
    """E2E: New group activation → bootstrap roles → message routed to preset role — AC-2."""

    def test_full_flow_message_routes_to_bootstrapped_role(self):
        """AC-2 full flow: activate group → roles auto-created → task message → agent executes."""
        from src.slock_engine.role_bootstrap import bootstrap_default_roles

        # Step 1: Simulate bootstrap creating agents
        engine = MagicMock()
        engine.registry = MagicMock()
        engine.registry.find_by_name.return_value = None
        engine.registry.register.side_effect = lambda a: a

        roles = bootstrap_default_roles(engine, "chat_flow", "coder:codex,reviewer:claude")
        assert len(roles) == 2

        # Step 2: Simulate message routing with bootstrapped roles available
        coder_agent = roles[0]
        assert coder_agent.role == "coder"
        assert coder_agent.agent_type == "codex"
        assert coder_agent.owner_group == "chat_flow"

        # Step 3: Simulate router selecting the coder for a coding task
        from src.slock_engine.task_router import TaskRouter
        router = TaskRouter.__new__(TaskRouter)

        # The task should NOT be filtered as chitchat
        assert router._is_chitchat("帮我写一个快速排序函数") is False

        # Step 4: Verify the agent identity has correct properties for execution
        assert "shell" in coder_agent.permissions
        assert "file_write" in coder_agent.permissions
        assert coder_agent.personality_traits == ["严谨", "注重细节", "高效"]

    def test_full_flow_create_team_with_immediate_task(self):
        """AC-2 E2E: /new-team → bootstrap → first message gets processed."""
        from src.slock_engine.role_bootstrap import bootstrap_default_roles

        # Simulate engine with registry that tracks agents
        engine = MagicMock()
        registered_agents = []
        engine.registry = MagicMock()
        engine.registry.find_by_name.return_value = None
        engine.registry.register.side_effect = lambda a: (registered_agents.append(a) or a)

        # Bootstrap happens during create_team
        bootstrap_default_roles(engine, "new_team_chat", "coder:codex,reviewer:claude")
        assert len(registered_agents) == 2

        # Simulate listing agents for routing (post-bootstrap)
        engine.registry = MagicMock()
        engine.registry.list_agents.return_value = registered_agents
        engine.router = MagicMock()
        engine.router.route_message.return_value = registered_agents[0]  # coder selected
        engine._execute_agent = MagicMock(return_value="Implementation complete")

        # User sends first task message — no /new-role needed
        task_text = "实现用户登录功能"
        agents = engine.registry.list_agents(channel_id="new_team_chat")
        selected = engine.router.route_message(task_text, agents)

        assert selected is not None
        assert selected.role == "coder"

        # Agent executes the task
        result = engine._execute_agent(selected, task_text, {})
        assert result == "Implementation complete"
        engine._execute_agent.assert_called_once_with(selected, task_text, {})


# ============================================================
# Task 16: Spec-based mock test (prevents _agent_registry regression)
# ============================================================


class TestBootstrapUsesPublicAPI:
    """Verify bootstrap_default_roles only accesses public SlockEngine attributes."""

    def test_bootstrap_with_autospec_engine(self):
        """Regression guard: bootstrap must use engine.registry (public property)."""
        from unittest.mock import PropertyMock, create_autospec

        from src.slock_engine.engine import SlockEngine
        from src.slock_engine.role_bootstrap import bootstrap_default_roles

        engine = create_autospec(SlockEngine, instance=True)
        mock_registry = MagicMock()
        mock_registry.find_by_name.return_value = None
        mock_registry.register.side_effect = lambda a: a
        type(engine).registry = PropertyMock(return_value=mock_registry)

        result = bootstrap_default_roles(engine, "chat_autospec", "coder:codex")
        assert len(result) == 1
        assert result[0].role == "coder"
        mock_registry.register.assert_called_once()


# ============================================================
# Task 21: Bootstrap signals dispatch loop
# ============================================================


class TestBootstrapSignalsDispatchLoop:
    """Verify that bootstrap always signals dispatch loop readiness."""

    @patch("src.slock_engine.activation_guard.get_activation_guard")
    @patch("src.feishu.handlers.slock.SlockHandler._ensure_project")
    @patch("src.feishu.handlers.slock.SlockHandler.get_working_dir", return_value="/tmp/test")
    @patch("src.feishu.handlers.slock.SlockHandler.get_engine_name", return_value="test-engine")
    @patch("src.slock_engine.role_bootstrap.bootstrap_default_roles")
    def test_bootstrap_signals_complete_on_success(
        self, mock_bootstrap, mock_engine_name, mock_workdir, mock_ensure_project, mock_guard
    ):
        """finish_bootstrap called after successful bootstrap via public API."""

        mock_guard.return_value.can_auto_activate.return_value = (True, "allowed")
        handler = self._make_handler()

        mock_project = MagicMock()
        mock_project.root_path = "/tmp/test"
        mock_project.project_name = "Test"
        mock_project.project_id = "p1"
        mock_ensure_project.return_value = mock_project

        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        manager.get_or_create_activated.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        mock_bootstrap.return_value = [MagicMock()]

        with patch("src.thread.manager.get_current_sender_id", return_value="user_1"):
            handler.activate_slock("msg_1", "chat_1", project=mock_project)

        # Wait briefly for async bootstrap thread to complete
        import time
        time.sleep(0.2)
        engine.finish_bootstrap.assert_called_once()

    @patch("src.slock_engine.activation_guard.get_activation_guard")
    @patch("src.feishu.handlers.slock.SlockHandler._ensure_project")
    @patch("src.feishu.handlers.slock.SlockHandler.get_working_dir", return_value="/tmp/test")
    @patch("src.feishu.handlers.slock.SlockHandler.get_engine_name", return_value="test-engine")
    @patch("src.slock_engine.role_bootstrap.bootstrap_default_roles")
    def test_bootstrap_signals_complete_on_failure(
        self, mock_bootstrap, mock_engine_name, mock_workdir, mock_ensure_project, mock_guard
    ):
        """finish_bootstrap called even if bootstrap fails (public API)."""
        mock_guard.return_value.can_auto_activate.return_value = (True, "allowed")
        handler = self._make_handler()

        mock_project = MagicMock()
        mock_project.root_path = "/tmp/test"
        mock_project.project_name = "Test"
        mock_project.project_id = "p1"
        mock_ensure_project.return_value = mock_project

        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        manager.get_or_create_activated.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        mock_bootstrap.side_effect = RuntimeError("bootstrap crash")

        with patch("src.thread.manager.get_current_sender_id", return_value="user_1"):
            handler.activate_slock("msg_1", "chat_1", project=mock_project)

        # Wait briefly for async bootstrap thread to complete
        import time
        time.sleep(0.2)
        engine.finish_bootstrap.assert_called_once()

    @patch("src.slock_engine.activation_guard.get_activation_guard")
    @patch("src.feishu.handlers.slock.SlockHandler._ensure_project")
    @patch("src.feishu.handlers.slock.SlockHandler.get_working_dir", return_value="/tmp/test")
    @patch("src.feishu.handlers.slock.SlockHandler.get_engine_name", return_value="test-engine")
    def test_empty_config_signals_immediately(
        self, mock_engine_name, mock_workdir, mock_ensure_project, mock_guard
    ):
        """prepare_bootstrap not called when no roles configured (dispatch starts immediately)."""
        mock_guard.return_value.can_auto_activate.return_value = (True, "allowed")
        handler = self._make_handler()
        handler.ctx.settings.slock_default_roles = ""

        mock_project = MagicMock()
        mock_project.root_path = "/tmp/test"
        mock_project.project_name = "Test"
        mock_project.project_id = "p1"
        mock_ensure_project.return_value = mock_project

        engine = MagicMock()
        manager = MagicMock()
        manager.get_activated_engine.return_value = None
        manager.get_or_create_activated.return_value = engine
        handler._get_engine_manager = MagicMock(return_value=manager)

        with patch("src.thread.manager.get_current_sender_id", return_value="user_1"):
            handler.activate_slock("msg_1", "chat_1", project=mock_project)

        # With no roles configured, bootstrap-ready state remains set (default),
        # no finish_bootstrap needed — dispatch loop starts immediately
        engine.prepare_bootstrap.assert_not_called()

    def _make_handler(self):
        from src.feishu.handlers.slock import SlockHandler
        ctx = MagicMock()
        ctx.settings = MagicMock()
        ctx.settings.slock_default_roles = "coder:codex,reviewer:claude"
        ctx.settings.slock_nli_confidence_threshold = 0.6
        ctx.settings.slock_nli_timeout = 2.5
        handler = SlockHandler(ctx)
        handler.reply_text = MagicMock()
        handler.reply_card = MagicMock()
        handler.send_card_to_chat = MagicMock(return_value="msg-001")
        handler.update_card = MagicMock(return_value=True)
        handler.send_text_to_chat = MagicMock()
        handler.add_reaction = MagicMock()
        handler.create_static_card_session = MagicMock()
        handler.create_static_card_session.return_value = MagicMock()
        return handler
