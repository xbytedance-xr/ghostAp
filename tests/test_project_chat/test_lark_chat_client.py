"""Tests for LarkChatClient — Feishu chat API wrapper."""
from unittest.mock import MagicMock

import pytest

from src.project_chat.errors import CreateChatError
from src.project_chat.lark_chat_client import CreateChatResult, LarkChatClient


@pytest.fixture
def mock_api_client():
    client = MagicMock()
    return client


@pytest.fixture
def chat_client(mock_api_client):
    return LarkChatClient(api_client_factory=lambda: mock_api_client)


class TestCreateChat:
    def test_create_chat_success(self, chat_client, mock_api_client):
        # Mock successful response
        response = MagicMock()
        response.success.return_value = True
        response.data = MagicMock()
        response.data.chat_id = "oc_test_chat_123"
        response.data.name = "myproject-dev"
        mock_api_client.im.v1.chat.create.return_value = response

        result = chat_client.create_chat(
            name="myproject-dev",
            description="test desc",
            user_id_list=["ou_user_1"],
        )

        assert isinstance(result, CreateChatResult)
        assert result.chat_id == "oc_test_chat_123"
        assert result.name == "myproject-dev"

    def test_create_chat_failure_raises(self, chat_client, mock_api_client):
        response = MagicMock()
        response.success.return_value = False
        response.code = 230001
        response.msg = "permission denied"
        mock_api_client.im.v1.chat.create.return_value = response

        with pytest.raises(CreateChatError, match="permission denied"):
            chat_client.create_chat(
                name="myproject-dev",
                description="test desc",
                user_id_list=["ou_user_1"],
            )


class TestDeleteChat:
    def test_delete_chat_success(self, chat_client, mock_api_client):
        response = MagicMock()
        response.success.return_value = True
        mock_api_client.im.v1.chat.delete.return_value = response

        # Should not raise
        chat_client.delete_chat("oc_test_chat_123")

    def test_delete_chat_failure_logs_warning(self, chat_client, mock_api_client):
        response = MagicMock()
        response.success.return_value = False
        response.code = 230099
        response.msg = "chat not found"
        mock_api_client.im.v1.chat.delete.return_value = response

        # delete_chat is best-effort for rollback, should not raise
        chat_client.delete_chat("oc_test_chat_123")
