"""Official lark-oapi adapter for employee-owned Slash Commands."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from lark_oapi.api.application.v7 import (
    AppSlashCommand,
    AppSlashCommandI18n,
    AppSlashCommandI18nText,
)
from lark_oapi.core.enum import AccessTokenType, HttpMethod
from lark_oapi.core.model.base_request import BaseRequest
from lark_oapi.core.model.base_response import BaseResponse

from .slash_commands import (
    CanonicalSlashCommand,
    ObservedSlashCommand,
    SlashCommandAPIError,
)

_COLLECTION_URI = "/open-apis/application/v7/app_slash_commands"
_ITEM_URI = f"{_COLLECTION_URI}/:command_id"
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class _AsyncLarkClient(Protocol):
    async def arequest(self, request: BaseRequest) -> BaseResponse: ...


class LarkSlashCommandAPI:
    """Call Slash v7 through ``Client.arequest(BaseRequest)`` with tenant auth."""

    def __init__(self, client: _AsyncLarkClient) -> None:
        self._client = client

    async def list_commands(self) -> tuple[ObservedSlashCommand, ...]:
        """Return a strictly decoded full server command set."""
        payload = await self._request(HttpMethod.GET, _COLLECTION_URI, operation="GET")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise SlashCommandAPIError("Slash GET response schema is invalid")
        commands: list[ObservedSlashCommand] = []
        try:
            for item in data["items"]:
                if not isinstance(item, dict) or not set(item) <= set(AppSlashCommand._types):
                    raise ValueError
                command_id = item.get("command_id")
                command = item.get("command")
                description = item.get("description")
                if (
                    not isinstance(command_id, str)
                    or _SAFE_ID.fullmatch(command_id) is None
                    or not isinstance(command, str)
                    or not isinstance(description, dict)
                    or not set(description) <= set(AppSlashCommandI18nText._types)
                    or "default_value" not in description
                ):
                    raise ValueError
                default_value = description.get("default_value")
                i18n = description.get("i18n", {})
                if not isinstance(default_value, str) or not isinstance(i18n, dict):
                    raise ValueError
                if not set(i18n) <= set(AppSlashCommandI18n._types):
                    raise ValueError
                localized: list[tuple[str, str]] = []
                for locale, text in i18n.items():
                    if not isinstance(locale, str) or not isinstance(text, str):
                        raise ValueError
                    localized.append((locale, text))
                commands.append(
                    ObservedSlashCommand(
                        command_id=command_id,
                        command=command,
                        description=default_value,
                        description_i18n=tuple(localized),
                    )
                )
        except (TypeError, ValueError):
            raise SlashCommandAPIError("Slash GET response schema is invalid") from None
        return tuple(commands)

    async def create_command(self, command: CanonicalSlashCommand) -> str:
        """Create one canonical command and return its server ID."""
        payload = await self._request(
            HttpMethod.POST,
            _COLLECTION_URI,
            operation="POST",
            body=self._body(command),
        )
        data = payload.get("data")
        command_id = data.get("command_id") if isinstance(data, dict) else None
        if not isinstance(command_id, str) or _SAFE_ID.fullmatch(command_id) is None:
            raise SlashCommandAPIError("Slash POST response schema is invalid")
        return command_id

    async def update_command(
        self,
        command_id: str,
        command: CanonicalSlashCommand,
    ) -> None:
        """Replace one command's server-visible fields."""
        await self._request(
            HttpMethod.PATCH,
            _ITEM_URI,
            operation="PATCH",
            paths={"command_id": self._command_id(command_id)},
            body=self._body(command),
        )

    async def delete_command(self, command_id: str) -> None:
        """Delete one command by its observed server ID."""
        await self._request(
            HttpMethod.DELETE,
            _ITEM_URI,
            operation="DELETE",
            paths={"command_id": self._command_id(command_id)},
        )

    @staticmethod
    def _command_id(command_id: str) -> str:
        if not isinstance(command_id, str) or _SAFE_ID.fullmatch(command_id) is None:
            raise SlashCommandAPIError("Slash command ID is invalid")
        return command_id

    @staticmethod
    def _body(command: CanonicalSlashCommand) -> AppSlashCommand:
        supported_locales = set(AppSlashCommandI18n._types)
        if any(locale not in supported_locales for locale, _ in command.description_i18n):
            raise SlashCommandAPIError("Slash description locale is invalid")
        i18n = AppSlashCommandI18n(dict(command.description_i18n))
        description = AppSlashCommandI18nText.builder().default_value(command.description).i18n(i18n).build()
        return AppSlashCommand.builder().command(command.command).description(description).build()

    async def _request(
        self,
        method: HttpMethod,
        uri: str,
        *,
        operation: str,
        paths: dict[str, str] | None = None,
        body: AppSlashCommand | None = None,
    ) -> dict[str, Any]:
        request = (
            BaseRequest.builder()
            .http_method(method)
            .uri(uri)
            .token_types({AccessTokenType.TENANT})
            .paths(paths or {})
            .body(body)
            .build()
        )
        try:
            response = await self._client.arequest(request)
        except Exception as exc:
            raise SlashCommandAPIError(f"Slash {operation} request failed ({type(exc).__name__})") from None
        if not isinstance(response, BaseResponse) or not response.success():
            code = response.code if isinstance(response, BaseResponse) else "invalid"
            raise SlashCommandAPIError(f"Slash {operation} failed (code={code})")
        raw = response.raw
        if (
            raw is None
            or not isinstance(raw.status_code, int)
            or not 200 <= raw.status_code < 300
            or not isinstance(raw.content, bytes)
        ):
            raise SlashCommandAPIError(f"Slash {operation} response schema is invalid")
        try:
            payload = json.loads(raw.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SlashCommandAPIError(f"Slash {operation} response schema is invalid") from None
        code = payload.get("code") if isinstance(payload, dict) else None
        if isinstance(code, bool) or not isinstance(code, int):
            raise SlashCommandAPIError(f"Slash {operation} response schema is invalid")
        if code != 0:
            raise SlashCommandAPIError(f"Slash {operation} failed (code={code})")
        if not isinstance(payload.get("data"), dict):
            raise SlashCommandAPIError(f"Slash {operation} response schema is invalid")
        return payload
