import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.agent.intent_recognizer import IntentResult, IntentType, TaskStep
from src.feishu.ws_client import FeishuWSClient
from src.mode import InteractionMode
from src.project import ProjectContext
from src.tasking import TaskPriority
from src.thread import set_current_thread_id


@pytest.fixture
def mock_ws_client():
    # Patch heavy components and side-effects to keep tests fast and isolated
    with patch("src.feishu.ws_client.ACPSessionManager"), \
         patch("src.feishu.ws_client.configure_logging_with_trace"):
         
        def dummy_callback(*args, **kwargs):
            pass

        client = FeishuWSClient(message_callback=dummy_callback)
        
        # Patch the intent recognizer dynamically for tests
        client._intent_recognizer = MagicMock()
        
        # Patch the scheduler to intercept task submissions without real execution
        client._scheduler.submit = MagicMock()

        # Mock out message duplicate check to always pass
        client._message_cache.is_duplicate = MagicMock(return_value=False)

        # Block real Feishu API calls (add_reaction triggers real HTTP requests)
        client._add_reaction = MagicMock()

        yield client
        client.close()


def create_mock_message(text: str, message_id="msg_123", chat_id="chat_456", message_type="text"):
    data = MagicMock()
    data.event.message.message_id = message_id
    data.event.message.chat_id = chat_id
    data.event.message.content = json.dumps({"text": text})
    data.event.message.message_type = message_type
    data.event.message.create_time = str(int(time.time() * 1000))
    # Reset parent/root
    data.event.message.parent_id = None
    data.event.message.root_id = None
    return data


def test_handle_message_system_command_routing(mock_ws_client: FeishuWSClient):
    """Test that system commands (like /help) bypass project queue and get HIGH priority."""
    msg = create_mock_message("/help")
    
    mock_ws_client._handle_message(msg)
    
    submit_mock = mock_ws_client._scheduler.submit
    assert submit_mock.call_count == 1
    spec, _ = submit_mock.call_args[0]
    
    assert spec.task_type == "system_help"
    assert spec.priority == TaskPriority.HIGH
    assert spec.is_system_command is True
    # System commands should not block behind regular project tasks (often goes to control queue or no strict project queue)


def test_handle_message_shell_command_routing(mock_ws_client: FeishuWSClient):
    """Test that likely shell commands are fast-tracked to a shell-specific queue."""
    # Using 'ls -la' which is likely recognized as shell command by SystemHandler.is_likely_shell_command
    msg = create_mock_message("ls -la")
    
    mock_ws_client._handle_message(msg)
    
    submit_mock = mock_ws_client._scheduler.submit
    assert submit_mock.call_count == 1
    spec, _ = submit_mock.call_args[0]
    
    assert spec.task_type == "feishu_message"
    assert spec.priority == TaskPriority.NORMAL
    assert spec.is_system_command is False
    # Should use the fast-track shell queue
    assert spec.queue_key is not None
    assert ":shell:" in spec.queue_key


def test_handle_message_spec_command_routing(mock_ws_client: FeishuWSClient):
    """Test that spec commands use the spec rate limit configuration."""
    msg = create_mock_message("/spec do something")
    
    mock_ws_client._handle_message(msg)
    
    submit_mock = mock_ws_client._scheduler.submit
    assert submit_mock.call_count == 1
    spec, _ = submit_mock.call_args[0]
    
    assert spec.task_type == "spec_command"
    assert spec.is_system_command is True
    assert spec.priority == TaskPriority.HIGH
    assert spec.queue_key is not None
    assert ":control:" in spec.queue_key


def test_handle_message_plain_message_does_not_fallback_to_recent_engine_topic(mock_ws_client: FeishuWSClient):
    """Plain chat messages must not continue a topic-bound engine without root_id."""
    mock_ws_client.settings.thread_programming_enabled = True
    mock_ws_client._thread_manager.register(
        "thread-wt",
        "chat_456",
        "proj_1",
        mode="worktree",
    )
    msg = create_mock_message("继续")
    msg.event.message.root_id = None
    msg.event.message.parent_id = None

    mock_ws_client._handle_message(msg)

    spec, _ = mock_ws_client._scheduler.submit.call_args[0]
    assert spec.project_id is None
    assert not spec.queue_key or ":t:thread-wt" not in spec.queue_key


