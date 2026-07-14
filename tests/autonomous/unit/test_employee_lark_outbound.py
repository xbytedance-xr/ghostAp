from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.autonomous.provisioning.lark_outbound import LarkEmployeeOutbound


class _Resource:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def create(self, request: object) -> object:
        self.requests.append(request)
        return SimpleNamespace(
            success=lambda: True,
            data=SimpleNamespace(message_id="om_employee", reply_id="reply_employee"),
        )

    def reply(self, request: object) -> object:
        return self.create(request)

    def patch(self, request: object) -> object:
        return self.create(request)


def _client() -> tuple[object, _Resource, _Resource]:
    messages = _Resource()
    comments = _Resource()
    client = SimpleNamespace(
        im=SimpleNamespace(v1=SimpleNamespace(message=messages)),
        drive=SimpleNamespace(v1=SimpleNamespace(file_comment_reply=comments)),
    )
    return client, messages, comments


def test_employee_outbound_sends_card_with_stable_uuid() -> None:
    client, messages, _comments = _client()
    outbound = LarkEmployeeOutbound(client)

    result = outbound.send(
        "oc_team",
        {"card": {"schema": "2.0"}},
        {"uuid": "fixed-uuid"},
    )

    assert result.message_id == "om_employee"
    request = messages.requests[0]
    assert request.receive_id_type == "chat_id"
    assert request.request_body.receive_id == "oc_team"
    assert request.request_body.msg_type == "interactive"
    assert request.request_body.uuid == "fixed-uuid"


def test_employee_outbound_replies_from_employee_bot() -> None:
    client, messages, _comments = _client()
    outbound = LarkEmployeeOutbound(client)

    outbound.send(
        "ou_requester",
        {"text": "ready"},
        {"reply_to": "om_status", "reply_in_thread": True},
    )

    request = messages.requests[0]
    assert request.message_id == "om_status"
    assert request.request_body.msg_type == "text"
    assert request.request_body.reply_in_thread is True


def test_employee_outbound_allows_multiline_agent_text() -> None:
    client, messages, _comments = _client()

    LarkEmployeeOutbound(client).send("oc_team", {"text": "first\nsecond"})

    assert "first\\nsecond" in messages.requests[0].request_body.content


def test_employee_outbound_supports_rich_post_mentions() -> None:
    client, messages, _comments = _client()
    outbound = LarkEmployeeOutbound(client)
    post = {
        "zh_cn": {
            "title": "handoff",
            "content": [[{"tag": "at", "user_id": "ou_peer"}]],
        }
    }

    outbound.send("oc_team", {"post": post})

    request = messages.requests[0]
    assert request.request_body.msg_type == "post"
    assert "ou_peer" in request.request_body.content


def test_employee_outbound_updates_existing_card() -> None:
    client, messages, _comments = _client()
    outbound = LarkEmployeeOutbound(client)

    result = outbound.update_card("om_card", {"schema": "2.0"})

    assert result.message_id == "om_card"
    request = messages.requests[0]
    assert request.message_id == "om_card"


def test_employee_outbound_replies_to_document_comment() -> None:
    client, _messages, comments = _client()
    outbound = LarkEmployeeOutbound(client)

    result = outbound.send(
        "doc-comment",
        {"text": "review complete"},
        {
            "comment_reply": {
                "file_token": "doccn_token",
                "file_type": "docx",
                "comment_id": "12345",
            }
        },
    )

    assert result.message_id == "reply_employee"
    request = comments.requests[0]
    assert request.file_token == "doccn_token"
    assert request.file_type == "docx"
    assert request.comment_id == "12345"


@pytest.mark.parametrize(
    ("message", "options"),
    [
        ({"text": "ok", "card": {}}, None),
        ({"post": []}, None),
        ({"text": "ok"}, {"unknown": True}),
        ({"text": "ok"}, {"comment_reply": {"file_token": "bad"}}),
    ],
)
def test_employee_outbound_rejects_ambiguous_or_unsafe_payloads(
    message: object,
    options: object,
) -> None:
    client, _messages, _comments = _client()
    with pytest.raises(ValueError):
        LarkEmployeeOutbound(client).send("oc_team", message, options)
