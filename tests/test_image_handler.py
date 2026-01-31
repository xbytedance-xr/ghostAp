import pytest
import os
import json
import tempfile
from io import BytesIO
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from src.feishu.image_handler import (
    FeishuImageHandler,
    ImageParseResult,
    ImageDownloadResult,
)


class TestImageParseResult:
    def test_default_creation(self):
        result = ImageParseResult(text="hello")
        assert result.text == "hello"
        assert result.image_keys == []

    def test_creation_with_images(self):
        result = ImageParseResult(text="task", image_keys=["img_1", "img_2"])
        assert result.text == "task"
        assert result.image_keys == ["img_1", "img_2"]


class TestImageDownloadResult:
    def test_default_creation(self):
        result = ImageDownloadResult()
        assert result.saved_paths == []
        assert result.failed_keys == []

    def test_creation_with_values(self):
        result = ImageDownloadResult(
            saved_paths=["/a/b.png"],
            failed_keys=["img_fail"],
        )
        assert result.saved_paths == ["/a/b.png"]
        assert result.failed_keys == ["img_fail"]


class TestParseTextMessage:
    @pytest.fixture
    def handler(self):
        return FeishuImageHandler(MagicMock())

    def test_parse_standard_text(self, handler):
        content = json.dumps({"text": "hello world"})
        result = handler.parse_message("text", content)
        assert result.text == "hello world"
        assert result.image_keys == []

    def test_parse_empty_text(self, handler):
        content = json.dumps({"text": ""})
        result = handler.parse_message("text", content)
        assert result.text == ""
        assert result.image_keys == []

    def test_parse_text_no_text_key(self, handler):
        content = json.dumps({"other": "value"})
        result = handler.parse_message("text", content)
        assert result.text == ""

    def test_parse_invalid_json(self, handler):
        result = handler.parse_message("text", "not json")
        assert result.text == "not json"
        assert result.image_keys == []


class TestParseImageMessage:
    @pytest.fixture
    def handler(self):
        return FeishuImageHandler(MagicMock())

    def test_parse_single_image(self, handler):
        content = json.dumps({"image_key": "img_v2_abc123"})
        result = handler.parse_message("image", content)
        assert result.text == ""
        assert result.image_keys == ["img_v2_abc123"]

    def test_parse_empty_image_key(self, handler):
        content = json.dumps({"image_key": ""})
        result = handler.parse_message("image", content)
        assert result.text == ""
        assert result.image_keys == []

    def test_parse_no_image_key(self, handler):
        content = json.dumps({"other": "value"})
        result = handler.parse_message("image", content)
        assert result.image_keys == []

    def test_parse_image_invalid_json(self, handler):
        result = handler.parse_message("image", "not json")
        assert result.text == ""
        assert result.image_keys == []


class TestParsePostMessage:
    @pytest.fixture
    def handler(self):
        return FeishuImageHandler(MagicMock())

    def _build_post(self, content_rows, lang="zh_cn", title=""):
        return json.dumps({
            lang: {
                "title": title,
                "content": content_rows,
            }
        })

    def test_parse_text_only(self, handler):
        content = self._build_post([
            [{"tag": "text", "text": "hello world"}]
        ])
        result = handler.parse_message("post", content)
        assert result.text == "hello world"
        assert result.image_keys == []

    def test_parse_images_only(self, handler):
        content = self._build_post([
            [{"tag": "img", "image_key": "img_v2_abc"}]
        ])
        result = handler.parse_message("post", content)
        assert result.text == ""
        assert result.image_keys == ["img_v2_abc"]

    def test_parse_mixed_text_and_images(self, handler):
        content = self._build_post([
            [
                {"tag": "text", "text": "请看这张图"},
                {"tag": "img", "image_key": "img_v2_abc"},
            ]
        ])
        result = handler.parse_message("post", content)
        assert result.text == "请看这张图"
        assert result.image_keys == ["img_v2_abc"]

    def test_parse_multiple_rows(self, handler):
        content = self._build_post([
            [{"tag": "text", "text": "第一行"}],
            [{"tag": "text", "text": "第二行"}, {"tag": "img", "image_key": "img_1"}],
            [{"tag": "img", "image_key": "img_2"}],
        ])
        result = handler.parse_message("post", content)
        assert result.text == "第一行 第二行"
        assert result.image_keys == ["img_1", "img_2"]

    def test_parse_multiple_images(self, handler):
        content = self._build_post([
            [
                {"tag": "img", "image_key": "img_a"},
                {"tag": "img", "image_key": "img_b"},
                {"tag": "img", "image_key": "img_c"},
            ]
        ])
        result = handler.parse_message("post", content)
        assert len(result.image_keys) == 3

    def test_parse_at_mentions_skipped(self, handler):
        content = self._build_post([
            [
                {"tag": "at", "user_id": "u123"},
                {"tag": "text", "text": "请帮我看看"},
            ]
        ])
        result = handler.parse_message("post", content)
        assert result.text == "请帮我看看"

    def test_parse_en_us_fallback(self, handler):
        content = json.dumps({
            "en_us": {
                "title": "",
                "content": [
                    [{"tag": "text", "text": "english text"}]
                ],
            }
        })
        result = handler.parse_message("post", content)
        assert result.text == "english text"

    def test_parse_first_available_lang(self, handler):
        content = json.dumps({
            "ja_jp": {
                "title": "",
                "content": [
                    [{"tag": "text", "text": "日本語"}]
                ],
            }
        })
        result = handler.parse_message("post", content)
        assert result.text == "日本語"

    def test_parse_empty_content(self, handler):
        content = self._build_post([])
        result = handler.parse_message("post", content)
        assert result.text == ""
        assert result.image_keys == []

    def test_parse_no_content_key(self, handler):
        content = json.dumps({"zh_cn": {"title": "test"}})
        result = handler.parse_message("post", content)
        assert result.text == ""

    def test_parse_invalid_json(self, handler):
        result = handler.parse_message("post", "not json")
        assert result.text == "not json"
        assert result.image_keys == []

    def test_empty_text_elements_ignored(self, handler):
        content = self._build_post([
            [{"tag": "text", "text": ""}, {"tag": "text", "text": "有效内容"}]
        ])
        result = handler.parse_message("post", content)
        assert result.text == "有效内容"

    def test_empty_image_key_ignored(self, handler):
        content = self._build_post([
            [{"tag": "img", "image_key": ""}, {"tag": "img", "image_key": "valid_key"}]
        ])
        result = handler.parse_message("post", content)
        assert result.image_keys == ["valid_key"]


