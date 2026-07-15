"""Contract tests for the official lark-oapi Slash Command adapter."""

from __future__ import annotations

import json

import pytest
from lark_oapi.api.application.v7 import (
    AppSlashCommand,
    AppSlashCommandI18nText,
)
from lark_oapi.core import JSON
from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.model.base_response import BaseResponse
from lark_oapi.core.model.raw_response import RawResponse

from src.autonomous.provisioning.slash_commands import (
    CanonicalSlashCommand,
    SlashCommandAPIError,
)
from src.autonomous.provisioning.slash_lark import LarkSlashCommandAPI


def _response(payload: object, *, code: int = 0) -> BaseResponse:
    response = BaseResponse()
    response.code = code
    response.msg = "tenant-access-token=super-secret"
    raw = RawResponse()
    raw.status_code = 200
    raw.headers = {"Content-Type": "application/json"}
    raw.content = json.dumps(payload).encode()
    response.raw = raw
    return response


class _RecordingClient:
    def __init__(self, responses: list[BaseResponse]) -> None:
        self.responses = responses
        self.requests = []

    async def arequest(self, request):
        self.requests.append(request)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_adapter_uses_v7_base_requests_tenant_token_and_official_models() -> None:
    client = _RecordingClient(
        [
            _response(
                {
                    "code": 0,
                    "msg": "success",
                    "data": {
                        "items": [
                            {
                                "command_id": "cmd_task",
                                "command": "task",
                                "description": {
                                    "default_value": "Assign a task",
                                    "i18n": {},
                                },
                            }
                        ]
                    },
                }
            ),
            _response(
                {
                    "code": 0,
                    "msg": "success",
                    "data": {"command_id": "cmd_new"},
                }
            ),
            _response({"code": 0, "msg": "success", "data": {}}),
            _response({"code": 0, "msg": "success", "data": {}}),
        ]
    )
    api = LarkSlashCommandAPI(client)
    command = CanonicalSlashCommand(command="task", description="Assign a task")

    observed = await api.list_commands()
    command_id = await api.create_command(command)
    await api.update_command("cmd_task", command)
    await api.delete_command("cmd_task")

    assert observed[0].command_id == "cmd_task"
    assert observed[0].command == "task"
    assert observed[0].description == "Assign a task"
    assert command_id == "cmd_new"
    assert [request.http_method for request in client.requests] == [
        HttpMethod.GET,
        HttpMethod.POST,
        HttpMethod.PATCH,
        HttpMethod.DELETE,
    ]
    assert all(request.token_types == {AccessTokenType.TENANT} for request in client.requests)
    collection_uri = "/open-apis/application/v7/app_slash_commands"
    item_uri = f"{collection_uri}/:command_id"
    assert [request.uri for request in client.requests] == [
        collection_uri,
        collection_uri,
        item_uri,
        item_uri,
    ]
    assert client.requests[2].paths == {"command_id": "cmd_task"}
    assert client.requests[3].paths == {"command_id": "cmd_task"}
    for request in client.requests[1:3]:
        assert isinstance(request.body, AppSlashCommand)
        assert request.body.command == "task"
        assert isinstance(request.body.description, AppSlashCommandI18nText)
        assert request.body.description.default_value == "Assign a task"
        encoded = json.loads(JSON.marshal(request.body))
        assert encoded["description"] == {"default_value": "Assign a task", "i18n": {}}
        assert "usage_hint" not in encoded
    assert client.requests[0].body is None
    assert client.requests[3].body is None


@pytest.mark.asyncio
async def test_list_accepts_server_omitting_optional_description_i18n() -> None:
    api = LarkSlashCommandAPI(
        _RecordingClient(
            [
                _response(
                    {
                        "code": 0,
                        "msg": "success",
                        "data": {
                            "items": [
                                {
                                    "command_id": "cmd_task",
                                    "command": "task",
                                    "description": {"default_value": "Assign a task"},
                                }
                            ]
                        },
                    }
                )
            ]
        )
    )

    observed = await api.list_commands()

    assert observed[0].description == "Assign a task"
    assert observed[0].description_i18n == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0, "msg": "success", "data": {}},
        {"code": 0, "msg": "success", "data": {"items": "not-a-list"}},
        {
            "code": 0,
            "msg": "success",
            "data": {
                "items": [
                    {
                        "command_id": "cmd_task",
                        "command": "/task",
                        "description": {"default_value": "Assign", "i18n": {}},
                    }
                ]
            },
        },
        {
            "code": 0,
            "msg": "success",
            "data": {
                "items": [
                    {
                        "command_id": "cmd_task",
                        "command": "task",
                        "description": "Assign",
                    }
                ]
            },
        },
        {
            "code": 0,
            "msg": "success",
            "data": {
                "items": [
                    {
                        "command_id": "cmd_task",
                        "command": "task",
                        "description": {
                            "default_value": "Assign",
                            "i18n": {},
                            "usage_hint": "/task",
                        },
                    }
                ]
            },
        },
    ],
)
async def test_list_rejects_malformed_response_shapes(payload: dict) -> None:
    api = LarkSlashCommandAPI(_RecordingClient([_response(payload)]))

    with pytest.raises(SlashCommandAPIError, match="response schema is invalid"):
        await api.list_commands()


@pytest.mark.asyncio
async def test_sdk_failures_are_redacted() -> None:
    api = LarkSlashCommandAPI(
        _RecordingClient(
            [
                _response(
                    {
                        "code": 999,
                        "msg": "tenant-access-token=super-secret",
                    },
                    code=999,
                )
            ]
        )
    )

    with pytest.raises(SlashCommandAPIError) as exc_info:
        await api.list_commands()

    assert "999" in str(exc_info.value)
    assert "super-secret" not in str(exc_info.value)
