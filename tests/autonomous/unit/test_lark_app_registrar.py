from __future__ import annotations

import logging

import pytest

from src.autonomous.provisioning.lark_app import (
    AppRegistrationError,
    LarkAppRegistrar,
    RegistrationRequest,
)


@pytest.mark.asyncio
async def test_registrar_uses_official_agent_manifest_and_forwards_callbacks() -> None:
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
    assert captured["create_only"] is False
    assert captured["source"] == "ghostap"
    assert captured["app_preset"] == {
        "name": "Atlas",
        "desc": "GhostAP employee",
    }
    assert captured["addons"] == {
        "preset": True,
        "scopes": {
            "tenant": [
                "application:application:self_manage",
                "application:bot.basic_info:read",
                "application:bot.menu:write",
                "application:app_slash_command:read",
                "application:app_slash_command:write",
                "cardkit:card:read",
                "cardkit:card:write",
                "contact:contact.base:readonly",
                "docs:document.comment:create",
                "docs:document.comment:delete",
                "docs:document.comment:read",
                "docs:document.comment:update",
                "docs:document.comment:write_only",
                "docx:document.block:convert",
                "docx:document:create",
                "docx:document:readonly",
                "docx:document:write_only",
                "drive:drive.metadata:readonly",
                "im:chat.members:bot_access",
                "im:chat:create",
                "im:chat.members:read",
                "im:chat:update",
                "im:message.group_at_msg:readonly",
                "im:message.group_at_msg.include_bot:readonly",
                "im:message.group_msg",
                "im:message.p2p_msg:readonly",
                "im:message.pins:read",
                "im:message.pins:write_only",
                "im:message.reactions:read",
                "im:message.reactions:write_only",
                "im:message:readonly",
                "im:message:send_as_bot",
                "im:message:send_multi_users",
                "im:message:send_sys_msg",
                "im:message:update",
                "im:resource",
                "wiki:node:read",
            ],
            "user": ["offline_access"],
        },
        "events": {
            "items": {
                "tenant": [
                    "im.message.receive_v1",
                    "im.message.reaction.created_v1",
                    "im.message.reaction.deleted_v1",
                    "im.chat.member.bot.added_v1",
                    "im.chat.member.bot.deleted_v1",
                    "drive.notice.comment_add_v1",
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


@pytest.mark.asyncio
async def test_existing_app_registration_rejects_mismatched_client_id() -> None:
    async def register(**_kwargs):
        return {
            "client_id": "cli_different_app",
            "client_secret": "secret-value",
        }

    with pytest.raises(AppRegistrationError, match="existing app identity mismatch"):
        await LarkAppRegistrar(register_fn=register).register(
            RegistrationRequest(
                name="Atlas",
                description="GhostAP employee",
                existing_app_id="cli_existing_123",
            ),
            on_link=lambda _url, _ttl: None,
        )


@pytest.mark.asyncio
async def test_existing_app_registration_updates_full_required_manifest() -> None:
    captured: dict = {}

    async def register(**kwargs):
        captured.update(kwargs)
        return {
            "client_id": "cli_existing_123",
            "client_secret": "secret-value",
        }

    result = await LarkAppRegistrar(register_fn=register).register(
        RegistrationRequest(
            name="Atlas",
            description="GhostAP employee",
            existing_app_id="cli_existing_123",
        ),
        on_link=lambda _url, _ttl: None,
    )

    assert result.app_id == "cli_existing_123"
    assert captured["app_id"] == "cli_existing_123"
    assert "create_only" not in captured
    assert "im:message.group_msg" in captured["addons"]["scopes"]["tenant"]


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


@pytest.mark.asyncio
async def test_registrar_suppresses_only_registration_400_transport_noise(caplog) -> None:
    registration_400 = (
        'HTTP Request: POST https://accounts.feishu.cn/oauth/v1/app/registration '
        '"HTTP/1.1 400 Bad Request"'
    )
    registration_200 = (
        'HTTP Request: POST https://accounts.feishu.cn/oauth/v1/app/registration '
        '"HTTP/1.1 200 OK"'
    )
    unrelated_400 = (
        'HTTP Request: POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal '
        '"HTTP/1.1 400 Bad Request"'
    )

    async def register(**_kwargs):
        httpx_logger = logging.getLogger("httpx")
        httpx_logger.info(registration_400)
        httpx_logger.info(registration_200)
        httpx_logger.info(unrelated_400)
        return {"client_id": "cli_employee", "client_secret": "secret-value"}

    with caplog.at_level(logging.INFO, logger="httpx"):
        await LarkAppRegistrar(register_fn=register).register(
            RegistrationRequest(name="Atlas", description="GhostAP employee"),
            on_link=lambda _url, _ttl: None,
        )

    messages = [record.getMessage() for record in caplog.records]
    assert registration_400 not in messages
    assert registration_200 in messages
    assert unrelated_400 in messages


@pytest.mark.asyncio
async def test_registrar_reports_each_registration_status_once() -> None:
    async def register(**kwargs):
        kwargs["on_status_change"]({"status": "polling"})
        kwargs["on_status_change"]({"status": "polling"})
        kwargs["on_status_change"]({"status": "slow_down"})
        kwargs["on_status_change"]({"status": "slow_down"})
        kwargs["on_status_change"]({"status": "polling"})
        return {"client_id": "cli_employee", "client_secret": "secret-value"}

    statuses: list[str] = []
    await LarkAppRegistrar(register_fn=register).register(
        RegistrationRequest(name="Atlas", description="GhostAP employee"),
        on_link=lambda _url, _ttl: None,
        on_status=statuses.append,
    )

    assert statuses == ["polling", "slow_down"]


@pytest.mark.asyncio
async def test_registrar_detaches_poll_noise_filter_even_when_registration_fails() -> None:
    from src.autonomous.provisioning import lark_app

    httpx_logger = logging.getLogger("httpx")

    async def register(**_kwargs):
        assert lark_app._poll_noise_filter in httpx_logger.filters
        raise RuntimeError("registration transport failed")

    with pytest.raises(AppRegistrationError):
        await LarkAppRegistrar(register_fn=register).register(
            RegistrationRequest(name="Atlas", description="GhostAP employee"),
            on_link=lambda _url, _ttl: None,
        )

    assert lark_app._poll_noise_filter not in httpx_logger.filters
    assert lark_app._poll_noise_refcount == 0
