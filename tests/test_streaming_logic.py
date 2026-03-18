import json
import time
import unittest
from unittest.mock import MagicMock, patch

from src.card.streaming import StreamingCardManager


class TestStreamingLogic(unittest.TestCase):
    def setUp(self):
        self.mock_client = MagicMock()
        self.manager = StreamingCardManager(self.mock_client)
        # Mock settings to ensure consistent layout behavior
        self.settings_patcher = patch("src.card.shared.get_settings")
        self.mock_settings = self.settings_patcher.start()
        self.mock_settings.return_value.card_button_layout = "responsive"
        self.mock_settings.return_value.card_button_size = "medium"

    def tearDown(self):
        self.settings_patcher.stop()

    def test_pagination_logic(self):
        # Create a card
        card = self.manager.create_streaming_card(chat_id="test_chat", project_name="TestProject")
        card.message_id = "test_msg_id"
        card.visible_chars = 10  # Small limit for testing
        card.pagination_step = 10
        card.size_threshold = 1  # Force update for this test
        self.manager._cards["test_msg_id"] = card

        # Helper to find content element
        def find_content(elements):
            mds = [e for e in elements if e.get("tag") == "markdown"]
            # Filter out elements that look like path (contain folder icon)
            content_mds = [e for e in mds if "📁" not in e["content"]]
            if content_mds:
                return content_mds[0]
            if len(mds) > 1:
                return mds[-1]
            return mds[0] if mds else None

        # 1. Update with content < limit
        content_short = "Short"
        self.manager.update_content(card, content_short)

        # Verify call args
        args, _ = self.mock_client.im.v1.message.patch.call_args
        req = args[0]
        body = json.loads(req.request_body.content)
        content_element = find_content(body["elements"])
        self.assertEqual(content_element["content"], "Short")

        # Check buttons - should NOT have "Load More"
        has_load_more = False
        for el in body["elements"]:
            if el.get("tag") == "column_set":
                for col in el.get("columns", []):
                    for item in col.get("elements", []):
                        if item.get("tag") == "button" and item.get("value", {}).get("action") == "load_more":
                            has_load_more = True
        self.assertFalse(has_load_more)

        # 2. Update with content > limit
        content_long = "This is a very long content that exceeds the limit."
        self.manager.update_content(card, content_long)

        args, _ = self.mock_client.im.v1.message.patch.call_args
        req = args[0]
        body = json.loads(req.request_body.content)
        content_element = find_content(body["elements"])

        # Should be truncated to 10 chars
        self.assertEqual(content_element["content"], content_long[:10])

        # Check buttons - SHOULD have "Load More"
        has_load_more = False
        for el in body["elements"]:
            if el.get("tag") == "column_set":
                for col in el.get("columns", []):
                    for item in col.get("elements", []):
                        if item.get("tag") == "button" and item.get("value", {}).get("action") == "load_more":
                            has_load_more = True
        self.assertTrue(has_load_more)

        # 3. Increase Pagination
        self.manager.increase_pagination("test_msg_id")

        # Verify update was called with new limit (20)
        args, _ = self.mock_client.im.v1.message.patch.call_args
        req = args[0]
        body = json.loads(req.request_body.content)
        content_element = find_content(body["elements"])

        self.assertEqual(content_element["content"], content_long[:20])

    def test_buffering_logic(self):
        card = self.manager.create_streaming_card(chat_id="test_chat")
        card.message_id = "test_buffer"

        # Override initial strategy state for testing
        card.flow_control_state.min_update_interval_s = 1.0

        card.size_threshold = 50
        card.last_update_at = time.time()  # Just updated
        card.last_content = "Base content"
        card.last_content_len = len("Base content")
        self.manager._cards["test_buffer"] = card

        # 1. Update with small change, short time -> Should BUFFER (return True but no API call)
        self.mock_client.im.v1.message.patch.reset_mock()
        result = self.manager.update_content(card, "Base content + small")
        self.assertTrue(result)
        self.mock_client.im.v1.message.patch.assert_not_called()

        # 2. Update with large change -> Should SEND
        self.mock_client.im.v1.message.patch.reset_mock()
        large_content = "Base content" + "A" * 60
        result = self.manager.update_content(card, large_content)
        self.assertTrue(result)
        self.mock_client.im.v1.message.patch.assert_called_once()

        # 3. Reset and test Time expiry
        card.last_update_at = time.time() - 2.0  # Expired
        card.last_content = large_content
        card.last_content_len = len(large_content)

        self.mock_client.im.v1.message.patch.reset_mock()
        result = self.manager.update_content(card, large_content + " small")
        self.assertTrue(result)
        self.mock_client.im.v1.message.patch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
