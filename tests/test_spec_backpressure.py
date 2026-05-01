import json
from unittest.mock import MagicMock, patch

from src.feishu.ws_client import FeishuWSClient
from src.utils.rate_limit import RateLimitExceededException


@patch("src.feishu.ws_client.TaskScheduler")
def test_spec_backpressure(mock_scheduler_cls):
    mock_scheduler = mock_scheduler_cls.return_value

    # Simulate backpressure by raising the exception on submit for spec tasks
    def mock_submit(spec, fn):
        if spec.task_type == "spec_command":
            raise RateLimitExceededException("Rate limit exceeded")
        return MagicMock()

    mock_scheduler.submit.side_effect = mock_submit

    client = FeishuWSClient(message_callback=lambda x: None)
    client._reply_text = MagicMock()

    data = MagicMock()
    data.event.message.message_id = "msg1"
    data.event.message.chat_id = "chat1"
    data.event.message.content = json.dumps({"text": "/spec do something"})

    client._handle_message(data)

    client._reply_text.assert_called_once()
    args = client._reply_text.call_args[0]
    assert "系统繁忙" in args[1]
