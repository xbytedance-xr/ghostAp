from __future__ import annotations

from dataclasses import replace

import pytest

from src.autonomous.provisioning import lark_app


@pytest.mark.asyncio
async def test_registration_addons_and_fingerprint_share_one_manifest() -> None:
    captured: dict = {}

    async def register(**kwargs):
        captured.update(kwargs)
        return {"client_id": "cli_employee", "client_secret": "secret-value"}

    manifest = lark_app.current_registration_manifest()
    await lark_app.LarkAppRegistrar(register_fn=register).register(
        lark_app.RegistrationRequest(name="Atlas", description="GhostAP employee"),
        on_link=lambda _url, _ttl: None,
    )

    assert captured["addons"] == manifest.addons()
    assert manifest.fingerprint().startswith("sha256:")


@pytest.mark.asyncio
async def test_existing_app_result_carries_official_manifest_receipt() -> None:
    async def register(**_kwargs):
        return {"client_id": "cli_employee", "client_secret": "secret-value"}

    result = await lark_app.LarkAppRegistrar(register_fn=register).register(
        lark_app.RegistrationRequest(
            name="Atlas",
            description="GhostAP employee",
            existing_app_id="cli_employee",
        ),
        on_link=lambda _url, _ttl: None,
    )

    assert result.manifest_hash == (
        lark_app.current_registration_manifest().fingerprint()
    )
    assert result.evidence_source == lark_app.MANIFEST_EVIDENCE_SOURCE


def test_manifest_fingerprint_is_order_stable_and_scope_sensitive() -> None:
    manifest = lark_app.current_registration_manifest()
    reordered = lark_app.RegistrationManifest(
        schema_version=manifest.schema_version,
        preset=manifest.preset,
        tenant_scopes=tuple(reversed(manifest.tenant_scopes)),
        user_scopes=tuple(reversed(manifest.user_scopes)),
        tenant_events=tuple(reversed(manifest.tenant_events)),
        callbacks=tuple(reversed(manifest.callbacks)),
    )

    assert reordered.fingerprint() == manifest.fingerprint()
    assert replace(
        reordered,
        tenant_scopes=reordered.tenant_scopes + ("test:scope",),
    ).fingerprint() != manifest.fingerprint()


@pytest.mark.parametrize(
    ("field_name", "bare_value"),
    [
        ("tenant_scopes", "scope:value"),
        ("tenant_scopes", b"scope:value"),
        ("user_scopes", "offline_access"),
        ("user_scopes", b"offline_access"),
        ("tenant_events", "im.message.receive_v1"),
        ("tenant_events", b"im.message.receive_v1"),
        ("callbacks", "card.action.trigger"),
        ("callbacks", b"card.action.trigger"),
    ],
)
def test_manifest_rejects_bare_string_or_bytes_tuple_fields(
    field_name: str,
    bare_value: str | bytes,
) -> None:
    with pytest.raises(ValueError, match=field_name):
        lark_app.RegistrationManifest(**{field_name: bare_value})  # type: ignore[arg-type]
