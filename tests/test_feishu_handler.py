import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.feishu.handler import FeishuEventHandler, MessageEvent


class TestFeishuEventHandler:
    def setup_method(self):
        self.handler = FeishuEventHandler()

    def test_handle_challenge(self):
        data = {"challenge": "test_challenge_token"}
        result = self.handler.handle_challenge(data)
        assert result == {"challenge": "test_challenge_token"}

    def test_handle_challenge_no_challenge(self):
        data = {"event": {}}
        result = self.handler.handle_challenge(data)
        assert result is None

    def test_extract_command_direct(self):
        command = self.handler.extract_command("ls -la")
        assert command == "ls -la"

    def test_extract_command_with_shell_prefix(self):
        command = self.handler.extract_command("/shell ls -la")
        assert command == "ls -la"

    def test_extract_command_with_sh_prefix(self):
        command = self.handler.extract_command("/sh whoami")
        assert command == "whoami"

    def test_extract_command_with_exec_prefix(self):
        command = self.handler.extract_command("/exec date")
        assert command == "date"

    def test_extract_command_with_dollar_prefix(self):
        command = self.handler.extract_command("$ pwd")
        assert command == "pwd"

    def test_extract_command_with_at_mention(self):
        command = self.handler.extract_command("@机器人 ls -la")
        assert command == "ls -la"

    def test_extract_command_unknown_slash_command(self):
        command = self.handler.extract_command("/help")
        assert command is None

    def test_extract_command_empty_after_at(self):
        command = self.handler.extract_command("@机器人")
        assert command is None

    def test_is_event_processed_first_time(self):
        result = self.handler.is_event_processed("event_123")
        assert result is False

    def test_is_event_processed_second_time(self):
        self.handler.is_event_processed("event_456")
        result = self.handler.is_event_processed("event_456")
        assert result is True

    def test_parse_event_wrong_type(self):
        data = {
            "header": {
                "event_type": "im.message.read_v1",
                "event_id": "test_event_id"
            },
            "event": {}
        }
        result = self.handler.parse_event(data)
        assert result is None

    def test_parse_event_valid(self):
        data = {
            "header": {
                "event_type": "im.message.receive_v1",
                "event_id": "test_event_id_valid"
            },
            "event": {
                "message": {
                    "message_id": "msg_123",
                    "chat_id": "chat_456",
                    "chat_type": "p2p",
                    "content": '{"text": "ls -la"}',
                    "message_type": "text",
                    "create_time": "1234567890"
                },
                "sender": {
                    "sender_id": {"open_id": "ou_xxx"},
                    "sender_type": "user"
                }
            }
        }
        result = self.handler.parse_event(data)
        assert result is not None
        assert result.message_id == "msg_123"
        assert result.content == "ls -la"


class TestMessageEvent:
    def test_from_event_data(self):
        data = {
            "header": {
                "event_id": "ev_123"
            },
            "event": {
                "message": {
                    "message_id": "msg_456",
                    "chat_id": "chat_789",
                    "chat_type": "group",
                    "content": '{"text": "hello"}',
                    "message_type": "text",
                    "create_time": "1234567890"
                },
                "sender": {
                    "sender_id": {"open_id": "ou_abc"},
                    "sender_type": "user"
                }
            }
        }
        event = MessageEvent.from_event_data(data)
        assert event.event_id == "ev_123"
        assert event.message_id == "msg_456"
        assert event.chat_id == "chat_789"
        assert event.content == "hello"
        assert event.sender_id == "ou_abc"

    def test_from_event_data_plain_content(self):
        data = {
            "header": {"event_id": "ev_plain"},
            "event": {
                "message": {
                    "message_id": "msg_plain",
                    "chat_id": "chat_plain",
                    "chat_type": "p2p",
                    "content": "plain text content",
                    "message_type": "text",
                    "create_time": "1234567890"
                },
                "sender": {
                    "sender_id": {"open_id": "ou_plain"},
                    "sender_type": "user"
                }
            }
        }
        event = MessageEvent.from_event_data(data)
        assert event.content == "plain text content"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
