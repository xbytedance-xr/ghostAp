import json
from unittest.mock import MagicMock, patch

import pytest

from src.card.streaming import StreamingCard, StreamingCardManager
from src.mode.manager import InteractionMode


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
        buttons = manager._build_buttons(mode=InteractionMode.COCO, project_id="proj_123")

        assert len(buttons) == 2
        assert buttons[0]["text"]["content"] == "🚪 退出Coco"
        assert buttons[0]["behaviors"][0]["value"]["action"] == "exit_coco"
        assert buttons[0]["behaviors"][0]["value"]["project_id"] == "proj_123"
        assert buttons[0].get("size") == "medium"
        assert buttons[1]["text"]["content"] == "🔄 切换项目"
        assert buttons[1].get("size") == "medium"

    def test_build_buttons_smart_mode(self, manager):
        buttons = manager._build_buttons(mode=InteractionMode.SMART, project_id="proj_123")

        assert len(buttons) == 4
        actions = [b["behaviors"][0]["value"]["action"] for b in buttons]
        assert "enter_coco" in actions
        assert "enter_claude" in actions
        assert "enter_gemini" in actions
        assert "enter_ttadk" in actions

    def test_create_streaming_card_success(self, manager, mock_client):
        card = manager.create_streaming_card(
            chat_id="chat_123",
            project_name="TestProject",
            project_path="/tmp/test",
            project_id="proj_456",
            initial_content="思考中...",
            mode=InteractionMode.COCO,
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
            mode=InteractionMode.COCO,
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
            mode=InteractionMode.CLAUDE,
        )
        assert card is not None
        assert card.header_template == "purple"

    def test_create_streaming_card_failure(self, manager, mock_client):
        card = manager.create_streaming_card(chat_id="chat_123", project_name="TestProject")

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
        )
        card.flow_control_state.min_update_interval_s = 0

        result = manager.update_content(card, "new content")

        import time
        for _ in range(20):
            if card.last_content == "new content":
                break
            time.sleep(0.05)

        assert result is True
        assert card.last_content == "new content"

    def test_update_content_skip_same_content(self, manager, mock_client):
        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="msg_123",
            last_content="same content",
        )

        result = manager.update_content(card, "same content")

        assert result is True
        mock_client.im.v1.message.patch.assert_not_called()

    def test_update_content_adaptive_rate(self, manager, mock_client):
        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="msg_123",
            last_content="start",
            last_content_len=5,
        )
        card.flow_control_state.last_arrival_time = 1000.0
        card.flow_control_state.content_arrival_rate = 0.0

        # Simulate high speed: 200 chars in 0.1s => rate = 2000 chars/s
        with patch("time.time", return_value=1000.1):
            manager.update_content(card, "start" + "x" * 200)

        # Expect interval to increase towards max (2.0)
        assert card.flow_control_state.content_arrival_rate > 100
        assert card.flow_control_state.min_update_interval_s == manager._flow_control.config.max_interval_s

        # Simulate low speed: 1 char in 1.0s => rate = 1 char/s
        # Need to reset time/rate to simulate sequence or just jump
        card.flow_control_state.last_arrival_time = 2000.0
        card.last_content_len = 205
        card.flow_control_state.content_arrival_rate = 0.0  # reset for clean test

        with patch("time.time", return_value=2001.0):
            manager.update_content(card, "start" + "x" * 200 + "y")

        # Expect interval to be base (0.3)
        assert card.flow_control_state.content_arrival_rate < 20
        assert card.flow_control_state.min_update_interval_s == manager._flow_control.config.base_interval_s

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
        title, template = manager._resolve_title_and_template("MyProject", mode=InteractionMode.COCO)
        assert title == "🤖 MyProject · Coco"
        assert template == "blue"

    def test_resolve_title_claude_with_project(self, manager):
        title, template = manager._resolve_title_and_template("MyProject", mode=InteractionMode.CLAUDE)
        assert title == "🔮 MyProject · Claude"
        assert template == "purple"

    def test_resolve_title_smart_with_project(self, manager):
        title, template = manager._resolve_title_and_template("MyProject", mode=InteractionMode.SMART)
        assert title == "🧠 MyProject"
        assert template == "turquoise"

    def test_resolve_title_coco_no_project(self, manager):
        title, template = manager._resolve_title_and_template(None, mode=InteractionMode.COCO)
        assert title == "🤖 编程模式"
        assert template == "blue"

    def test_resolve_title_claude_no_project(self, manager):
        title, template = manager._resolve_title_and_template(None, mode=InteractionMode.CLAUDE)
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

    # ---- _build_update_card_json (PATCH compatible) ----

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

        # PATCH 载荷使用 schema 2.0 包装
        assert card_json["schema"] == "2.0"
        assert "body" in card_json
        assert "elements" in card_json["body"]
        assert card_json["header"]["template"] == "blue"

        # PATCH 元素不应包含 schema 2.0 专属字段
        md_elements = [e for e in card_json["body"]["elements"] if e.get("tag") == "markdown"]
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
        assert payload["schema"] == "2.0"
        assert "body" in payload
        assert "elements" in payload["body"]

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
            mode=InteractionMode.COCO,
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
            mode=InteractionMode.CLAUDE,
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
            mode=InteractionMode.CLAUDE,
        )

        assert message_id == "msg_claude"
        req = mock_client.im.v1.message.create.call_args[0][0]
        card_data = json.loads(req.body.content)
        assert card_data["header"]["template"] == "purple"
        assert "Claude" in card_data["header"]["title"]["content"]

    def test_close_streaming_empty_string_falls_back_to_last_content(self, manager, mock_client):
        """close_streaming with empty string should use card.last_content instead."""
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.im.v1.message.patch.return_value = mock_response

        card = StreamingCard(
            chat_id="chat_456",
            title="🤖 Test",
            header_template="blue",
            message_id="msg_123",
            last_content="actual content from streaming",
        )

        result = manager.close_streaming(card, final_content="")

        assert result is True
        # Verify the PATCH payload uses last_content, not empty string
        req = mock_client.im.v1.message.patch.call_args[0][0]
        payload = json.loads(req.body.content)
        md_elements = [e for e in payload["body"]["elements"] if e.get("tag") == "markdown"]
        content_el = [e for e in md_elements if "actual content from streaming" in e["content"]]
        assert len(content_el) == 1

    def test_build_buttons_claude_mode(self, manager):
        buttons = manager._build_buttons(mode=InteractionMode.CLAUDE, project_id="proj_123")

        assert len(buttons) == 2
        assert buttons[0]["text"]["content"] == "🚪 退出Claude"
        assert buttons[0]["behaviors"][0]["value"]["action"] == "exit_claude"
        assert buttons[0]["behaviors"][0]["value"]["project_id"] == "proj_123"
        assert buttons[0].get("size") == "medium"