def test_resolve_message_context_plain_message_does_not_fallback_to_engine_topic(mock_ws_client: FeishuWSClient):
    """Context resolution should only use exact Feishu topic roots for engine continuation."""
    mock_ws_client.settings.thread_programming_enabled = True
    mock_ws_client._thread_manager.register(
        "thread-wt",
        "chat_456",
        "proj_1",
        mode="worktree",
    )
    fallback_project = ProjectContext("active", "Active", "/tmp")
    mock_ws_client._resolve_project_from_message = MagicMock(return_value=(fallback_project, None))
    msg = create_mock_message("继续")
    msg.event.message.root_id = None
    msg.event.message.parent_id = None

    project, auto_enter_mode = mock_ws_client._resolve_message_context(msg.event.message)

    assert project is fallback_project
    assert auto_enter_mode is None


def test_worktree_topic_goal_routes_without_interaction_mode_cast(mock_ws_client: FeishuWSClient):
    """A worktree topic is an engine context, not an InteractionMode enum value."""
    mock_ws_client.settings.thread_programming_enabled = True
    mock_ws_client._thread_manager.register(
        "thread-wt",
        "chat_456",
        "proj_1",
        mode="worktree",
    )
    project = ProjectContext("proj_1", "GhostAP", "/tmp")
    mock_ws_client._is_worktree_awaiting_goal = MagicMock(return_value=True)
    mock_ws_client._handle_worktree_execute = MagicMock()

    set_current_thread_id("thread-wt")
    try:
        mock_ws_client._message_dispatcher.process_with_intent(
            "msg_goal",
            "chat_456",
            "从不同的视角审查下当前项目的实现",
            project,
        )
    finally:
        set_current_thread_id(None)

    mock_ws_client._handle_worktree_execute.assert_called_once_with(
        "msg_goal",
        "chat_456",
        "从不同的视角审查下当前项目的实现",
        project,
    )


def test_dispatch_message_logic_worktree_topic_bypasses_project_chat_default(mock_ws_client: FeishuWSClient):
    """WT 话题里的普通消息应先交给 WT 引擎，不能掉到项目群默认 Coco 入口。"""
    project = ProjectContext("proj_1", "GhostAP", "/tmp")
    mock_ws_client._project_manager.find_by_bound_chat_id = MagicMock(return_value=project)
    mock_ws_client._process_with_intent = MagicMock()
    mock_ws_client._handle_worktree_execute = MagicMock()
    mock_ws_client._message_dispatcher._handle_enter_coco = MagicMock()

    mock_ws_client._dispatch_message_logic(
        "msg_goal",
        "chat_456",
        "修复卡片样式",
        project,
        "worktree",
        command_match=None,
    )

    mock_ws_client._handle_worktree_execute.assert_called_once_with(
        "msg_goal",
        "chat_456",
        "修复卡片样式",
        project,
    )
    mock_ws_client._process_with_intent.assert_not_called()
    mock_ws_client._message_dispatcher._handle_enter_coco.assert_not_called()


def test_worktree_topic_plain_text_keeps_wt_strategy_after_previous_goal(mock_ws_client: FeishuWSClient):
    """WT is a persistent topic strategy, not only an awaiting-goal transient state."""
    project = ProjectContext("proj_1", "GhostAP", "/tmp")
    mock_ws_client._handle_worktree_execute = MagicMock()
    mock_ws_client._process_with_intent = MagicMock()

    mock_ws_client._dispatch_message_logic(
        "msg_next",
        "chat_456",
        "继续优化刚才的实现",
        project,
        "worktree",
        command_match=None,
    )

    mock_ws_client._handle_worktree_execute.assert_called_once_with(
        "msg_next",
        "chat_456",
        "继续优化刚才的实现",
        project,
    )
    mock_ws_client._process_with_intent.assert_not_called()


