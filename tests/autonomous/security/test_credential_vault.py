import base64
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from src.autonomous.workforce.credential_vault import (
    CredentialKeyring,
    CredentialVault,
    CredentialVaultConfigurationError,
    CredentialVaultError,
)


def _key(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode()


def _settings(payload: str, active_key_id: str = "k1") -> SimpleNamespace:
    return SimpleNamespace(
        autonomous_credential_keys=SecretStr(payload),
        autonomous_credential_active_key_id=active_key_id,
    )


def _vault(tmp_path, *, key_id: str = "k1", byte: int = 1) -> CredentialVault:
    return CredentialVault(
        tmp_path / "credentials",
        CredentialKeyring(keys={key_id: _key(byte)}, active_key_id=key_id),
    )


def _put(vault: CredentialVault, *, secret: str = "secret"):
    return vault.put(
        agent_id="agt_1",
        app_id="cli_1",
        app_secret=secret,
        hire_intent_id="hire_1",
        attempt_id="attempt_1",
    )


def test_keyring_parses_versioned_rotation_set_and_rejects_missing_active() -> None:
    settings = _settings(
        json.dumps({"version": 1, "keys": {"old": _key(1), "new": _key(2)}}),
        active_key_id="new",
    )
    assert CredentialKeyring.from_settings(settings).active_key_id == "new"
    settings.autonomous_credential_active_key_id = "absent"
    with pytest.raises(CredentialVaultConfigurationError):
        CredentialKeyring.from_settings(settings)


@pytest.mark.parametrize(
    "payload,active_key_id",
    [
        ("", ""),
        (json.dumps({"version": 1, "keys": {"k1": _key(1)}, "extra": True}), "k1"),
        (json.dumps({"version": 2, "keys": {"k1": _key(1)}}), "k1"),
        ('{"version":1,"keys":{"k1":"%s","k1":"%s"}}' % (_key(1), _key(2)), "k1"),
        (json.dumps({"version": 1, "keys": {"k1": "not-base64!"}}), "k1"),
        (json.dumps({"version": 1, "keys": {"k1": base64.urlsafe_b64encode(b"short").decode()}}), "k1"),
        (json.dumps({"version": 1, "keys": {}}), "k1"),
    ],
)
def test_keyring_rejects_malformed_or_unsafe_settings_without_disclosure(
    payload: str,
    active_key_id: str,
) -> None:
    with pytest.raises(CredentialVaultConfigurationError) as raised:
        CredentialKeyring.from_settings(_settings(payload, active_key_id))

    if payload:
        assert payload not in str(raised.value)
    assert _key(1) not in str(raised.value)
    assert str(raised.value) == "CredentialVaultConfigurationError"


def test_vault_encrypts_secret_and_enforces_modes(tmp_path) -> None:
    vault = _vault(tmp_path)
    receipt = _put(vault, secret="super-secret")

    raw = receipt.path.read_bytes()
    assert b"super-secret" not in raw
    assert os.stat(receipt.path).st_mode & 0o777 == 0o600
    assert os.stat(receipt.path.parent).st_mode & 0o777 == 0o700
    assert vault.resolve(receipt.credential_ref, agent_id="agt_1", app_id="cli_1") == "super-secret"


def test_vault_writes_exact_envelope_and_non_secret_deterministic_ref(tmp_path) -> None:
    vault = _vault(tmp_path)
    receipt = _put(vault, secret="first-secret")
    envelope = json.loads(receipt.path.read_text())

    assert set(envelope) == {
        "schema_version",
        "credential_ref",
        "key_id",
        "agent_id",
        "app_id",
        "hire_intent_id",
        "attempt_id",
        "nonce",
        "ciphertext",
        "ciphertext_sha256",
        "created_at",
    }
    expected_ref = "cred_" + hashlib.sha256(b"hire_1|attempt_1").hexdigest()
    assert receipt.credential_ref == expected_ref
    assert envelope["credential_ref"] == expected_ref
    assert envelope["ciphertext_sha256"] == hashlib.sha256(
        base64.urlsafe_b64decode(envelope["ciphertext"])
    ).hexdigest()


def test_vault_rejects_wrong_associated_identity(tmp_path) -> None:
    vault = _vault(tmp_path)
    receipt = _put(vault)

    with pytest.raises(CredentialVaultError) as raised:
        vault.resolve(receipt.credential_ref, agent_id="agt_2", app_id="cli_1")

    assert str(raised.value) == f"CredentialVaultError:{receipt.credential_ref}"


def test_vault_rejects_tampered_ciphertext_without_disclosure(tmp_path) -> None:
    vault = _vault(tmp_path)
    receipt = _put(vault, secret="do-not-disclose")
    envelope = json.loads(receipt.path.read_text())
    ciphertext = bytearray(base64.urlsafe_b64decode(envelope["ciphertext"]))
    ciphertext[-1] ^= 1
    envelope["ciphertext"] = base64.urlsafe_b64encode(ciphertext).decode()
    receipt.path.write_text(json.dumps(envelope))

    with pytest.raises(CredentialVaultError) as raised:
        vault.resolve(receipt.credential_ref, agent_id="agt_1", app_id="cli_1")

    assert str(raised.value) == f"CredentialVaultError:{receipt.credential_ref}"
    assert "do-not-disclose" not in str(raised.value)
    assert _key(1) not in str(raised.value)


def test_vault_finds_orphan_receipt_and_rewraps_to_active_key(tmp_path) -> None:
    root = tmp_path / "credentials"
    old = CredentialVault(root, CredentialKeyring(keys={"old": _key(1)}, active_key_id="old"))
    receipt = _put(old)
    rotated = CredentialVault(
        root,
        CredentialKeyring(keys={"old": _key(1), "new": _key(2)}, active_key_id="new"),
    )

    assert [r.credential_ref for r in rotated.find_orphan_receipts(set())] == [receipt.credential_ref]
    assert rotated.find_orphan_receipts({receipt.credential_ref}) == []
    rotated.rewrap(receipt.credential_ref, agent_id="agt_1", app_id="cli_1")
    envelope = json.loads(receipt.path.read_text())
    assert envelope["key_id"] == "new"
    assert rotated.resolve(receipt.credential_ref, agent_id="agt_1", app_id="cli_1") == "secret"


def test_destroy_is_idempotent_and_removes_secret(tmp_path) -> None:
    vault = _vault(tmp_path)
    receipt = _put(vault)

    assert vault.destroy(receipt.credential_ref) is True
    assert vault.destroy(receipt.credential_ref) is False
    assert not receipt.path.exists()


def test_vault_rejects_credential_ref_path_traversal(tmp_path) -> None:
    vault = _vault(tmp_path)

    with pytest.raises(CredentialVaultError) as raised:
        vault.resolve("../outside", agent_id="agt_1", app_id="cli_1")

    assert str(raised.value) == "CredentialVaultError:../outside"
