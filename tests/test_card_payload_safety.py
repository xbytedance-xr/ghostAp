import json
import unittest
from unittest.mock import MagicMock

from src.feishu.handlers.base import BaseHandler
from src.feishu.renderers.base import BaseRenderer


class TestCardPayloadSafety(unittest.TestCase):
    def setUp(self):
        self.mock_handler = MagicMock(spec=BaseHandler)
        self.mock_handler.ctx = MagicMock()
        self.mock_handler.settings = MagicMock()
        self.renderer = BaseRenderer(self.mock_handler)

    def test_payload_no_truncation(self):
        """Test small payload passes through"""
        small_content = json.dumps({"text": "hello"})
        result = self.renderer._check_and_truncate_payload(small_content, max_size=1000)
        self.assertEqual(result, small_content)

    def test_payload_truncation(self):
        """Test large string fields are truncated when they exceed the per-string cap."""
        # Threshold for content/text field truncation is 8000 chars.
        long_text = "a" * 20000
        card = {"header": {"title": "Test"}, "elements": [{"tag": "markdown", "content": long_text}]}
        content = json.dumps(card)

        # Set limit smaller than content so truncation path is taken.
        max_size = 10000
        result = self.renderer._check_and_truncate_payload(content, max_size=max_size)

        # Verify result is valid JSON
        truncated_card = json.loads(result)
        truncated_text = truncated_card["elements"][0]["content"]

        # Verify text was truncated (capped at 8000 + suffix)
        self.assertTrue(len(truncated_text) < 20000)
        self.assertTrue(truncated_text.endswith("...(已截断)"))

    def test_recursive_truncation(self):
        """Test nested objects are processed — uses the >10K fallback string cap."""
        long_text = "b" * 20000
        card = {"body": {"nested": {"deep": long_text}}}
        content = json.dumps(card)

        result = self.renderer._check_and_truncate_payload(content, max_size=12000)
        truncated_card = json.loads(result)

        deep_val = truncated_card["body"]["nested"]["deep"]
        self.assertTrue(len(deep_val) < 20000)
        self.assertIn("已截断", deep_val)

    def test_fallback_truncation(self):
        """Test drastic fallback if truncation fails to reduce size enough"""
        # Even after truncating strings to 2000 chars, if we have MANY fields, it might still be too big
        # Construct a card with many fields
        card = {"items": ["a" * 100 for _ in range(100)]}  # ~10KB
        content = json.dumps(card)

        # Set impossible limit
        max_size = 100

        result = self.renderer._check_and_truncate_payload(content, max_size=max_size)

        # Should return fallback card
        fallback = json.loads(result)
        self.assertEqual(fallback["header"]["title"]["content"], "⚠️ 卡片过大")


if __name__ == "__main__":
    unittest.main()