@pytest.mark.parametrize(
    ("engine", "expected_method"),
    [
        ("deep", "_start_deep_engine"),
        ("spec", "_start_spec_engine"),
    ],
)
def test_deep_and_spec_topic_plain_text_keeps_engine_strategy(
    mock_ws_client: FeishuWSClient,
    engine: str,
    expected_method: str,
):
    """Deep/Spec topic continuation should not fall back to SMART intent routing."""
    project = ProjectContext("proj_1", "GhostAP", "/tmp")
    mock_ws_client._process_with_intent = MagicMock()
    setattr(mock_ws_client, expected_method, MagicMock())

    mock_ws_client._dispatch_message_logic(
        "msg_next",
        "chat_456",
        "继续按这个方向做",
        project,
        engine,
        command_match=None,
    )

    getattr(mock_ws_client, expected_method).assert_called_once_with(
        "msg_next",
        "chat_456",
        "继续按这个方向做",
        project,
    )
    mock_ws_client._process_with_intent.assert_not_called()


def test_process_message_async_auto_enter_mode(mock_ws_client: FeishuWSClient):
    """Test that an ongoing mode (auto_enter_mode) directly forwards to the respective handler."""
    msg = create_mock_message("hello")
    # Mock validation and parsing to skip actual processing overhead
    mock_ws_client._validate_message = MagicMock(return_value=True)
    
    # Mock resolving context to return a project and an auto-entered mode
    project = ProjectContext("proj_1", "Test", "/tmp")
    mock_ws_client._resolve_message_context = MagicMock(return_value=(project, "coco"))
    
    # Mock the mode handler
    mock_coco_handler = MagicMock()
    mock_ws_client._coco_handler = mock_coco_handler
    mock_ws_client._get_mode_handler = MagicMock(return_value=mock_coco_handler)
    
    # Execute the core async logic (synchronously in test)
    mock_ws_client._process_message_async(msg, task_ctx=MagicMock())
    
    # Since auto_enter_mode is 'coco', it should bypass intent recognition and call handle_message directly
    mock_ws_client._intent_recognizer.recognize.assert_not_called()
    mock_coco_handler.handle_message.assert_called_once_with(
        "msg_123", "chat_456", "hello", project
    )


def test_topic_bound_worktree_blocks_spec_switch_command(mock_ws_client: FeishuWSClient):
    """A WT topic must not be implicitly switched to Spec by a slash command."""
    project = ProjectContext("proj_1", "Test", "/tmp")
    mock_ws_client._reply_text = MagicMock()
    mock_ws_client._process_with_intent = MagicMock()

    mock_ws_client._dispatch_message_logic(
        "msg_123",
        "chat_456",
        "/spec rewrite this",
        project,
        "worktree",
        command_match=MagicMock(command="/spec"),
    )

    mock_ws_client._reply_text.assert_called_once()
    assert "WT" in mock_ws_client._reply_text.call_args.args[1]
    assert "Spec" in mock_ws_client._reply_text.call_args.args[1]
    mock_ws_client._process_with_intent.assert_not_called()


def test_topic_bound_worktree_blocks_spec_resume_command_family(mock_ws_client: FeishuWSClient):
    """Engine switch blocking applies to the whole command family, not only /spec."""
    project = ProjectContext("proj_1", "Test", "/tmp")
    mock_ws_client._reply_text = MagicMock()
    mock_ws_client._process_with_intent = MagicMock()

    mock_ws_client._dispatch_message_logic(
        "msg_123",
        "chat_456",
        "/spec_resume",
        project,
        "worktree",
        command_match=MagicMock(command="/spec_resume"),
    )

    mock_ws_client._reply_text.assert_called_once()
    assert "WT" in mock_ws_client._reply_text.call_args.args[1]
    assert "Spec" in mock_ws_client._reply_text.call_args.args[1]
    mock_ws_client._process_with_intent.assert_not_called()


def test_topic_bound_spec_allows_spec_command(mock_ws_client: FeishuWSClient):
    """Same-engine explicit commands remain available inside their topic."""
    project = ProjectContext("proj_1", "Test", "/tmp")
    mock_ws_client._reply_text = MagicMock()
    mock_ws_client._process_with_intent = MagicMock()
    mock_ws_client._is_interceptable_command_match = MagicMock(return_value=False)

    mock_ws_client._dispatch_message_logic(
        "msg_123",
        "chat_456",
        "/spec_status",
        project,
        "spec",
        command_match=MagicMock(command="/spec_status"),
    )

    mock_ws_client._reply_text.assert_not_called()
    mock_ws_client._process_with_intent.assert_called_once()


