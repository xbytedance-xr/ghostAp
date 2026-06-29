"""Tests for surrogate sanitization in FeishuIMClient."""

import pytest

from src.feishu.im_client import _sanitize_content


class TestSanitizeContent:
    """Test _sanitize_content handles surrogate code points."""

    def test_clean_string_passes_through(self):
        """Normal strings are returned unchanged."""
        assert _sanitize_content("hello world") == "hello world"

    def test_chinese_passes_through(self):
        """Chinese characters pass through unchanged."""
        text = "你好世界 🎉 测试"
        assert _sanitize_content(text) == text

    def test_emoji_passes_through(self):
        """Proper emoji (not surrogates) pass through unchanged."""
        text = "status: ✅ done 🚀"
        assert _sanitize_content(text) == text

    def test_surrogate_replaced(self):
        """Unpaired surrogate code points are replaced."""
        # Create a string with an unpaired surrogate using surrogatepass
        bad = "hello \ud800 world"
        result = _sanitize_content(bad)
        # The surrogate should be replaced (not present in result)
        assert "\ud800" not in result
        assert "hello" in result
        assert "world" in result

    def test_surrogate_pair_replaced(self):
        """Surrogate pairs in isolation are handled."""
        bad = "prefix𐀀suffix"
        result = _sanitize_content(bad)
        # Should not raise, and should contain prefix/suffix
        assert "prefix" in result
        assert "suffix" in result

    def test_empty_string(self):
        """Empty string returns empty."""
        assert _sanitize_content("") == ""

    def test_json_content_with_surrogates(self):
        """JSON-like content (typical for card messages) with surrogates is sanitized."""
        # Simulate what happens when AI output with surrogates gets embedded in card JSON
        bad = '{"text": "result: \ud83d value"}'
        result = _sanitize_content(bad)
        assert "\ud83d" not in result
        assert "result:" in result
        assert "value" in result
