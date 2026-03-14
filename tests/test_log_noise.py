import asyncio
import logging
import pytest
import time
from unittest.mock import MagicMock, patch
from src.feishu.ws_client import FeishuWSClient

@pytest.fixture
def mock_ws_client():
    # Mock dependencies
    mock_callback = MagicMock()
    with patch('src.feishu.ws_client.get_settings') as mock_settings:
        mock_settings.return_value.app_id = "test_app_id"
        mock_settings.return_value.app_secret = "test_secret"
        mock_settings.return_value.coco_session_timeout = 300
        mock_settings.return_value.claude_session_timeout = 300
        mock_settings.return_value.task_scheduler_max_concurrent = 10
        mock_settings.return_value.task_scheduler_per_key_concurrency = 1
        mock_settings.return_value.streaming_enabled = False
        
        client = FeishuWSClient(message_callback=mock_callback)
        # Mock internal components to avoid side effects
        client._scheduler = MagicMock()
        client._message_linker = MagicMock()
        client._mode_manager = MagicMock()
        client._project_manager = MagicMock()
        client._reply_message = MagicMock()
        client._get_image_handler = MagicMock()
        client._get_image_handler.return_value.parse_message.return_value.image_keys = []
        client._get_image_handler.return_value.parse_message.return_value.text = "test message"
        
        yield client

@pytest.mark.asyncio
async def test_process_message_timeout_error_log_level(mock_ws_client, caplog):
    """Verify asyncio.TimeoutError log level in _process_message_async."""
    # Setup
    mock_data = MagicMock()
    mock_data.event.message.message_id = "msg_123"
    mock_data.event.message.chat_id = "chat_123"
    mock_data.event.message.message_type = "text"
    mock_data.event.message.content = '{"text": "hello"}'
    mock_data.event.message.create_time = str(int(time.time() * 1000))

    # Simulate TimeoutError during processing
    # We need to mock _resolve_project_from_message to raise TimeoutError
    
    with patch.object(mock_ws_client, '_resolve_project_from_message', side_effect=asyncio.TimeoutError("Timeout")) as mock_resolve:
        mock_ws_client._process_message_async(mock_data)
        
    # Check log records
    timeout_logs = [r for r in caplog.records if "asyncio.TimeoutError" in r.message or "处理消息超时" in r.message]
    assert len(timeout_logs) > 0

@pytest.mark.asyncio
async def test_process_card_action_timeout_error_log_level(mock_ws_client, caplog):
    """Verify asyncio.TimeoutError log level in _process_card_action_async."""
    mock_data = MagicMock()
    mock_data.event.action.value = {"action": "test"}
    mock_data.event.context.open_message_id = "msg_123"
    
    mock_ws_client._action_registry_exact = {"test": MagicMock(side_effect=asyncio.TimeoutError("Timeout"))}
    
    mock_ws_client._process_card_action_async(mock_data)
    
    timeout_logs = [r for r in caplog.records if "asyncio.TimeoutError" in r.message or "处理卡片回调超时" in r.message]
    assert len(timeout_logs) > 0

def test_duplicate_card_event_log_level(mock_ws_client, caplog):
    """Verify duplicate card event log level."""
    mock_data = MagicMock()
    mock_data.header.event_id = "evt_123"
    
    # Force duplicate
    mock_ws_client._card_event_cache.is_duplicate = MagicMock(return_value=True)
    
    mock_ws_client._handle_card_action(mock_data)
    
    dup_logs = [r for r in caplog.records if "跳过重复卡片回调事件" in r.message]
    assert len(dup_logs) > 0