def test_deep_start_binds_topic_context(mock_ws_client: FeishuWSClient):
    """Starting Deep registers the current Feishu topic as a Deep strategy context."""
    project = ProjectContext("proj_1", "GhostAP", "/tmp")
    mock_ws_client._thread_manager.remove("msg_deep")
    mock_ws_client._deep_handler._submit_engine_task = MagicMock()
    mock_ws_client._deep_handler.add_reaction = MagicMock()
    mock_ws_client._deep_handler.ensure_request_id = MagicMock(return_value="req-1")
    mock_ws_client._deep_handler.ctx.deep_engine_manager.get = MagicMock(return_value=None)
    mock_ws_client._deep_handler.ctx.deep_engine_manager.get_or_create = MagicMock(return_value=MagicMock())

    set_current_thread_id(None)
    try:
        mock_ws_client._deep_handler.start_deep_engine("msg_deep", "chat_456", "深入分析", project)
    finally:
        set_current_thread_id(None)

    ctx = mock_ws_client._thread_manager.get("msg_deep")
    assert ctx is not None
    assert ctx.mode == "deep"
    assert ctx.project_id == "proj_1"


def test_spec_start_binds_topic_context(mock_ws_client: FeishuWSClient):
    """Starting Spec registers the current Feishu topic as a Spec strategy context."""
    project = ProjectContext("proj_1", "GhostAP", "/tmp")
    mock_ws_client._thread_manager.remove("msg_spec")
    mock_ws_client._spec_handler._submit_engine_task = MagicMock()
    mock_ws_client._spec_handler.add_reaction = MagicMock()
    mock_ws_client._spec_handler.ensure_request_id = MagicMock(return_value="req-1")
    mock_ws_client._spec_handler.ctx.spec_engine_manager.get = MagicMock(return_value=None)
    mock_ws_client._spec_handler.ctx.spec_engine_manager.get_or_create = MagicMock(return_value=MagicMock())

    set_current_thread_id(None)
    try:
        mock_ws_client._spec_handler.start_spec_engine("msg_spec", "chat_456", "写清规格", project)
    finally:
        set_current_thread_id(None)

    ctx = mock_ws_client._thread_manager.get("msg_spec")
    assert ctx is not None
    assert ctx.mode == "spec"
    assert ctx.project_id == "proj_1"


def test_exit_in_engine_topic_unbinds_topic_strategy(mock_ws_client: FeishuWSClient):
    """In an engine-only topic, /exit exits the topic strategy instead of reporting SMART."""
    project = ProjectContext("proj_1", "GhostAP", "/tmp")
    mock_ws_client._thread_manager.register("thread-wt-exit", "chat_456", "proj_1", mode="worktree")
    mock_ws_client._system_handler.reply_text = MagicMock()
    mock_ws_client._control_plane.should_defer_exit = MagicMock(return_value=False)

    set_current_thread_id("thread-wt-exit")
    try:
        mock_ws_client._dispatch_message_logic(
            "msg_exit",
            "chat_456",
            "/exit",
            project,
            "worktree",
            command_match=MagicMock(command="/exit"),
        )
    finally:
        set_current_thread_id(None)

    assert mock_ws_client._thread_manager.get("thread-wt-exit") is None
    mock_ws_client._system_handler.reply_text.assert_called_once()


