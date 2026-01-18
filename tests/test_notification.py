import json
import pytest
from unittest.mock import MagicMock

from src.project.context import ProjectContext
from src.notification.hub import NotificationHub, NotificationType


class TestNotificationHub:
    @pytest.fixture
    def sample_project(self):
        return ProjectContext(
            project_id="test_project",
            project_name="Test Project",
            root_path="/tmp/test",
            theme_color="green",
            emoji_prefix="🟢",
        )

    @pytest.fixture
    def mock_send_callback(self):
        callback = MagicMock(return_value="msg_123")
        return callback

    def test_init_without_callback(self):
        hub = NotificationHub()
        assert hub._send_message is None

    def test_init_with_callback(self, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        assert hub._send_message == mock_send_callback

    def test_set_send_callback(self, mock_send_callback):
        hub = NotificationHub()
        hub.set_send_callback(mock_send_callback)
        assert hub._send_message == mock_send_callback

    def test_notify_stores_in_history(self, sample_project, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        
        hub.notify(
            chat_id="chat_123",
            notification_type=NotificationType.TASK_COMPLETED,
            title="Test",
            content="Test content",
            project=sample_project,
        )
        
        history = hub.get_recent_notifications(10)
        assert len(history) == 1
        assert history[0].title == "Test"

    def test_notify_calls_send_callback(self, sample_project, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        
        hub.notify(
            chat_id="chat_123",
            notification_type=NotificationType.TASK_COMPLETED,
            title="Test",
            content="Test content",
            project=sample_project,
        )
        
        mock_send_callback.assert_called_once()
        args = mock_send_callback.call_args[0]
        assert args[0] == "chat_123"
        assert args[2] == "interactive"

    def test_notify_without_callback_returns_none(self, sample_project):
        hub = NotificationHub()
        
        result = hub.notify(
            chat_id="chat_123",
            notification_type=NotificationType.TASK_COMPLETED,
            title="Test",
            content="Test content",
            project=sample_project,
        )
        
        assert result is None

    def test_notify_task_completed(self, sample_project, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        
        hub.notify_task_completed(
            chat_id="chat_123",
            project=sample_project,
            task_name="npm install",
            result_summary="安装了 100 个包",
            duration_seconds=45,
            suggestions=["运行 npm start"],
        )
        
        mock_send_callback.assert_called_once()
        content = mock_send_callback.call_args[0][1]
        
        assert "npm install" in content
        assert "45" in content

    def test_notify_task_failed(self, sample_project, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        
        hub.notify_task_failed(
            chat_id="chat_123",
            project=sample_project,
            task_name="build",
            error_message="Compilation error",
        )
        
        mock_send_callback.assert_called_once()
        content = mock_send_callback.call_args[0][1]
        
        assert "build" in content
        assert "Compilation error" in content

    def test_notify_coco_completed(self, sample_project, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        
        hub.notify_coco_completed(
            chat_id="chat_123",
            project=sample_project,
            query="帮我写一个排序函数",
            response_summary="已创建 sort.py 文件",
        )
        
        mock_send_callback.assert_called_once()
        content = mock_send_callback.call_args[0][1]
        
        assert "排序" in content

    def test_notify_project_switched(self, sample_project, mock_send_callback):
        from_project = ProjectContext(
            project_id="old_proj",
            project_name="Old Project",
            root_path="/tmp/old",
        )
        
        hub = NotificationHub(send_message_callback=mock_send_callback)
        
        hub.notify_project_switched(
            chat_id="chat_123",
            from_project=from_project,
            to_project=sample_project,
        )
        
        mock_send_callback.assert_called_once()
        content = mock_send_callback.call_args[0][1]
        
        assert "Old Project" in content
        assert "Test Project" in content

    def test_history_limit(self, sample_project, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        hub._max_history = 3
        
        for i in range(5):
            hub.notify(
                chat_id="chat_123",
                notification_type=NotificationType.TASK_COMPLETED,
                title=f"Test {i}",
                content="Content",
                project=sample_project,
            )
        
        history = hub.get_recent_notifications(10)
        assert len(history) == 3
        assert history[0].title == "Test 2"

    def test_clear_history(self, sample_project, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        
        hub.notify(
            chat_id="chat_123",
            notification_type=NotificationType.TASK_COMPLETED,
            title="Test",
            content="Content",
            project=sample_project,
        )
        
        hub.clear_history()
        
        history = hub.get_recent_notifications(10)
        assert len(history) == 0

    def test_notification_without_project(self, mock_send_callback):
        hub = NotificationHub(send_message_callback=mock_send_callback)
        
        hub.notify(
            chat_id="chat_123",
            notification_type=NotificationType.SYSTEM_ALERT,
            title="System Alert",
            content="System message",
            suggestions=["Check logs"],
        )
        
        mock_send_callback.assert_called_once()
        content = mock_send_callback.call_args[0][1]
        card = json.loads(content)
        
        assert "System Alert" in card["header"]["title"]["content"]
