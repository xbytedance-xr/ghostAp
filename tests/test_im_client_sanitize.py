"""Tests for surrogate sanitization and file helpers in FeishuIMClient."""

import json
from unittest.mock import MagicMock

from src.feishu.im_client import FeishuIMClient, _sanitize_content


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


class _Response:
    def __init__(self, *, data=None, ok=True):
        self.data = data
        self._ok = ok
        self.code = 0 if ok else 1
        self.msg = "ok" if ok else "failed"

    def success(self):
        return self._ok


def test_upload_file_returns_file_key(tmp_path):
    """FeishuIMClient should upload a local file and expose file_key."""
    report = tmp_path / "report.html"
    report.write_text("<html>report</html>", encoding="utf-8")
    file_api = MagicMock()
    file_api.create.return_value = _Response(data=MagicMock(file_key="file_key_123"))
    client_obj = MagicMock()
    client_obj.im.v1.file = file_api

    client = FeishuIMClient(lambda: client_obj, MagicMock(im_api_max_retries=1))

    file_key = client.upload_file(str(report), file_type="stream")

    assert file_key == "file_key_123"
    file_api.create.assert_called_once()


def test_reply_file_sends_file_message_with_file_key():
    """FeishuIMClient.reply_file should reply with msg_type=file and file_key content."""
    message_api = MagicMock()
    message_api.reply.return_value = _Response(data=MagicMock(message_id="reply_1"))
    client_obj = MagicMock()
    client_obj.im.v1.message = message_api
    client = FeishuIMClient(lambda: client_obj, MagicMock(im_api_max_retries=1))

    response = client.reply_file("msg_1", "file_key_123", reply_in_thread=True)

    assert response is not None
    request = message_api.reply.call_args.args[0]
    body = request.request_body
    assert body.msg_type == "file"
    assert json.loads(body.content) == {"file_key": "file_key_123"}