def test_process_message_async_slash_parse_is_request_scoped(mock_ws_client: FeishuWSClient):
    """SlashCommandParser.parse must be called exactly once per message."""
    msg = create_mock_message("hello")
    mock_ws_client._validate_message = MagicMock(return_value=True)

    # Ensure we go through SMART routing (no auto-enter mode)
    project = ProjectContext("proj_1", "Test", "/tmp")
    mock_ws_client._resolve_message_context = MagicMock(return_value=(project, None))
    mock_ws_client._get_effective_mode = MagicMock(return_value=(InteractionMode.SMART, False))
    mock_ws_client._chat_lock_gate.check = MagicMock(return_value=False)

    # Avoid coupling to downstream task execution in this test
    mock_ws_client._intent_recognizer.recognize.return_value = IntentResult(
        confidence=0.9,
        tasks=[TaskStep(intent=IntentType.CREATE_PROJECT, data={"name": "p"}, description="Create")],
    )
    mock_ws_client._message_dispatcher.execute_single_task = MagicMock()

    # Keep message parsing minimal
    mock_img_handler = MagicMock()
    mock_img_handler.parse_message.return_value = MagicMock(text="hello", image_keys=[])
    mock_ws_client._get_image_handler = MagicMock(return_value=mock_img_handler)

    with patch("src.feishu.ws_client.SlashCommandParser.parse", return_value=None) as p:
        mock_ws_client._process_message_async(msg, task_ctx=MagicMock())
        assert p.call_count == 1


def test_process_with_intent_multitask(mock_ws_client: FeishuWSClient):
    """Test that intent recognizer correctly triggers multi-task execution."""
    project = ProjectContext("proj_1", "Test", "/tmp")
    mock_ws_client._get_effective_mode = MagicMock(return_value=(InteractionMode.SMART, False))
    
    # Mock the intent result to return a multi-task plan
    mock_intent_result = IntentResult(
        confidence=0.9,
        tasks=[
            TaskStep(intent=IntentType.CREATE_PROJECT, data={"name": "new_proj"}, description="Create project"),
            TaskStep(intent=IntentType.ENTER_COCO, data={}, description="Enter coco")
        ]
    )
    mock_ws_client._intent_recognizer.recognize.return_value = mock_intent_result
    
    # Mock message reply and task steps
    mock_ws_client._reply_text = MagicMock()
    mock_ws_client._message_dispatcher.execute_task_step = MagicMock(return_value=True)
    
    mock_ws_client._process_with_intent("msg_123", "chat_456", "create a project and enter coco", project)
    
    # It should reply with a multi-task plan
    assert mock_ws_client._reply_text.call_count >= 1
    # It should have called _execute_task_step for each task
    assert mock_ws_client._message_dispatcher.execute_task_step.call_count == 2
    
    call_args_list = mock_ws_client._message_dispatcher.execute_task_step.call_args_list
    assert call_args_list[0][0][2].intent == IntentType.CREATE_PROJECT
    assert call_args_list[1][0][2].intent == IntentType.ENTER_COCO


def test_process_with_intent_system_command_interception(mock_ws_client: FeishuWSClient):
    """Test that system commands bypass intent recognition completely during SMART mode."""
    project = ProjectContext("proj_1", "Test", "/tmp")
    mock_ws_client._get_effective_mode = MagicMock(return_value=(InteractionMode.SMART, False))
    
    mock_ws_client._handle_deep_command = MagicMock()
    
    # Send a deep engine command
    mock_ws_client._process_with_intent("msg_123", "chat_456", "/deep something", project)
    
    # Intent recognizer must not be called
    mock_ws_client._intent_recognizer.recognize.assert_not_called()
    # It should be directly routed to handle_deep_command
    mock_ws_client._handle_deep_command.assert_called_once_with("msg_123", "chat_456", "/deep something", project)


def test_card_action_deduplication_and_routing(mock_ws_client: FeishuWSClient):
    """Test card action callback ignores duplicates and routes correctly via ActionDispatcher."""
    # Create fake card action data
    data = MagicMock()
    data.header.event_id = "event_001"
    data.event.context.open_message_id = "msg_123"
    data.event.context.open_chat_id = "chat_456"
    data.event.action.value = '{"action": "show_status", "project_id": "proj_1"}'
    data.event.operator.open_id = "ou_test"
    data.event.operator.user_id = "u_test"
    
    # Mock deduplication cache to False
    mock_ws_client._card_event_cache.is_duplicate = MagicMock(return_value=False)
    
    # Inject action dispatcher spy
    mock_ws_client._action_dispatcher.dispatch = MagicMock(return_value=True)
    
    mock_ws_client._handle_card_action(data)
    
    # The action should be submitted as a task
    submit_mock = mock_ws_client._scheduler.submit
    assert submit_mock.call_count == 1
    spec, func = submit_mock.call_args[0]
    
    assert spec.task_type == "feishu_card_action"
    assert spec.project_id == "proj_1"
    # System card actions like show_status are HIGH priority and is_system_command
    assert spec.priority == TaskPriority.HIGH
    assert spec.is_system_command is True
    
    # Now run the callback to verify dispatcher routing
    task_ctx = MagicMock()
    func(task_ctx)
    
    # ActionDispatcher should have received the decoded value
    mock_ws_client._action_dispatcher.dispatch.assert_called_once()
    args, kwargs = mock_ws_client._action_dispatcher.dispatch.call_args
    assert args[0] == "show_status"
    assert args[1] == "msg_123"
    assert args[2] == "chat_456"
    assert args[3] == "proj_1"
    assert args[4]["action"] == "show_status"