class TestParseMessageRouter:
    @pytest.fixture
    def handler(self):
        return FeishuImageHandler(MagicMock())

    def test_unsupported_type(self, handler):
        result = handler.parse_message("audio", '{"key": "val"}')
        assert result.text == ""
        assert result.image_keys == []

    def test_file_type_unsupported(self, handler):
        result = handler.parse_message("file", '{"file_key": "xxx"}')
        assert result.text == ""
        assert result.image_keys == []


class TestDownloadImages:
    @pytest.fixture
    def mock_client(self):
        return MagicMock()

    @pytest.fixture
    def handler(self, mock_client):
        return FeishuImageHandler(mock_client)

    def test_download_empty_list(self, handler):
        result = handler.download_images("msg_1", [], "/tmp/test")
        assert result.saved_paths == []
        assert result.failed_keys == []

    def test_download_single_image_success(self, handler, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.file = BytesIO(b"\x89PNG\r\n\x1a\nfakeimage")
        mock_client.im.v1.message_resource.get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            result = handler.download_images("msg_1", ["img_v2_abc"], tmpdir)
            assert len(result.saved_paths) == 1
            assert result.failed_keys == []
            assert os.path.exists(result.saved_paths[0])
            assert result.saved_paths[0].endswith(".png")
            assert "img_v2_abc" in result.saved_paths[0]

    def test_download_multiple_images(self, handler, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.file = BytesIO(b"fakedata")
        mock_client.im.v1.message_resource.get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            result = handler.download_images(
                "msg_1", ["img_1", "img_2", "img_3"], tmpdir
            )
            assert len(result.saved_paths) == 3
            assert result.failed_keys == []

    def test_download_api_failure(self, handler, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 99999
        mock_response.msg = "permission denied"
        mock_client.im.v1.message_resource.get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            result = handler.download_images("msg_1", ["img_fail"], tmpdir)
            assert result.saved_paths == []
            assert result.failed_keys == ["img_fail"]

    def test_download_partial_failure(self, handler, mock_client):
        success_resp = MagicMock()
        success_resp.success.return_value = True
        success_resp.file = BytesIO(b"ok")

        fail_resp = MagicMock()
        fail_resp.success.return_value = False
        fail_resp.code = 99999
        fail_resp.msg = "error"

        mock_client.im.v1.message_resource.get.side_effect = [
            success_resp, fail_resp, success_resp
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            result = handler.download_images(
                "msg_1", ["img_ok1", "img_fail", "img_ok2"], tmpdir
            )
            assert len(result.saved_paths) == 2
            assert result.failed_keys == ["img_fail"]

    def test_download_creates_directory(self, handler, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.file = BytesIO(b"data")
        mock_client.im.v1.message_resource.get.return_value = mock_response

        with tempfile.TemporaryDirectory() as tmpdir:
            save_dir = os.path.join(tmpdir, "nested", "picturechat")
            result = handler.download_images("msg_1", ["img_1"], save_dir)
            assert len(result.saved_paths) == 1
            assert os.path.isdir(save_dir)

    def test_download_exception_handling(self, handler, mock_client):
        mock_client.im.v1.message_resource.get.side_effect = Exception("network error")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = handler.download_images("msg_1", ["img_err"], tmpdir)
            assert result.saved_paths == []
            assert result.failed_keys == ["img_err"]


class TestBuildImageReferenceText:
    def test_empty_paths(self):
        assert FeishuImageHandler.build_image_reference_text([]) == ""

    def test_single_path(self):
        text = FeishuImageHandler.build_image_reference_text(["/a/b/img.png"])
        assert "[参考图片]" in text
        assert "- /a/b/img.png" in text

    def test_multiple_paths(self):
        paths = ["/a/img1.png", "/a/img2.png", "/a/img3.png"]
        text = FeishuImageHandler.build_image_reference_text(paths)
        assert text.count("- /a/") == 3
        assert "[参考图片]" in text

    def test_format_starts_with_newlines(self):
        text = FeishuImageHandler.build_image_reference_text(["/x.png"])
        assert text.startswith("\n\n")


class TestGetImageSaveDir:
    def test_with_project_root(self):
        result = FeishuImageHandler.get_image_save_dir("/home/user/project", "/fallback")
        assert result == "/home/user/project/picturechat"

    def test_without_project_root(self):
        result = FeishuImageHandler.get_image_save_dir(None, "/fallback/dir")
        assert result == "/fallback/dir/picturechat"

    def test_empty_string_project_root(self):
        result = FeishuImageHandler.get_image_save_dir("", "/fallback")
        assert result == "/fallback/picturechat"
