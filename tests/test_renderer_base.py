import unittest
from unittest.mock import MagicMock, patch

from src.feishu.renderers.base import SmartSender


class TestSmartSender(unittest.TestCase):
    def setUp(self):
        self.handler = MagicMock()
        self.settings = MagicMock()
        self.handler.settings = self.settings
        self.settings.deep_stream_interval = 1.0
        self.settings.deep_stream_min_chars = 10
        self.settings.default_reply_mode = "thread"

        self.sender = SmartSender(self.handler, "msg_id", "chat_id")

    def test_throttle_logic(self):
        """Test throttling logic based on time and character count."""
        # Initial state: no last update
        self.assertTrue(self.sender.check_throttle(100), "First check should pass")

        # Update state
        self.sender.update_stream_state(100)
        self.sender.last_stream_ts = 1000.0  # Set a base time

        # Too soon, too few chars
        with patch("time.monotonic", return_value=1000.5):
            self.assertFalse(self.sender.check_throttle(105), "Should throttle: < interval and < chars")

        # Enough time, few chars
        with patch("time.monotonic", return_value=1001.1):
            self.assertTrue(self.sender.check_throttle(105), "Should pass: > interval")

        # Short time, enough chars
        with patch("time.monotonic", return_value=1000.5):
            self.assertTrue(self.sender.check_throttle(120), "Should pass: > chars")

        # Force pass
        with patch("time.monotonic", return_value=1000.5):
            self.assertTrue(self.sender.check_throttle(105, force=True), "Should pass: force=True")

    def test_send_new_message(self):
        """Test sending a new message (first time)."""
        self.handler.reply_message.return_value = "new_msg_id"

        mid = self.sender.send("content")

        # Check result
        self.assertEqual(mid, "new_msg_id")
        self.assertEqual(self.sender.current_message_id, "new_msg_id")
        self.assertEqual(self.sender.thread_root_message_id, "new_msg_id")

        # Check handler call
        self.handler.reply_message.assert_called_once()
        args, kwargs = self.handler.reply_message.call_args
        self.assertEqual(args[0], "msg_id")  # reply to original message
        self.assertTrue(kwargs["reply_in_thread"])

    def test_send_update_success(self):
        """Test successful patch of existing message."""
        self.sender.current_message_id = "curr_msg_id"
        self.handler.patch_message.return_value = True

        mid = self.sender.send("content", is_update=True)

        self.assertEqual(mid, "curr_msg_id")
        self.handler.patch_message.assert_called_once_with("curr_msg_id", "content", max_retries=1, throttle=False)
        self.handler.reply_message.assert_not_called()

    def test_send_update_fail_reanchor(self):
        """Test re-anchoring when patch fails."""
        self.sender.current_message_id = "curr_msg_id"
        self.sender.thread_root_message_id = "root_msg_id"

        # Mock patch failure
        self.handler.patch_message.return_value = False
        # Mock reply success
        self.handler.reply_message.return_value = "new_msg_id"

        mid = self.sender.send("content", is_update=True)

        self.assertEqual(mid, "new_msg_id")
        self.handler.patch_message.assert_called_once()
        self.handler.reply_message.assert_called_once()

        # Check reply target is thread root
        args, kwargs = self.handler.reply_message.call_args
        self.assertEqual(args[0], "root_msg_id")

        # Check state update
        self.assertEqual(self.sender.current_message_id, "new_msg_id")

    def test_send_in_chat_mode(self):
        """Test sending when default_reply_mode is not thread."""
        self.settings.default_reply_mode = "chat"
        self.handler.send_message.return_value = "chat_msg_id"

        mid = self.sender.send("content")

        self.assertEqual(mid, "chat_msg_id")
        self.handler.send_message.assert_called_once()
        self.handler.reply_message.assert_not_called()
        self.assertEqual(self.sender.current_message_id, "chat_msg_id")


if __name__ == "__main__":
    unittest.main()