# ---------------------------------------------------------------------------
# AC-18: chat-lock intercept card fallback on card send failure
# ---------------------------------------------------------------------------


class TestChatLockInterceptFallback:
    """AC-18: when the chat-lock intercept card fails to send, a plain text
    fallback message is delivered to the user.

    The card building + sending now lives in BaseHandler; ws_client delegates.
    """

    def test_fallback_text_on_card_build_failure(self, mock_ws_client):
        """Card build failure in handler → fallback plain text with 🔒."""
        from unittest.mock import MagicMock, patch
        from src.feishu.handlers.lock_helper import LockHelper

        handler = MagicMock()

        # Simulate card build failure inside handler method
        clm = MagicMock()
        clm.get_lock_info.side_effect = RuntimeError("db error")

        # Use the real LockHelper with the mock handler
        lock_helper = LockHelper(handler)
        lock_helper.send_chat_lock_intercept_card("msg_1", "chat_1", clm)

        # Fallback should have been called via reply_text
        handler.reply_text.assert_called_once()
        args = handler.reply_text.call_args[0]
        assert args[0] == "msg_1"
        assert "🔒" in args[1] or "locked" in args[1].lower() or "锁定" in args[1]

    def test_no_exception_when_both_fail(self, mock_ws_client):
        """Even if fallback also fails, no exception escapes."""
        from unittest.mock import MagicMock
        from src.feishu.handlers.lock_helper import LockHelper

        handler = MagicMock()
        handler.reply_text.side_effect = RuntimeError("all fail")

        clm = MagicMock()
        clm.get_lock_info.side_effect = RuntimeError("db error")

        # Should NOT raise
        lock_helper = LockHelper(handler)
        lock_helper.send_chat_lock_intercept_card("msg_2", "chat_2", clm)

    def test_chat_lock_gate_delegates_to_handler(self, mock_ws_client):
        """ChatLockGate._try_block delegates to handler layer via host._get_handler('system')."""
        from unittest.mock import MagicMock
        from src.feishu.chat_lock_gate import ChatLockGate
        from src.feishu.message_cache import MessageCache
        from src.feishu.slash_command_parser import SlashCommandParser

        clm = MagicMock()
        clm.should_block.return_value = True

        handler = MagicMock()
        host = MagicMock()
        host._get_handler.return_value = handler

        cache = MessageCache(ttl=30, max_size=10_000, cleanup_interval=60)
        gate = ChatLockGate(chat_lock_manager=clm, dedup_cache=cache, host=host)

        m = SlashCommandParser.parse("/test")
        assert m is not None
        blocked = gate._try_block("chat_1", "user_1", "msg_1", command_match=m)
        assert blocked is True
        handler.send_chat_lock_intercept_card.assert_called_once_with("msg_1", "chat_1", clm)


# ---------------------------------------------------------------------------
# AC-19: on_eviction callback wired in ws_client
# ---------------------------------------------------------------------------


class TestOnEvictionWiring:
    """AC-19: ProjectManager.on_eviction is wired to _on_project_evicted."""

    def test_on_eviction_is_wired(self, mock_ws_client):
        """After init, ProjectManager.on_eviction should not be None."""
        assert mock_ws_client._project_manager.on_eviction is not None

    def test_on_eviction_callback_callable(self, mock_ws_client):
        """The wired callback should be callable."""
        assert callable(mock_ws_client._project_manager.on_eviction)
