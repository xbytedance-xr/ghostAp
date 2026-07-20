"""Handler-level tests for Slock assign_task and create_role changes.

Covers:
- Task 17: assign_task sends error card when add_task returns None
- Task 18: create_role rejects regular (non-admin/non-owner) users
- Task 22: assign_task async sends placeholder card then updates on completion
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.slock_engine.models import AgentIdentity, SlockTask, TaskStatus

# ============================================================
# Helpers
# ============================================================

PATCH_GET_SENDER = "src.thread.manager.get_current_sender_id"
PATCH_GET_SETTINGS = "src.config.get_settings"


def _make_handler():
    """Create a SlockHandler with fully mocked context."""
    from src.feishu.handlers.slock import SlockHandler

    ctx = MagicMock()
    ctx.settings = MagicMock()
    ctx.settings.admin_user_ids = frozenset(["admin_001"])
    ctx.slock_engine_manager = MagicMock()
    ctx.api_client_factory = MagicMock()
    handler = SlockHandler(ctx)
    handler.reply_text = MagicMock(return_value=True)
    handler.reply_card = MagicMock()
    handler.send_card_to_chat = MagicMock(return_value="card-msg-001")
    handler.update_card = MagicMock(return_value=True)
    handler.send_text_to_chat = MagicMock()
    handler.add_reaction = MagicMock()
    handler.get_working_dir = MagicMock(return_value="/tmp/test")
    return handler


def _make_engine_mock(owner_id="owner_001"):
    """Create a mock engine with channel and registry."""
    engine = MagicMock()
    engine.is_active = True
    engine.channel = MagicMock()
    engine.channel.channel_id = "chat-001"
    engine.channel.team_name = "TestTeam"
    engine.channel.owner_id = owner_id
    engine.engine_name = "Slock"
    engine.root_path = "/tmp/test"
    return engine


# ============================================================
# Task 17: assign_task add_task returns None → error card
# ============================================================


class TestAssignTaskAddTaskReturnsNone:
    """When engine.add_task returns None, handler sends error feedback."""

    @patch(PATCH_GET_SENDER, return_value="admin_001")
    @patch(PATCH_GET_SETTINGS)
    def test_add_task_none_sends_error(self, mock_settings, mock_sender):
        """If add_task returns None (limit reached), handler replies with error text."""
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()
        engine.add_task = MagicMock(return_value=None)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.assign_task("msg-001", "chat-001", "Write tests", "Coder-A", None)

        # Should have called reply_text with error about limit
        handler.reply_text.assert_called()
        call_args = handler.reply_text.call_args[0]
        assert "任务创建失败" in call_args[1]

    @patch(PATCH_GET_SENDER, return_value="admin_001")
    @patch(PATCH_GET_SETTINGS)
    def test_add_task_none_no_role_sends_error(self, mock_settings, mock_sender):
        """If add_task returns None without role_name specified, still sends error."""
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()
        engine.add_task = MagicMock(return_value=None)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.assign_task("msg-002", "chat-001", "Auto route task", "", None)

        handler.reply_text.assert_called()
        call_args = handler.reply_text.call_args[0]
        assert "任务创建失败" in call_args[1]


# ============================================================
# Task 18: create_role rejects regular users
# ============================================================


class TestCreateRoleRegularUserBlocked:
    """Non-admin, non-owner users are rejected by create_role permission gate."""

    @patch(PATCH_GET_SENDER, return_value="regular_user_999")
    @patch(PATCH_GET_SETTINGS)
    def test_regular_user_blocked(self, mock_settings, mock_sender):
        """A regular user (not admin, not owner) gets permission denied."""
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock(owner_id="owner_001")
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.create_role("msg-001", "chat-001", "Coder --tool codex", None)

        # Permission denied message should have been sent
        handler.reply_text.assert_called()
        call_args = handler.reply_text.call_args[0]
        assert "权限不足" in call_args[1]

    @patch(PATCH_GET_SENDER, return_value="admin_001")
    @patch(PATCH_GET_SETTINGS)
    def test_admin_passes(self, mock_settings, mock_sender):
        """Admin user is allowed to create roles (no permission error)."""
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock(owner_id="owner_001")
        engine.registry.find_by_name = MagicMock(return_value=None)
        # Mock memory methods to prevent file I/O
        engine.memory.agent_memory_path = MagicMock(return_value="/tmp/agent_mem")
        engine.memory.write_agent_memory = MagicMock()
        engine.memory.read_agent_template = MagicMock(return_value=None)
        engine.memory.write_skill_profiles = MagicMock()
        engine.registry.register = MagicMock()
        engine.registry.get = MagicMock(return_value=None)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.create_role("msg-002", "chat-001", "Coder --tool codex", None)

        # Should NOT have permission denied
        for call in handler.reply_text.call_args_list:
            msg = call[0][1] if len(call[0]) > 1 else ""
            assert "权限不足" not in msg

    @patch(PATCH_GET_SENDER, return_value="owner_001")
    @patch(PATCH_GET_SETTINGS)
    def test_channel_owner_passes(self, mock_settings, mock_sender):
        """Channel owner is allowed to create roles."""
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock(owner_id="owner_001")
        engine.registry.find_by_name = MagicMock(return_value=None)
        engine.memory.agent_memory_path = MagicMock(return_value="/tmp/agent_mem")
        engine.memory.write_agent_memory = MagicMock()
        engine.memory.read_agent_template = MagicMock(return_value=None)
        engine.memory.write_skill_profiles = MagicMock()
        engine.registry.register = MagicMock()
        engine.registry.get = MagicMock(return_value=None)
        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.create_role("msg-003", "chat-001", "Writer --tool coco", None)

        # Should NOT have permission denied
        for call in handler.reply_text.call_args_list:
            msg = call[0][1] if len(call[0]) > 1 else ""
            assert "权限不足" not in msg


# ============================================================
# Task 22: assign_task async sends placeholder then updates
# ============================================================


class TestAssignTaskAsyncPlaceholderThenUpdate:
    """Async assign_task sends placeholder card, then updates with result."""

    @patch(PATCH_GET_SENDER, return_value="admin_001")
    @patch(PATCH_GET_SETTINGS)
    def test_placeholder_sent_and_updated_on_success(self, mock_settings, mock_sender):
        """Handler sends a placeholder card and updates it with result after execution."""
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        settings.slock_queue_wait_timeout = 60
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()

        agent = AgentIdentity(
            agent_id="agent-async-001",
            name="AsyncCoder",
            emoji="🔧",
            agent_type="coco",
            owner_group="chat-001",
        )
        task = SlockTask(
            task_id="task-async-001",
            content="Implement feature",
            status=TaskStatus.TODO,
            created_in="chat-001",
        )

        engine.add_task = MagicMock(return_value=task)
        engine.registry.find_by_name = MagicMock(return_value=agent)
        engine.claim_task = MagicMock(return_value=True)
        engine.execute_task = MagicMock(return_value="Feature implemented!")
        engine._mouthpiece = MagicMock()
        engine._mouthpiece.format_card = MagicMock(return_value={
            "schema": "2.0",
            "header": {"title": {"content": "Result"}},
            "body": {"elements": []},
        })

        # Use a synchronous executor mock that runs immediately
        mock_executor = MagicMock()

        def immediate_submit(fn, *args, **kwargs):
            fn()  # execute synchronously for testing
            future = MagicMock()
            return future

        mock_executor.submit = immediate_submit
        engine._get_executor = MagicMock(return_value=mock_executor)

        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.assign_task("msg-001", "chat-001", "Implement feature", "AsyncCoder", None)

        # 1. Placeholder card was sent via send_card_to_chat
        handler.send_card_to_chat.assert_called()
        first_card_call = handler.send_card_to_chat.call_args_list[0]
        card_json = first_card_call[0][1]
        card_data = json.loads(card_json)
        assert "⏳" in card_data["header"]["title"]["content"]

        # 2. Card was updated with result
        handler.update_card.assert_called()
        update_call = handler.update_card.call_args_list[0]
        assert update_call[0][0] == "card-msg-001"

    @patch(PATCH_GET_SENDER, return_value="admin_001")
    @patch(PATCH_GET_SETTINGS)
    def test_placeholder_updated_with_error_on_failure(self, mock_settings, mock_sender):
        """On execution failure, placeholder card is updated with error."""
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        settings.slock_queue_wait_timeout = 60
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()

        agent = AgentIdentity(
            agent_id="agent-fail-001",
            name="FailCoder",
            emoji="💥",
            agent_type="coco",
            owner_group="chat-001",
        )
        task = SlockTask(
            task_id="task-fail-001",
            content="Break things",
            status=TaskStatus.TODO,
            created_in="chat-001",
        )

        engine.add_task = MagicMock(return_value=task)
        engine.registry.find_by_name = MagicMock(return_value=agent)
        engine.claim_task = MagicMock(return_value=True)
        engine.execute_task = MagicMock(side_effect=RuntimeError("ACP session crashed"))
        engine._mouthpiece = MagicMock()

        # Synchronous executor mock
        mock_executor = MagicMock()

        def immediate_submit(fn, *args, **kwargs):
            fn()
            return MagicMock()

        mock_executor.submit = immediate_submit
        engine._get_executor = MagicMock(return_value=mock_executor)

        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.assign_task("msg-002", "chat-001", "Break things", "FailCoder", None)

        # Card should be updated with error content
        handler.update_card.assert_called()
        update_call = handler.update_card.call_args_list[0]
        card_json = update_call[0][1]
        card_data = json.loads(card_json)
        assert "❌" in card_data["header"]["title"]["content"]

    @patch(PATCH_GET_SENDER, return_value="admin_001")
    @patch(PATCH_GET_SETTINGS)
    def test_queue_full_updates_placeholder_with_busy(self, mock_settings, mock_sender):
        """When executor rejects submission, placeholder is updated with busy message."""
        from src.slock_engine.bounded_executor import QueueFullError

        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()

        agent = AgentIdentity(
            agent_id="agent-busy-001",
            name="BusyCoder",
            emoji="⚡",
            agent_type="coco",
            owner_group="chat-001",
        )
        task = SlockTask(
            task_id="task-busy-001",
            content="Queue full task",
            status=TaskStatus.TODO,
            created_in="chat-001",
        )

        engine.add_task = MagicMock(return_value=task)
        engine.registry.find_by_name = MagicMock(return_value=agent)
        engine.claim_task = MagicMock(return_value=True)

        # Executor that rejects submission
        mock_executor = MagicMock()
        mock_executor.submit = MagicMock(side_effect=QueueFullError("queue full"))
        engine._get_executor = MagicMock(return_value=mock_executor)

        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)

        handler.assign_task("msg-003", "chat-001", "Queue full task", "BusyCoder", None)

        # Placeholder should be updated with busy message
        handler.update_card.assert_called()
        update_call = handler.update_card.call_args_list[0]
        card_json = update_call[0][1]
        card_data = json.loads(card_json)
        assert "繁忙" in card_data["header"]["title"]["content"]


# ============================================================
# TestExecuteAsyncHelper: directly test _execute_async
# ============================================================


class TestExecuteAsyncHelper:
    """Directly test the _execute_async helper method."""

    def _call_execute_async(
        self,
        handler,
        engine,
        *,
        execute_fn,
        result_card_fn=None,
        error_card_fn=None,
        empty_card_fn=None,
        busy_card_fn=None,
    ):
        """Helper to invoke _execute_async with sensible defaults."""
        if result_card_fn is None:
            def result_card_fn(result, duration):
                return json.dumps(
                    {"schema": "2.0", "header": {"title": {"content": "✅ Done"}}, "body": {"elements": []}},
                    ensure_ascii=False,
                )

        if error_card_fn is None:
            def error_card_fn(exc):
                return json.dumps(
                    {"schema": "2.0", "header": {"title": {"content": "❌ Error"}}, "body": {"elements": []}},
                    ensure_ascii=False,
                )

        if empty_card_fn is None:
            def empty_card_fn():
                return json.dumps(
                    {"schema": "2.0", "header": {"title": {"content": "⚠️ Empty"}}, "body": {"elements": []}},
                    ensure_ascii=False,
                )

        if busy_card_fn is None:
            def busy_card_fn():
                return json.dumps(
                    {"schema": "2.0", "header": {"title": {"content": "⚠️ 团队繁忙"}}, "body": {"elements": []}},
                    ensure_ascii=False,
                )

        placeholder_card = json.dumps(
            {"schema": "2.0", "header": {"title": {"content": "⏳ Processing..."}}, "body": {"elements": []}},
            ensure_ascii=False,
        )

        handler._execute_async(
            engine=engine,
            execute_fn=execute_fn,
            placeholder_card_json=placeholder_card,
            result_card_fn=result_card_fn,
            error_card_fn=error_card_fn,
            empty_card_fn=empty_card_fn,
            busy_card_fn=busy_card_fn,
            message_id="msg-async-001",
            chat_id="chat-async-001",
        )

    def _make_immediate_executor(self):
        """Create a mock executor that runs submitted work synchronously."""
        mock_executor = MagicMock()

        def immediate_submit(fn, *args, **kwargs):
            fn()
            future = MagicMock()
            future.enqueue_time = 0
            return future

        mock_executor.submit = immediate_submit
        return mock_executor

    @patch("src.config.get_settings")
    def test_success_path(self, mock_get_settings):
        """execute_fn returns a result -> result_card_fn is called, card is updated."""
        settings = MagicMock()
        settings.slock_queue_wait_timeout = 60
        mock_get_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()
        engine._get_executor = MagicMock(return_value=self._make_immediate_executor())

        result_card_fn = MagicMock(return_value=json.dumps(
            {"schema": "2.0", "header": {"title": {"content": "✅ Done"}}, "body": {"elements": []}},
            ensure_ascii=False,
        ))

        self._call_execute_async(
            handler, engine,
            execute_fn=lambda: "result",
            result_card_fn=result_card_fn,
        )

        # Placeholder was sent
        handler.send_card_to_chat.assert_called_once()
        # result_card_fn was invoked with the result and a duration
        result_card_fn.assert_called_once()
        call_args = result_card_fn.call_args[0]
        assert call_args[0] == "result"
        assert isinstance(call_args[1], float)
        # Card was updated
        handler.update_card.assert_called()

    @patch("src.config.get_settings")
    def test_empty_result_path(self, mock_get_settings):
        """execute_fn returns None -> empty_card_fn is called."""
        settings = MagicMock()
        settings.slock_queue_wait_timeout = 60
        mock_get_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()
        engine._get_executor = MagicMock(return_value=self._make_immediate_executor())

        empty_card_fn = MagicMock(return_value=json.dumps(
            {"schema": "2.0", "header": {"title": {"content": "⚠️ Empty"}}, "body": {"elements": []}},
            ensure_ascii=False,
        ))

        self._call_execute_async(
            handler, engine,
            execute_fn=lambda: None,
            empty_card_fn=empty_card_fn,
        )

        # Placeholder was sent
        handler.send_card_to_chat.assert_called_once()
        # empty_card_fn was invoked
        empty_card_fn.assert_called_once()
        # Card was updated with empty card
        handler.update_card.assert_called()

    @patch("src.config.get_settings")
    def test_error_path(self, mock_get_settings):
        """execute_fn raises an exception -> error_card_fn is called."""
        settings = MagicMock()
        settings.slock_queue_wait_timeout = 60
        mock_get_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()
        engine._get_executor = MagicMock(return_value=self._make_immediate_executor())

        error_card_fn = MagicMock(return_value=json.dumps(
            {"schema": "2.0", "header": {"title": {"content": "❌ Error"}}, "body": {"elements": []}},
            ensure_ascii=False,
        ))

        def raise_error():
            raise RuntimeError("something broke")

        self._call_execute_async(
            handler, engine,
            execute_fn=raise_error,
            error_card_fn=error_card_fn,
        )

        # Placeholder was sent
        handler.send_card_to_chat.assert_called_once()
        # error_card_fn was invoked with the exception
        error_card_fn.assert_called_once()
        exc_arg = error_card_fn.call_args[0][0]
        assert isinstance(exc_arg, RuntimeError)
        assert "something broke" in str(exc_arg)
        # Card was updated
        handler.update_card.assert_called()

    def test_queue_full_path(self):
        """executor.submit raises QueueFullError -> busy_card_fn is called."""
        from src.slock_engine.bounded_executor import QueueFullError

        handler = _make_handler()
        engine = _make_engine_mock()

        mock_executor = MagicMock()
        mock_executor.submit = MagicMock(side_effect=QueueFullError("queue full"))
        engine._get_executor = MagicMock(return_value=mock_executor)

        busy_card_fn = MagicMock(return_value=json.dumps(
            {"schema": "2.0", "header": {"title": {"content": "⚠️ 团队繁忙"}}, "body": {"elements": []}},
            ensure_ascii=False,
        ))

        self._call_execute_async(
            handler, engine,
            execute_fn=lambda: "should not be called",
            busy_card_fn=busy_card_fn,
        )

        # Placeholder was sent
        handler.send_card_to_chat.assert_called_once()
        # busy_card_fn was invoked
        busy_card_fn.assert_called_once()
        # Card was updated with busy card
        handler.update_card.assert_called()

    @patch("src.config.get_settings")
    def test_queue_wait_timeout(self, mock_get_settings):
        """Task waited too long in queue -> timeout card is shown."""
        import time as _time

        settings = MagicMock()
        settings.slock_queue_wait_timeout = 5  # 5 second timeout
        mock_get_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()

        # _execute_async captures `future` via closure. In real thread pools,
        # `future = executor.submit(fn)` completes before fn() runs on another thread.
        # To simulate this, our mock submit stores the fn, returns the future,
        # and we run fn() *after* submit returns (via a deferred list).
        mock_executor = MagicMock()
        deferred_fns = []

        def submit_deferred(fn, *args, **kwargs):
            future = MagicMock()
            # Set enqueue_time to 10 seconds ago so the elapsed exceeds timeout
            future.enqueue_time = _time.time() - 10
            # Store fn for deferred execution (simulates thread pool scheduling)
            deferred_fns.append(fn)
            return future

        mock_executor.submit = submit_deferred
        engine._get_executor = MagicMock(return_value=mock_executor)

        execute_fn_called = []

        def track_execute():
            execute_fn_called.append(True)
            return "result"

        self._call_execute_async(
            handler, engine,
            execute_fn=track_execute,
        )

        # Now run the deferred work (simulates the thread pool executing the task)
        for fn in deferred_fns:
            fn()

        # Placeholder was sent
        handler.send_card_to_chat.assert_called_once()
        # Card was updated with timeout card
        handler.update_card.assert_called()
        update_call = handler.update_card.call_args_list[0]
        card_json = update_call[0][1]
        card_data = json.loads(card_json)
        assert "超时" in card_data["header"]["title"]["content"]
        # execute_fn should NOT have been called (timeout aborted before execution)
        assert len(execute_fn_called) == 0


# ============================================================
# TestHandleMessageUsesExecuteAsync: verify delegation
# ============================================================


class TestHandleMessageUsesExecuteAsync:
    """Verify that handle_message delegates to _execute_async."""

    @pytest.mark.slow
    def test_handle_message_delegates(self):
        """Mock _execute_async on the handler, call handle_message, assert _execute_async was called."""
        handler = _make_handler()
        engine = _make_engine_mock()
        engine.registry.find_by_name = MagicMock(return_value=None)

        handler.ctx.slock_engine_manager.get_activated_engine = MagicMock(return_value=engine)
        handler._execute_async = MagicMock()

        handler.handle_message("msg-delegate-001", "chat-001", "Hello agent", None)

        handler._execute_async.assert_called_once()
        call_kwargs = handler._execute_async.call_args[1]
        assert call_kwargs["engine"] is engine
        assert call_kwargs["message_id"] == "msg-delegate-001"
        assert call_kwargs["chat_id"] == "chat-001"
        assert callable(call_kwargs["execute_fn"])
        assert callable(call_kwargs["result_card_fn"])
        assert callable(call_kwargs["error_card_fn"])
        assert callable(call_kwargs["empty_card_fn"])
        assert callable(call_kwargs["busy_card_fn"])
        assert isinstance(call_kwargs["placeholder_card_json"], str)


# ============================================================
# TestSubmitTaskUsesExecuteAsync: verify delegation
# ============================================================


class TestSubmitTaskUsesExecuteAsync:
    """Verify that _submit_task_execution delegates to _execute_async."""

    @patch(PATCH_GET_SENDER, return_value="admin_001")
    @patch(PATCH_GET_SETTINGS)
    def test_submit_task_delegates(self, mock_settings, mock_sender):
        """Mock _execute_async on the handler, call _submit_task_execution, assert called."""
        settings = MagicMock()
        settings.admin_user_ids = frozenset(["admin_001"])
        mock_settings.return_value = settings

        handler = _make_handler()
        engine = _make_engine_mock()

        agent = AgentIdentity(
            agent_id="agent-delegate-001",
            name="DelegateCoder",
            emoji="🔧",
            agent_type="coco",
            owner_group="chat-001",
        )
        task = SlockTask(
            task_id="task-delegate-001",
            content="Do something",
            status=TaskStatus.TODO,
            created_in="chat-001",
        )

        handler._execute_async = MagicMock()

        handler._submit_task_execution(
            engine, task, agent, "msg-submit-001", "chat-001", "Do something", None
        )

        handler._execute_async.assert_called_once()
        call_kwargs = handler._execute_async.call_args[1]
        assert call_kwargs["engine"] is engine
        assert call_kwargs["message_id"] == "msg-submit-001"
        assert call_kwargs["chat_id"] == "chat-001"
        assert callable(call_kwargs["execute_fn"])
        assert callable(call_kwargs["result_card_fn"])
        assert callable(call_kwargs["error_card_fn"])
        assert callable(call_kwargs["empty_card_fn"])
        assert callable(call_kwargs["busy_card_fn"])
        assert isinstance(call_kwargs["placeholder_card_json"], str)
