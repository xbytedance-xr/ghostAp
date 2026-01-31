import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from src.card.streaming import StreamingCard, StreamingCardManager


class TestStreamingCard:
    def test_streaming_card_creation(self):
        card = StreamingCard(
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456"
        )

        assert card.card_id == "card_123"
        assert card.element_id == "content_md"
        assert card.chat_id == "chat_456"
        assert card.message_id is None
        assert card.sequence == 1
        assert card.last_content == ""
        assert card.project_id is None

    def test_streaming_card_with_project(self):
        card = StreamingCard(
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456",
            project_id="proj_789"
        )

        assert card.project_id == "proj_789"


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
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.card_id = "card_abc123"
        mock_client.cardkit.v1.card.create.return_value = mock_response

        card = manager.create_streaming_card(
            chat_id="chat_123",
            project_name="TestProject",
            project_path="/tmp/test",
            project_id="proj_456",
            initial_content="思考中...",
            is_coco_mode=True
        )

        assert card is not None
        assert card.card_id == "card_abc123"
        assert card.chat_id == "chat_123"
        assert card.project_id == "proj_456"
        assert card.last_content == "思考中..."

    def test_create_streaming_card_with_images(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.card_id = "card_img"
        mock_client.cardkit.v1.card.create.return_value = mock_response

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
        # Verify the card JSON passed to API contains image elements
        call_args = mock_client.cardkit.v1.card.create.call_args
        request = call_args[0][0]
        import json
        card_data = json.loads(request.body.data)
        body_elements = card_data["body"]["elements"]
        img_elements = [e for e in body_elements if e.get("tag") == "img"]
        assert len(img_elements) == 2
        assert img_elements[0]["img_key"] == "img_v2_abc"
        assert img_elements[1]["img_key"] == "img_v2_def"

    def test_create_streaming_card_failure(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = False
        mock_response.code = 500
        mock_response.msg = "Internal error"
        mock_client.cardkit.v1.card.create.return_value = mock_response

        card = manager.create_streaming_card(
            chat_id="chat_123",
            project_name="TestProject"
        )

        assert card is None

    def test_send_streaming_card_success(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_response.data.message_id = "msg_xyz789"
        mock_client.im.v1.message.create.return_value = mock_response

        card = StreamingCard(
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456"
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
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456"
        )

        message_id = manager.send_streaming_card(card)

        assert message_id is None

    def test_update_content_success(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.cardkit.v1.card_element.content.return_value = mock_response

        card = StreamingCard(
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456",
            last_content="old content"
        )

        result = manager.update_content(card, "new content")

        assert result is True
        assert card.sequence == 2
        assert card.last_content == "new content"

    def test_update_content_skip_same_content(self, manager, mock_client):
        card = StreamingCard(
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456",
            last_content="same content"
        )

        result = manager.update_content(card, "same content")

        assert result is True
        assert card.sequence == 1
        mock_client.cardkit.v1.card_element.content.assert_not_called()

    def test_close_streaming_success(self, manager, mock_client):
        mock_response = MagicMock()
        mock_response.success.return_value = True
        mock_client.cardkit.v1.card.settings.return_value = mock_response

        card = StreamingCard(
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456"
        )

        with manager._lock:
            manager._cards["card_123"] = card

        result = manager.close_streaming(card)

        assert result is True
        assert "card_123" not in manager._cards

    def test_close_streaming_with_final_content(self, manager, mock_client):
        mock_update_response = MagicMock()
        mock_update_response.success.return_value = True
        mock_client.cardkit.v1.card_element.content.return_value = mock_update_response

        mock_settings_response = MagicMock()
        mock_settings_response.success.return_value = True
        mock_client.cardkit.v1.card.settings.return_value = mock_settings_response

        card = StreamingCard(
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456",
            last_content="old"
        )

        result = manager.close_streaming(card, final_content="final result")

        assert result is True
        mock_client.cardkit.v1.card_element.content.assert_called_once()

    def test_get_card(self, manager):
        card = StreamingCard(
            card_id="card_123",
            element_id="content_md",
            chat_id="chat_456"
        )

        with manager._lock:
            manager._cards["card_123"] = card

        retrieved = manager.get_card("card_123")
        assert retrieved is card

        not_found = manager.get_card("nonexistent")
        assert not_found is None

    def test_cleanup_expired_cards(self, manager):
        import time

        old_card = StreamingCard(
            card_id="old_card",
            element_id="content_md",
            chat_id="chat_456"
        )
        old_card.created_at = time.time() - 7200

        new_card = StreamingCard(
            card_id="new_card",
            element_id="content_md",
            chat_id="chat_789"
        )
        new_card.created_at = time.time()

        with manager._lock:
            manager._cards["old_card"] = old_card
            manager._cards["new_card"] = new_card

        manager.cleanup_expired_cards(max_age_seconds=3600)

        assert "old_card" not in manager._cards
        assert "new_card" in manager._cards
