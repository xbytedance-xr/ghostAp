from __future__ import annotations

import pytest

from src.autonomous.provisioning.lark_app import (
    AppRegistrationError,
    LarkAppRegistrar,
    RegistrationRequest,
)


@pytest.mark.asyncio
async def test_registrar_uses_minimal_manifest_and_forwards_callbacks() -> None:
    captured: dict = {}

    async def register(**kwargs):
        captured.update(kwargs)
        kwargs["on_qr_code"]({"url": "https://accounts.feishu.cn/device", "expire_in": 600})
        kwargs["on_status_change"]({"status": "polling"})
        return {"client_id": "cli_employee", "client_secret": "secret-value"}

    links: list[tuple[str, int]] = []
    statuses: list[str] = []
    registrar = LarkAppRegistrar(register_fn=register)

    result = await registrar.register(
        RegistrationRequest(name="Atlas", description="GhostAP employee"),
        on_link=lambda url, ttl: links.append((url, ttl)),
        on_status=statuses.append,
    )

    assert result.app_id == "cli_employee"
    assert result.app_secret == "secret-value"
    assert links == [("https://accounts.feishu.cn/device", 600)]
    assert statuses == ["polling"]
    assert captured["create_only"] is True
    assert captured["source"] == "ghostap"
    assert captured["app_preset"] == {
        "name": "Atlas",
        "desc": "GhostAP employee",
    }
    assert captured["addons"] == {
        "preset": False,
        "scopes": {
            "tenant": [
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
            ]
        },
        "events": {
            "items": {
                "tenant": [
                    "im.message.receive_v1",
                    "im.chat.member.bot.added_v1",
                    "im.chat.member.bot.deleted_v1",
                ]
            }
        },
        "callbacks": {"items": ["card.action.trigger"]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [None, {}, {"client_id": "cli_only"}, {"client_secret": "secret-only"}],
)
async def test_registrar_rejects_incomplete_credentials_without_disclosure(result) -> None:
    async def register(**_kwargs):
        return result

    registrar = LarkAppRegistrar(register_fn=register)
    with pytest.raises(AppRegistrationError, match="incomplete credentials") as exc_info:
        await registrar.register(
            RegistrationRequest(name="Atlas", description="GhostAP employee"),
            on_link=lambda _url, _ttl: None,
        )
    assert "secret-only" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("app_id", "app_secret"),
    [
        ("cli_", "secret-value"),
        ("cli_okay", "   "),
        ("cli_okay", "bad\nsecret"),
        ("cli_okay", "x" * 513),
    ],
)
async def test_registrar_rejects_malformed_credentials(app_id, app_secret) -> None:
    async def register(**_kwargs):
        return {"client_id": app_id, "client_secret": app_secret}

    with pytest.raises(AppRegistrationError, match="incomplete credentials"):
        await LarkAppRegistrar(register_fn=register).register(
            RegistrationRequest(name="Atlas", description="GhostAP employee"),
            on_link=lambda _url, _ttl: None,
        )


def test_registration_request_rejects_blank_or_control_characters() -> None:
    with pytest.raises(ValueError, match="name"):
        RegistrationRequest(name="", description="employee")
    with pytest.raises(ValueError, match="description"):
        RegistrationRequest(name="Atlas", description="bad\ntext")


def test_registration_request_freezes_and_validates_avatar_urls() -> None:
    avatars = ["https://example.com/avatar.png"]
    request = RegistrationRequest(
        name="Atlas",
        description="employee",
        avatar_urls=avatars,  # type: ignore[arg-type]
    )
    avatars.append("https://example.com/changed.png")
    assert request.avatar_urls == ("https://example.com/avatar.png",)
    with pytest.raises(ValueError, match="avatar_urls"):
        RegistrationRequest(
            name="Atlas",
            description="employee",
            avatar_urls=("https://example.com/bad\nname.png",),
        )
