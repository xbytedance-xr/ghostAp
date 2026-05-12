"""Tests for FeishuCardAPIClient request mapping."""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.card.delivery.feishu_client import FeishuCardAPIClient


class _FakeResponse:
    code = 0
    msg = ""
    data = SimpleNamespace(message_id="msg_1")

    def success(self) -> bool:
        return True


class _FakeMessageApi:
    def __init__(self) -> None:
        self.created_request = None
        self.replied_request = None

    def create(self, request):
        self.created_request = request
        return _FakeResponse()

    def reply(self, request):
        self.replied_request = request
        return _FakeResponse()


def _client_for(message_api: _FakeMessageApi):
    return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=message_api)))


def test_create_card_sets_feishu_uuid_for_direct_message() -> None:
    message_api = _FakeMessageApi()
    client = FeishuCardAPIClient(_client_for(message_api))

    client.create_card("chat_1", {"body": {}}, idempotency_key="idem-1")

    body = message_api.created_request.request_body
    assert body.receive_id == "chat_1"
    assert body.msg_type == "interactive"
    assert body.uuid == "idem-1"


def test_create_card_sets_feishu_uuid_for_reply_message() -> None:
    message_api = _FakeMessageApi()
    client = FeishuCardAPIClient(_client_for(message_api))

    client.create_card("chat_1", {"body": {}}, reply_to="origin_1", idempotency_key="idem-2")

    assert message_api.replied_request.message_id == "origin_1"
    body = message_api.replied_request.request_body
    assert body.msg_type == "interactive"
    assert body.uuid == "idem-2"


def test_send_card_reference_sets_feishu_uuid() -> None:
    message_api = _FakeMessageApi()
    client = FeishuCardAPIClient(_client_for(message_api))

    client.send_card_reference("chat_1", "card_1", idempotency_key="idem-3")

    body = message_api.created_request.request_body
    assert json.loads(body.content) == {"type": "card", "data": {"card_id": "card_1"}}
    assert body.uuid == "idem-3"
