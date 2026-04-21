import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.agent.intent_recognizer import IntentResult, IntentType, TaskStep
from src.feishu.ws_client import FeishuWSClient
from src.mode import InteractionMode
from src.project import ProjectContext
from src.tasking import TaskPriority


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
    mock_ws_client._reply_message = MagicMock()
    mock_ws_client._message_dispatcher.execute_task_step = MagicMock(return_value=True)
    
    mock_ws_client._process_with_intent("msg_123", "chat_456", "create a project and enter coco", project)
    
    # It should reply with a multi-task plan
    assert mock_ws_client._reply_message.call_count >= 1
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
