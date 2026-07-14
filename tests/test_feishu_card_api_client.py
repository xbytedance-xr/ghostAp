"""Tests for FeishuCardAPIClient request mapping."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

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


def test_card_message_create_reply_and_patch_are_audited_before_sdk_dispatch() -> None:
    message_api = _FakeMessageApi()
    message_api.patch = lambda _request: _FakeResponse()
    events: list[tuple[str, str, str]] = []
    client = FeishuCardAPIClient(
        _client_for(message_api),
        outbound_audit=lambda tenant, operation, target: events.append(
            (tenant, operation, target)
        ),
        tenant_key_resolver=lambda: "tenant-a",
    )

    client.create_card("chat_1", {"body": {}})
    client.create_card("chat_1", {"body": {}}, reply_to="origin_1")
    client.update_card("card_1", {"body": {}})
    client.send_card_reference("chat_1", "card_entity")
    client.send_card_reference("chat_1", "card_entity", reply_to="origin_2")

    assert events == [
        ("tenant-a", "create", "chat_1"),
        ("tenant-a", "reply", "origin_1"),
        ("tenant-a", "patch", "card_1"),
        ("tenant-a", "create", "chat_1"),
        ("tenant-a", "reply", "origin_2"),
    ]


def test_call_api_times_out_without_waiting_for_stuck_sdk_call(monkeypatch) -> None:
    message_api = _FakeMessageApi()
    client = FeishuCardAPIClient(_client_for(message_api))
    release = threading.Event()

    monkeypatch.setattr(client, "_api_timeout_seconds", lambda: 0.01)

    started_at = time.monotonic()
    with pytest.raises(TimeoutError):
        client._call_api("slow.test", lambda: release.wait(timeout=1.0))

    assert time.monotonic() - started_at < 0.5
    release.set()


def test_call_api_rejects_when_worker_slots_are_exhausted(monkeypatch) -> None:
    message_api = _FakeMessageApi()
    client = FeishuCardAPIClient(_client_for(message_api))
    release = threading.Event()
    slots = threading.BoundedSemaphore(1)

    monkeypatch.setattr(FeishuCardAPIClient, "_worker_slots", slots)
    monkeypatch.setattr(client, "_api_timeout_seconds", lambda: 0.01)

    with pytest.raises(TimeoutError):
        client._call_api("slow.first", lambda: release.wait(timeout=1.0))

    started_at = time.monotonic()
    with pytest.raises(TimeoutError, match="worker slots exhausted"):
        client._call_api("slow.second", lambda: "late")
    assert time.monotonic() - started_at < 0.5
    release.set()
