"""Official one-click Feishu/Lark employee app registration adapter."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import lark_oapi as lark


class AppRegistrationError(RuntimeError):
    """One-click registration failed without exposing credential material."""


_REQUIRED_PARAMETERS = frozenset({"app_preset", "addons", "create_only", "app_id"})

_TENANT_SCOPES = (
    "application:application:self_manage",
    "application:bot.basic_info:read",
    "application:app_slash_command:read",
    "application:app_slash_command:write",
    "cardkit:card:read",
    "cardkit:card:write",
    "im:chat.members:bot_access",
    "im:chat:read",
    "im:message.group_at_msg:readonly",
    "im:message.group_at_msg.include_bot:readonly",
    "im:message.p2p_msg:readonly",
    "im:message:readonly",
    "im:message:send_as_bot",
    "im:message:update",
    "im:resource",
)
_TENANT_EVENTS = (
    "im.message.receive_v1",
    "im.chat.member.bot.added_v1",
    "im.chat.member.bot.deleted_v1",
)
_CALLBACKS = ("card.action.trigger",)


def _strict_text(value: str, field_name: str, *, max_length: int = 200) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field_name} must be non-empty and at most {max_length} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise ValueError(f"{field_name} contains control characters")
    return normalized


@dataclass(frozen=True)
class RegistrationRequest:
    name: str
    description: str
    avatar_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _strict_text(self.name, "name", max_length=80))
        object.__setattr__(
            self,
            "description",
            _strict_text(self.description, "description", max_length=200),
        )
        avatars = tuple(self.avatar_urls)
        object.__setattr__(self, "avatar_urls", avatars)
        if len(avatars) > 6:
            raise ValueError("avatar_urls must contain at most 6 entries")
        for url in avatars:
            if (
                not isinstance(url, str)
                or not url.startswith("https://")
                or len(url) > 2048
                or any(ord(char) < 32 or ord(char) == 127 for char in url)
            ):
                raise ValueError("avatar_urls must use https")


@dataclass(frozen=True, repr=False)
class RegistrationResult:
    app_id: str
    app_secret: str


RegistrationFunction = Callable[..., Awaitable[dict[str, Any]]]


class LarkAppRegistrar:
    """Wrap ``lark_oapi.aregister_app`` behind a strict, secret-safe port."""

    def __init__(self, register_fn: RegistrationFunction | None = None) -> None:
        self._register = register_fn or lark.aregister_app

    @staticmethod
    def assert_sdk_capability() -> None:
        for name in ("register_app", "aregister_app"):
            parameters = set(inspect.signature(getattr(lark, name)).parameters)
            missing = sorted(_REQUIRED_PARAMETERS - parameters)
            if missing:
                raise AppRegistrationError(
                    f"lark-oapi registration capability unavailable: {name} missing {','.join(missing)}"
                )

    async def register(
        self,
        request: RegistrationRequest,
        *,
        on_link: Callable[[str, int], None],
        on_status: Callable[[str], None] | None = None,
    ) -> RegistrationResult:
        self.assert_sdk_capability()

        def handle_qr_code(info: Any) -> None:
            if not isinstance(info, dict):
                raise AppRegistrationError("registration link payload is invalid")
            url = info.get("url")
            expire_in = info.get("expire_in")
            if (
                not isinstance(url, str)
                or not url.startswith("https://")
                or isinstance(expire_in, bool)
                or not isinstance(expire_in, int)
                or expire_in <= 0
            ):
                raise AppRegistrationError("registration link payload is invalid")
            on_link(url, expire_in)

        def handle_status(info: Any) -> None:
            if on_status is None or not isinstance(info, dict):
                return
            status = info.get("status")
            if isinstance(status, str) and status:
                on_status(status)

        app_preset: dict[str, Any] = {
            "name": request.name,
            "desc": request.description,
        }
        if request.avatar_urls:
            app_preset["avatar"] = list(request.avatar_urls)
        try:
            raw = await self._register(
                on_qr_code=handle_qr_code,
                on_status_change=handle_status,
                source="ghostap",
                app_preset=app_preset,
                addons={
                    "preset": False,
                    "scopes": {"tenant": list(_TENANT_SCOPES)},
                    "events": {"items": {"tenant": list(_TENANT_EVENTS)}},
                    "callbacks": {"items": list(_CALLBACKS)},
                },
                create_only=True,
            )
        except AppRegistrationError:
            raise
        except Exception as exc:
            raise AppRegistrationError(
                f"one-click registration failed ({type(exc).__name__})"
            ) from None
        if not isinstance(raw, dict):
            raise AppRegistrationError("one-click registration returned incomplete credentials")
        app_id = raw.get("client_id")
        app_secret = raw.get("client_secret")
        if not isinstance(app_id, str) or re.fullmatch(
            r"cli_[A-Za-z0-9_-]{3,128}", app_id
        ) is None:
            raise AppRegistrationError("one-click registration returned incomplete credentials")
        if (
            not isinstance(app_secret, str)
            or not 8 <= len(app_secret) <= 512
            or app_secret.strip() != app_secret
            or any(ord(char) < 32 or ord(char) == 127 for char in app_secret)
        ):
            raise AppRegistrationError("one-click registration returned incomplete credentials")
        return RegistrationResult(app_id=app_id, app_secret=app_secret)


__all__ = [
    "AppRegistrationError",
    "LarkAppRegistrar",
    "RegistrationRequest",
    "RegistrationResult",
]
