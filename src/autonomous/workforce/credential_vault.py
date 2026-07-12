"""Encrypted employee credential storage with explicit key rotation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import stat
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEYRING_FIELDS = frozenset({"version", "keys"})
_ENVELOPE_FIELDS = frozenset(
    {
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
)
_IDENTITY_FIELDS = (
    "credential_ref",
    "agent_id",
    "app_id",
    "hire_intent_id",
    "attempt_id",
)
_CREDENTIAL_REF_RE = re.compile(r"cred_[0-9a-f]{64}\Z")
_INVALID_INPUT_REF = "invalid-input"


class CredentialVaultConfigurationError(ValueError):
    """Raised when the credential keyring is absent or unsafe."""

    def __init__(self) -> None:
        super().__init__(type(self).__name__)


class CredentialVaultError(RuntimeError):
    """Raised when a credential operation cannot complete safely."""

    def __init__(self, credential_ref: str) -> None:
        super().__init__(f"{type(self).__name__}:{credential_ref}")


def _decode_key(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        decoded = value
    elif isinstance(value, str):
        try:
            decoded = base64.b64decode(value, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CredentialVaultConfigurationError() from exc
    else:
        raise CredentialVaultConfigurationError()
    if len(decoded) != 32:
        raise CredentialVaultConfigurationError()
    return decoded


def _reject_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CredentialVaultConfigurationError()
        result[key] = value
    return result


@dataclass(frozen=True)
class CredentialKeyring:
    """Validated rotation set containing one active AES-256 key."""

    keys: Mapping[str, str | bytes] = field(repr=False)
    active_key_id: str

    def __post_init__(self) -> None:
        try:
            decoded = {
                key_id: _decode_key(value)
                for key_id, value in self.keys.items()
                if isinstance(key_id, str) and key_id
            }
        except (AttributeError, CredentialVaultConfigurationError) as exc:
            raise CredentialVaultConfigurationError() from exc
        if len(decoded) != len(self.keys) or not decoded or self.active_key_id not in decoded:
            raise CredentialVaultConfigurationError()
        object.__setattr__(self, "keys", MappingProxyType(decoded))

    @classmethod
    def from_settings(cls, settings: Any) -> CredentialKeyring:
        """Build a strict keyring from redacted application settings."""
        try:
            secret = settings.autonomous_credential_keys.get_secret_value()
            active_key_id = settings.autonomous_credential_active_key_id
            if not secret or not active_key_id:
                raise CredentialVaultConfigurationError()
            payload = json.loads(secret, object_pairs_hook=_reject_duplicate_object)
            if not isinstance(payload, dict) or set(payload) != _KEYRING_FIELDS:
                raise CredentialVaultConfigurationError()
            if type(payload["version"]) is not int or payload["version"] != 1:
                raise CredentialVaultConfigurationError()
            keys = payload["keys"]
            if not isinstance(keys, dict):
                raise CredentialVaultConfigurationError()
            return cls(keys=keys, active_key_id=active_key_id)
        except CredentialVaultConfigurationError:
            raise
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CredentialVaultConfigurationError() from exc


@dataclass(frozen=True)
class CredentialReceipt:
    """Non-secret proof that a credential envelope was persisted.

    ``path`` is informational only. It reflects the configured root pathname
    and can become stale after that directory is renamed; Vault I/O remains
    anchored to the verified root directory descriptor and never trusts it.
    """

    credential_ref: str
    key_id: str
    agent_id: str
    app_id: str
    hire_intent_id: str
    attempt_id: str
    ciphertext_sha256: str
    path: Path


class CredentialVault:
    """AES-GCM credential vault backed by atomic mode-restricted files."""

    def __init__(self, root: str | Path, keyring: CredentialKeyring) -> None:
        self._root = Path(root).expanduser()
        self._keyring = keyring
        self._root_fd = self._open_root(self._root)
        self._root_finalizer = weakref.finalize(self, os.close, self._root_fd)

    def __enter__(self) -> CredentialVault:
        """Return this open Vault for context-managed use."""
        if not self._root_finalizer.alive:
            raise CredentialVaultConfigurationError()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the held root directory capability."""
        self.close()

    def close(self) -> None:
        """Idempotently release the held root directory descriptor."""
        if self._root_finalizer.alive:
            self._root_finalizer()
            self._root_fd = -1

    def put(
        self,
        agent_id: str,
        app_id: str,
        app_secret: str,
        hire_intent_id: str,
        attempt_id: str,
    ) -> CredentialReceipt:
        """Encrypt and durably store an application secret."""
        self._validate_put_values(
            agent_id=agent_id,
            app_id=app_id,
            app_secret=app_secret,
            hire_intent_id=hire_intent_id,
            attempt_id=attempt_id,
        )
        credential_ref = self._derive_ref(hire_intent_id, attempt_id)
        try:
            envelope = self._encrypt_envelope(
                credential_ref=credential_ref,
                key_id=self._keyring.active_key_id,
                agent_id=agent_id,
                app_id=app_id,
                app_secret=app_secret,
                hire_intent_id=hire_intent_id,
                attempt_id=attempt_id,
                created_at=datetime.now(UTC).isoformat(),
            )
            self._atomic_write(self._filename(credential_ref), envelope)
            return self._receipt(envelope)
        except Exception as exc:
            if isinstance(exc, CredentialVaultError):
                raise
            raise CredentialVaultError(credential_ref) from None

    def resolve(self, credential_ref: str, agent_id: str, app_id: str) -> str:
        """Decrypt a credential only for its authenticated employee identity."""
        try:
            envelope = self._read_envelope(credential_ref)
            key = self._keyring.keys[envelope["key_id"]]
            nonce = self._decode_envelope_bytes(envelope["nonce"])
            ciphertext = self._decode_envelope_bytes(envelope["ciphertext"])
            if len(nonce) != 12:
                raise ValueError
            if hashlib.sha256(ciphertext).hexdigest() != envelope["ciphertext_sha256"]:
                raise ValueError
            identity = {
                "credential_ref": credential_ref,
                "agent_id": agent_id,
                "app_id": app_id,
                "hire_intent_id": envelope["hire_intent_id"],
                "attempt_id": envelope["attempt_id"],
            }
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, self._associated_data(identity))
            return plaintext.decode("utf-8")
        except Exception:
            raise CredentialVaultError(credential_ref) from None

    def rewrap(self, credential_ref: str, agent_id: str, app_id: str) -> CredentialReceipt:
        """Re-encrypt a credential with the current active key."""
        try:
            current = self._read_envelope(credential_ref)
            secret = self.resolve(credential_ref, agent_id=agent_id, app_id=app_id)
            envelope = self._encrypt_envelope(
                credential_ref=credential_ref,
                key_id=self._keyring.active_key_id,
                agent_id=agent_id,
                app_id=app_id,
                app_secret=secret,
                hire_intent_id=current["hire_intent_id"],
                attempt_id=current["attempt_id"],
                created_at=current["created_at"],
            )
            self._atomic_write(self._filename(credential_ref), envelope)
            return self._receipt(envelope)
        except Exception:
            raise CredentialVaultError(credential_ref) from None

    def destroy(self, credential_ref: str) -> bool:
        """Idempotently remove a credential and durably record the deletion."""
        try:
            filename = self._filename(credential_ref)
            try:
                os.unlink(filename, dir_fd=self._root_fd)
            except FileNotFoundError:
                removed = False
            else:
                removed = True
            self._fsync_directory()
            return removed
        except Exception:
            raise CredentialVaultError(credential_ref) from None

    def find_orphan_receipts(self, live_credential_refs: set[str]) -> list[CredentialReceipt]:
        """Return stored receipts that no current employee record references."""
        receipts: list[CredentialReceipt] = []
        for filename in sorted(os.listdir(self._root_fd)):
            if not filename.endswith(".json"):
                continue
            credential_ref = filename.removesuffix(".json")
            if not _CREDENTIAL_REF_RE.fullmatch(credential_ref):
                continue
            if credential_ref not in live_credential_refs:
                receipts.append(self._receipt(self._read_envelope(credential_ref)))
        return receipts

    @staticmethod
    def _validate_put_values(**values: object) -> None:
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise CredentialVaultError(_INVALID_INPUT_REF)

    @staticmethod
    def _derive_ref(hire_intent_id: str, attempt_id: str) -> str:
        digest = hashlib.sha256(f"{hire_intent_id}|{attempt_id}".encode()).hexdigest()
        return f"cred_{digest}"

    @staticmethod
    def _open_root(root: Path) -> int:
        parts = root.parts[1:] if root.is_absolute() else root.parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise CredentialVaultConfigurationError()
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open("/" if root.is_absolute() else ".", flags)
            for part in parts:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=descriptor)
                child_stat = os.fstat(child)
                if not stat.S_ISDIR(child_stat.st_mode):
                    os.close(child)
                    raise CredentialVaultConfigurationError()
                os.close(descriptor)
                descriptor = child
            os.fchmod(descriptor, 0o700)
            return descriptor
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            raise CredentialVaultConfigurationError() from None

    @staticmethod
    def _filename(credential_ref: str) -> str:
        if not _CREDENTIAL_REF_RE.fullmatch(credential_ref):
            raise CredentialVaultError(credential_ref)
        return f"{credential_ref}.json"

    def _path(self, credential_ref: str) -> Path:
        return self._root / self._filename(credential_ref)

    def _encrypt_envelope(
        self,
        *,
        credential_ref: str,
        key_id: str,
        agent_id: str,
        app_id: str,
        app_secret: str,
        hire_intent_id: str,
        attempt_id: str,
        created_at: str,
    ) -> dict[str, Any]:
        identity = {
            "credential_ref": credential_ref,
            "agent_id": agent_id,
            "app_id": app_id,
            "hire_intent_id": hire_intent_id,
            "attempt_id": attempt_id,
        }
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._keyring.keys[key_id]).encrypt(
            nonce,
            app_secret.encode("utf-8"),
            self._associated_data(identity),
        )
        return {
            "schema_version": 1,
            **identity,
            "key_id": key_id,
            "nonce": base64.urlsafe_b64encode(nonce).decode(),
            "ciphertext": base64.urlsafe_b64encode(ciphertext).decode(),
            "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
            "created_at": created_at,
        }

    @staticmethod
    def _associated_data(identity: Mapping[str, str]) -> bytes:
        canonical = {field: identity[field] for field in _IDENTITY_FIELDS}
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def _decode_envelope_bytes(value: Any) -> bytes:
        if not isinstance(value, str):
            raise ValueError
        return base64.b64decode(value, altchars=b"-_", validate=True)

    def _read_envelope(self, credential_ref: str) -> dict[str, Any]:
        filename = self._filename(credential_ref)
        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(filename, flags, dir_fd=self._root_fd)
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) != 0o600:
                raise ValueError
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                envelope = json.load(stream, object_pairs_hook=_reject_duplicate_object)
            if not isinstance(envelope, dict) or set(envelope) != _ENVELOPE_FIELDS:
                raise ValueError
            if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
                raise ValueError
            string_fields = _ENVELOPE_FIELDS - {"schema_version"}
            if any(not isinstance(envelope[field], str) or not envelope[field] for field in string_fields):
                raise ValueError
            if envelope["credential_ref"] != credential_ref:
                raise ValueError
            if self._derive_ref(envelope["hire_intent_id"], envelope["attempt_id"]) != credential_ref:
                raise ValueError
            nonce = self._decode_envelope_bytes(envelope["nonce"])
            ciphertext = self._decode_envelope_bytes(envelope["ciphertext"])
            if len(nonce) != 12 or not ciphertext:
                raise ValueError
            if not re.fullmatch(r"[0-9a-f]{64}", envelope["ciphertext_sha256"]):
                raise ValueError
            if hashlib.sha256(ciphertext).hexdigest() != envelope["ciphertext_sha256"]:
                raise ValueError
            created_at = datetime.fromisoformat(envelope["created_at"])
            if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
                raise ValueError
            return envelope
        except Exception:
            raise CredentialVaultError(credential_ref) from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _receipt(self, envelope: Mapping[str, Any]) -> CredentialReceipt:
        credential_ref = envelope["credential_ref"]
        return CredentialReceipt(
            credential_ref=credential_ref,
            key_id=envelope["key_id"],
            agent_id=envelope["agent_id"],
            app_id=envelope["app_id"],
            hire_intent_id=envelope["hire_intent_id"],
            attempt_id=envelope["attempt_id"],
            ciphertext_sha256=envelope["ciphertext_sha256"],
            path=self._path(credential_ref),
        )

    def _atomic_write(self, filename: str, envelope: Mapping[str, Any]) -> None:
        temporary = f".{filename}.{secrets.token_hex(8)}.tmp"
        descriptor: int | None = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(temporary, flags, 0o600, dir_fd=self._root_fd)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                json.dump(envelope, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(
                temporary,
                filename,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
            self._fsync_directory()
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass

    def _fsync_directory(self) -> None:
        os.fsync(self._root_fd)
