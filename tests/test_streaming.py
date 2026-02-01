import json
import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from src.card.streaming import StreamingCard, StreamingCardManager


class TestStreamingCard:
    def test_streaming_card_creation(self):
        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
        )

        assert card.chat_id == "chat_456"
        assert card.message_id is None
        assert card.last_content == ""
        assert card.project_id is None

    def test_streaming_card_with_project(self):
        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            project_id="proj_789",
            project_path="/tmp/test",
        )

        assert card.project_id == "proj_789"
        assert card.project_path == "/tmp/test"


class TestStreamingCardManager:
    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        return client

    @pytest.fixture
    def manager(self, mock_client):
        return StreamingCardManager(mock_client)

    def test_build_buttons_coco_mode(self, manager):
        buttons = manager._build_buttons(is_coco_mode=True, project_id="proj_123")

        assert len(buttons) == 2
        assert buttons[0]["text"]["content"] == "🚪 退出Coco"
        assert buttons[0]["behaviors"][0]["value"]["action"] == "exit_coco"
        assert buttons[0]["behaviors"][0]["value"]["project_id"] == "proj_123"
        assert buttons[0].get("size") == "small"
        assert buttons[1]["text"]["content"] == "🔄 切换项目"
        assert buttons[1].get("size") == "small"

    def test_build_buttons_smart_mode(self, manager):
        buttons = manager._build_buttons(is_coco_mode=False, project_id="proj_123")

        assert len(buttons) == 2
        assert buttons[0]["text"]["content"] == "🤖 Coco模式"
        assert buttons[0]["behaviors"][0]["value"]["action"] == "enter_coco"
        assert buttons[0].get("size") == "small"
        assert buttons[1]["text"]["content"] == "🔮 Claude模式"
        assert buttons[1]["behaviors"][0]["value"]["action"] == "enter_claude"
        assert buttons[1].get("size") == "small"

    def test_create_streaming_card_success(self, manager, mock_client):
        card = manager.create_streaming_card(
            chat_id="chat_123",
            project_name="TestProject",
            project_path="/tmp/test",
            project_id="proj_456",
            initial_content="思考中...",
            is_coco_mode=True,
        )

        assert card is not None
        assert card.chat_id == "chat_123"
        assert card.project_id == "proj_456"
        assert card.project_path == "/tmp/test"
        assert card.last_content == "思考中..."
        assert card.header_template == "blue"

    def test_create_streaming_card_with_images(self, manager, mock_client):
        card = manager.create_streaming_card(
            chat_id="chat_123",
            project_name="TestProject",
            project_path="/tmp/test",
            project_id="proj_456",
            initial_content="思考中...",
            is_coco_mode=True,
            image_keys=["img_v2_abc", "img_v2_def"],
        )

        assert card is not None
        assert card.image_keys == ["img_v2_abc", "img_v2_def"]

    def test_create_streaming_card_claude_template_and_mobile_layout(self, manager, mock_client):
        card = manager.create_streaming_card(
            chat_id="chat_123",
            project_name="TestProject",
            project_path="/tmp/test",
            project_id="proj_456",
            initial_content="thinking...",
            is_coco_mode=False,
            is_claude_mode=True,
        )
        assert card is not None
        assert card.header_template == "purple"

    def test_create_streaming_card_failure(self, manager, mock_client):
        card = manager.create_streaming_card(
            chat_id="chat_123",
            project_name="TestProject"
        )

        assert card is not None

    def test_send_streaming_card_success(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "msg_xyz789"
        mock_client.im.v1.message.create.return_value = mock_response

        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
        )

        message_id = manager.send_streaming_card(card)

        assert message_id == "msg_xyz789"
        assert card.message_id == "msg_xyz789"

    def test_send_streaming_card_failure(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 403
        mock_response.msg = "Forbidden"
        mock_client.im.v1.message.create.return_value = mock_response

        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
        )

        message_id = manager.send_streaming_card(card)

        assert message_id is None

    def test_update_content_success(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.im.v1.message.patch.return_value = mock_response

        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="msg_123",
            last_content="old content",
            min_update_interval_s=0,
        )

        result = manager.update_content(card, "new content")

        assert result is True
        assert card.last_content == "new content"

    def test_update_content_skip_same_content(self, manager, mock_client):
        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="msg_123",
            last_content="same content"
        )

        result = manager.update_content(card, "same content")

        assert result is True
        mock_client.im.v1.message.patch.assert_not_called()

    def test_close_streaming_success(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.im.v1.message.patch.return_value = mock_response

        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="msg_123",
        )

        with manager._lock:
            manager._cards["msg_123"] = card

        result = manager.close_streaming(card)

        assert result is True
        assert "msg_123" not in manager._cards

    def test_close_streaming_with_final_content(self, manager, mock_client):
        mock_update_response = MagicMock()
        mock_update_response.success.return_value = True
        mock_client.im.v1.message.patch.return_value = mock_update_response

        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="msg_123",
            last_content="old",
        )

        result = manager.close_streaming(card, final_content="final result")

        assert result is True
        mock_client.im.v1.message.patch.assert_called_once()

    def test_get_card(self, manager):
        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="msg_123",
        )

        with manager._lock:
            manager._cards["msg_123"] = card

        retrieved = manager.get_card("msg_123")
        assert retrieved is card

        not_found = manager.get_card("nonexistent")
        assert not_found is None

    def test_cleanup_expired_cards(self, manager):
        import time

        old_card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="old_msg",
        )
        old_card.created_at = time.time() - 7200

        new_card = StreamingCard(
            chat_id="chat_789",
            title="🤖 Test",
            header_template="blue",
            message_id="new_msg",
        )
        new_card.created_at = time.time()

        with manager._lock:
            manager._cards["old_msg"] = old_card
            manager._cards["new_msg"] = new_card

        manager.cleanup_expired_cards(max_age_seconds=3600)

        assert "old_msg" not in manager._cards
        assert "new_msg" in manager._cards

    # ---- _resolve_title_and_template ----

    def test_resolve_title_coco_with_project(self, manager):
        title, template = manager._resolve_title_and_template("MyProject", is_coco_mode=True, is_claude_mode=False)
        assert title == "🤖 MyProject · Coco"
        assert template == "blue"

    def test_resolve_title_claude_with_project(self, manager):
        title, template = manager._resolve_title_and_template("MyProject", is_coco_mode=False, is_claude_mode=True)
        assert title == "🔮 MyProject · Claude"
        assert template == "purple"

    def test_resolve_title_smart_with_project(self, manager):
        title, template = manager._resolve_title_and_template("MyProject", is_coco_mode=False, is_claude_mode=False)
        assert title == "🧠 MyProject"
        assert template == "turquoise"

    def test_resolve_title_coco_no_project(self, manager):
        title, template = manager._resolve_title_and_template(None, is_coco_mode=True, is_claude_mode=False)
        assert title == "🤖 编程模式"
        assert template == "blue"

    def test_resolve_title_claude_no_project(self, manager):
        title, template = manager._resolve_title_and_template(None, is_coco_mode=False, is_claude_mode=True)
        assert title == "🔮 Claude 编程模式"
        assert template == "purple"

    # ---- _build_card_json ----

    def test_build_card_json_streaming_mode(self, manager):
        with patch("src.card.streaming.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.card_button_layout = "responsive"
            mock_get_settings.return_value = mock_settings

            card_json = manager._build_card_json(
                title="Test Title",
                header_template="blue",
                project_path="/tmp/test",
                initial_content="thinking...",
                streaming_mode=True,
            )

        assert card_json["schema"] == "2.0"
        assert card_json["config"]["streaming_mode"] is True
        assert "streaming_config" in card_json["config"]
        assert card_json["header"]["template"] == "blue"
        assert card_json["header"]["title"]["content"] == "Test Title"

        # 内容元素应包含 path 和 content
        elements = card_json["body"]["elements"]
        md_elements = [e for e in elements if e.get("tag") == "markdown"]
        assert any("📁" in e["content"] for e in md_elements)  # path
        assert any("thinking..." in e["content"] for e in md_elements)  # content

        # 验证 text_size 属性
        path_el = next(e for e in md_elements if "📁" in e["content"])
        assert path_el.get("text_size") == "notation"
        content_el = next(e for e in md_elements if "thinking..." in e["content"])
        assert content_el.get("text_size") == "normal"

    def test_build_card_json_non_streaming_mode(self, manager):
        with patch("src.card.streaming.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.card_button_layout = "responsive"
            mock_get_settings.return_value = mock_settings

            card_json = manager._build_card_json(
                title="Test Title",
                header_template="purple",
                project_path="/tmp/test",
                initial_content="完整内容",
                streaming_mode=False,
            )

        assert card_json["schema"] == "2.0"
        assert "streaming_mode" not in card_json["config"]
        assert "streaming_config" not in card_json["config"]
        assert card_json["config"]["wide_screen_mode"] is True
        assert card_json["config"]["update_multi"] is True

    def test_build_card_json_with_images(self, manager):
        with patch("src.card.streaming.get_settings") as mock_get_settings:
            mock_settings = MagicMock()
            mock_settings.card_button_layout = "responsive"
            mock_get_settings.return_value = mock_settings

            card_json = manager._build_card_json(
                title="Title",
                header_template="blue",
                initial_content="content",
                image_keys=["img_key_1", "img_key_2"],
                streaming_mode=False,
            )

        elements = card_json["body"]["elements"]
        img_elements = [e for e in elements if e.get("tag") == "img"]
        assert len(img_elements) == 2
        assert img_elements[0]["img_key"] == "img_key_1"

    # ---- _build_update_card_json (legacy for PATCH update) ----

    def test_build_update_card_json_is_legacy_format(self, manager):
        card_json = manager._build_update_card_json(
            title="Test Title",
            header_template="blue",
            project_path="/tmp/test",
            initial_content="thinking...",
            streaming_mode=True,
            buttons=None,
            image_keys=None,
        )

        # legacy 卡片没有 schema/body 包装
        assert "schema" not in card_json
        assert "body" not in card_json
        assert "elements" in card_json
        assert card_json["header"]["template"] == "blue"

        # legacy 元素不应包含 schema 2.0 专属字段
        md_elements = [e for e in card_json["elements"] if e.get("tag") == "markdown"]
        assert md_elements
        for el in md_elements:
            assert "text_size" not in el
            assert "element_id" not in el

    def test_send_streaming_card_uses_legacy_payload(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "msg_xyz789"
        mock_client.im.v1.message.create.return_value = mock_response

        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            last_content="思考中...",
        )

        message_id = manager.send_streaming_card(card)
        assert message_id == "msg_xyz789"

        req = mock_client.im.v1.message.create.call_args[0][0]
        payload = json.loads(req.body.content)
        assert "schema" not in payload
        assert "elements" in payload

    # ---- create_and_send_card ----

    def test_create_and_send_card_success_reply(self, manager, mock_client):
        # Mock message reply
        mock_msg_response = MagicMock()
        mock_msg_response.success.return_value = True
        mock_msg_response.data.message_id = "msg_reply_123"
        mock_client.im.v1.message.reply.return_value = mock_msg_response

        message_id = manager.create_and_send_card(
            chat_id="chat_123",
            content="完整回复内容",
            project_name="TestProject",
            project_path="/tmp/test",
            project_id="proj_456",
            is_coco_mode=True,
            reply_to_message_id="original_msg_id",
        )

        assert message_id == "msg_reply_123"
        mock_client.im.v1.message.reply.assert_called_once()
        # 验证发出的 message 内容是 schema 2.0 card JSON
        req = mock_client.im.v1.message.reply.call_args[0][0]
        card_data = json.loads(req.body.content)
        assert card_data["schema"] == "2.0"
        assert "streaming_mode" not in card_data["config"]

    def test_create_and_send_card_success_create(self, manager, mock_client):
        # Mock message create (no reply_to)
        mock_msg_response = MagicMock()
        mock_msg_response.success.return_value = True
        mock_msg_response.data.message_id = "msg_create_456"
        mock_client.im.v1.message.create.return_value = mock_msg_response

        message_id = manager.create_and_send_card(
            chat_id="chat_123",
            content="内容",
            is_claude_mode=True,
        )

        assert message_id == "msg_create_456"
        mock_client.im.v1.message.create.assert_called_once()

    def test_create_and_send_card_card_creation_fails(self, manager, mock_client):
        # 直接发送 schema 2.0 card JSON，不依赖 cardkit，因此这里不再有“卡片创建失败”场景。
        mock_msg_response = MagicMock()
        mock_msg_response.success.return_value = False
        mock_msg_response.code = 500
        mock_msg_response.msg = "Error"
        mock_client.im.v1.message.create.return_value = mock_msg_response

        message_id = manager.create_and_send_card(chat_id="chat_123", content="内容")
        assert message_id is None

    def test_create_and_send_card_message_send_fails(self, manager, mock_client):
        mock_msg_response = MagicMock()
        mock_msg_response.success.return_value = False
        mock_msg_response.code = 403
        mock_msg_response.msg = "Forbidden"
        mock_client.im.v1.message.create.return_value = mock_msg_response

        message_id = manager.create_and_send_card(
            chat_id="chat_123",
            content="内容",
        )

        assert message_id is None

    def test_create_and_send_card_claude_template(self, manager, mock_client):
        mock_msg_response = MagicMock()
        mock_msg_response.success.return_value = True
        mock_msg_response.data.message_id = "msg_claude"
        mock_client.im.v1.message.create.return_value = mock_msg_response

        message_id = manager.create_and_send_card(
            chat_id="chat_123",
            content="Claude 回复",
            project_name="ProjectX",
            is_coco_mode=False,
            is_claude_mode=True,
        )

        assert message_id == "msg_claude"
        req = mock_client.im.v1.message.create.call_args[0][0]
        card_data = json.loads(req.body.content)
        assert card_data["header"]["template"] == "purple"
        assert "Claude" in card_data["header"]["title"]["content"]

    def test_build_buttons_claude_mode(self, manager):
        buttons = manager._build_buttons(is_coco_mode=False, project_id="proj_123", is_claude_mode=True)

        assert len(buttons) == 2
        assert buttons[0]["text"]["content"] == "🚪 退出Claude"
        assert buttons[0]["behaviors"][0]["value"]["action"] == "exit_claude"
        assert buttons[0]["behaviors"][0]["value"]["project_id"] == "proj_123"
        assert buttons[0].get("size") == "small"
