"""Encrypted content-addressed blob storage for sensitive runtime payloads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from Crypto.Cipher import AES

BLOB_MAGIC = "GHOSTAP-BLOB"
BLOB_SCHEMA_VERSION = 1
_HEX_CHARS = frozenset("0123456789abcdef")


class BlobError(RuntimeError):
    """Base class for blob-store failures."""


class BlobFormatError(BlobError):
    """The on-disk envelope is malformed or unsupported."""


class BlobIntegrityError(BlobError):
    """The content address or plaintext hash does not match."""


class BlobAuthenticationError(BlobError):
    """Authenticated decryption failed."""


class BlobPublishError(BlobError):
    """A durable publication boundary failed."""


class InvalidEncryptionKeyError(BlobError):
    """The resolved encryption key is invalid."""


class KeyResolutionError(BlobError):
    """A key reference could not be resolved."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in _HEX_CHARS for char in value)
    )


def _random_nonce() -> bytes:
    return secrets.token_bytes(12)


def _write_bytes(path: Path, value: bytes) -> None:
    with open(path, "xb") as file:
        os.chmod(path, 0o600)
        file.write(value)
        file.flush()


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as file:
        os.fsync(file.fileno())


def _atomic_replace(source: Path, target: Path) -> None:
    os.replace(source, target)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class BlobRef:
    """Immutable reference to one encrypted content-addressed blob."""

    blob_hash: str = ""
    payload_hash: str = ""
    labels_hash: str = ""
    key_ref: str = ""
    size: int = 0
    labels: Mapping[str, str] | None = None
    blob_id: str = ""
    content_hash: str = ""
    ciphertext_hash: str = ""

    def __post_init__(self) -> None:
        blob_aliases = [
            value
            for value in (self.blob_hash, self.blob_id, self.ciphertext_hash)
            if value
        ]
        payload_aliases = [
            value
            for value in (self.payload_hash, self.content_hash)
            if value
        ]
        if len(set(blob_aliases)) > 1:
            raise ValueError("conflicting blob hash aliases")
        if len(set(payload_aliases)) > 1:
            raise ValueError("conflicting payload hash aliases")
        blob_hash = self.blob_hash or self.blob_id or self.ciphertext_hash
        payload_hash = self.payload_hash or self.content_hash
        if not blob_hash:
            raise ValueError("blob hash is required")
        if not payload_hash:
            raise ValueError("payload hash is required")
        object.__setattr__(self, "blob_hash", blob_hash)
        object.__setattr__(self, "blob_id", blob_hash)
        object.__setattr__(self, "ciphertext_hash", blob_hash)
        object.__setattr__(self, "payload_hash", payload_hash)
        object.__setattr__(self, "content_hash", payload_hash)
        object.__setattr__(
            self,
            "labels",
            MappingProxyType(dict(self.labels or {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blob_id": self.blob_id,
            "content_hash": self.content_hash,
            "ciphertext_hash": self.ciphertext_hash,
            "payload_hash": self.payload_hash,
            "labels_hash": self.labels_hash,
            "size": self.size,
            "labels": dict(self.labels or {}),
            "key_ref": self.key_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BlobRef:
        return cls(
            blob_hash=str(
                value.get("blob_hash")
                or value.get("blob_id")
                or value.get("ciphertext_hash")
                or ""
            ),
            payload_hash=str(
                value.get("payload_hash") or value.get("content_hash") or ""
            ),
            labels_hash=str(value.get("labels_hash") or ""),
            key_ref=str(value.get("key_ref") or ""),
            size=int(value.get("size") or 0),
            labels=value.get("labels") if isinstance(value.get("labels"), Mapping) else {},
        )


class EncryptionProvider(Protocol):
    """Authenticated encryption boundary used by BlobStore."""

    def encrypt(
        self,
        plaintext: bytes,
        *,
        key_ref: str,
        aad: bytes,
        nonce: bytes,
    ) -> tuple[bytes, bytes]: ...

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        key_ref: str,
        aad: bytes,
        nonce: bytes,
        tag: bytes,
    ) -> bytes: ...


class AesGcmEncryptionProvider:
    """AES-256-GCM provider backed by an injected key resolver."""

    def __init__(self, key_resolver: Callable[[str], bytes]) -> None:
        self._key_resolver = key_resolver

    def _resolve(self, key_ref: str) -> bytes:
        try:
            key = self._key_resolver(key_ref)
        except Exception as exc:
            raise KeyResolutionError(f"failed to resolve key_ref {key_ref!r}") from exc
        if not isinstance(key, bytes) or len(key) != 32:
            raise InvalidEncryptionKeyError("AES-GCM key must be exactly 32 bytes")
        return key

    def encrypt(
        self,
        plaintext: bytes,
        *,
        key_ref: str,
        aad: bytes,
        nonce: bytes,
    ) -> tuple[bytes, bytes]:
        cipher = AES.new(self._resolve(key_ref), AES.MODE_GCM, nonce=nonce)
        cipher.update(aad)
        return cipher.encrypt_and_digest(plaintext)

    def decrypt(
        self,
        ciphertext: bytes,
        *,
        key_ref: str,
        aad: bytes,
        nonce: bytes,
        tag: bytes,
    ) -> bytes:
        cipher = AES.new(self._resolve(key_ref), AES.MODE_GCM, nonce=nonce)
        cipher.update(aad)
        try:
            return cipher.decrypt_and_verify(ciphertext, tag)
        except ValueError as exc:
            raise BlobAuthenticationError("blob authentication failed") from exc


def _aad(*, key_ref: str, labels_hash: str) -> bytes:
    return _canonical_json(
        {
            "magic": BLOB_MAGIC,
            "schema_version": BLOB_SCHEMA_VERSION,
            "key_ref": key_ref,
            "labels_hash": labels_hash,
        }
    )


class BlobStore:
    """Durably publish and read encrypted content-addressed blobs."""

    def __init__(self, root: str | Path, encryption: EncryptionProvider) -> None:
        self.root = Path(root)
        self.encryption = encryption
        root_existed = self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        if not root_existed:
            _fsync_directory(self.root.parent)

    def stage_and_publish(
        self,
        payload: bytes,
        labels: Mapping[str, str],
        key_ref: str,
    ) -> BlobRef:
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if not isinstance(labels, Mapping):
            raise TypeError("labels must be a mapping")
        if not isinstance(key_ref, str) or not key_ref:
            raise ValueError("key_ref must be a non-empty string")
        labels_value = {
            str(key): str(value)
            for key, value in labels.items()
        }
        labels_hash = _sha256(_canonical_json(labels_value))
        payload_hash = _sha256(payload)
        nonce = _random_nonce()
        aad = _aad(key_ref=key_ref, labels_hash=labels_hash)
        ciphertext, tag = self.encryption.encrypt(
            payload,
            key_ref=key_ref,
            aad=aad,
            nonce=nonce,
        )
        envelope = _canonical_json(
            {
                "magic": BLOB_MAGIC,
                "schema_version": BLOB_SCHEMA_VERSION,
                "key_ref": key_ref,
                "labels_hash": labels_hash,
                "payload_hash": payload_hash,
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "tag": base64.b64encode(tag).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            }
        )
        blob_hash = _sha256(envelope)
        ref = BlobRef(
            blob_hash=blob_hash,
            payload_hash=payload_hash,
            labels_hash=labels_hash,
            key_ref=key_ref,
            size=len(payload),
            labels=labels_value,
        )
        target = self.root / f"{blob_hash}.blob"
        fd, raw_temp = tempfile.mkstemp(
            prefix=".blob-",
            suffix=".tmp",
            dir=self.root,
        )
        os.close(fd)
        temp = Path(raw_temp)
        temp.unlink()
        try:
            _write_bytes(temp, envelope)
            _fsync_file(temp)
            if target.is_symlink():
                raise BlobIntegrityError(
                    "existing content address is not a regular file"
                )
            if target.exists():
                target_stat = target.stat(follow_symlinks=False)
                if not stat.S_ISREG(target_stat.st_mode):
                    raise BlobIntegrityError(
                        "existing content address is not a regular file"
                    )
                if target.read_bytes() != envelope:
                    raise BlobIntegrityError(
                        "existing content address contains different bytes"
                    )
                temp.unlink(missing_ok=True)
                _fsync_directory(self.root)
                return ref
            _atomic_replace(temp, target)
            target.chmod(0o600)
            _fsync_directory(self.root)
            return ref
        except BlobError:
            temp.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temp.unlink(missing_ok=True)
            boundary = next(
                (
                    name
                    for name in (
                        "_write_bytes",
                        "_fsync_file",
                        "_atomic_replace",
                        "_fsync_directory",
                    )
                    if name in str(exc)
                ),
                "blob publish",
            )
            raise BlobPublishError(f"{boundary} failed: {exc}") from exc

    def read(self, ref: BlobRef) -> bytes:
        if not isinstance(ref, BlobRef):
            raise TypeError("ref must be BlobRef")
        if not _is_sha256(ref.blob_hash):
            raise BlobIntegrityError("blob hash is not sha256 hex")
        if not _is_sha256(ref.payload_hash):
            raise BlobIntegrityError("payload hash is not sha256 hex")
        if not _is_sha256(ref.labels_hash):
            raise BlobIntegrityError("labels hash is not sha256 hex")
        path = self.root / f"{ref.blob_hash}.blob"
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise BlobIntegrityError("blob is missing") from exc
        except OSError as exc:
            raise BlobIntegrityError(f"blob read failed: {exc}") from exc
        if _sha256(raw) != ref.blob_hash:
            raise BlobIntegrityError("blob hash mismatch")
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BlobFormatError("blob envelope is malformed") from exc
        required = {
            "magic",
            "schema_version",
            "key_ref",
            "labels_hash",
            "payload_hash",
            "nonce",
            "tag",
            "ciphertext",
        }
        if not isinstance(envelope, dict) or set(envelope) != required:
            raise BlobFormatError("invalid blob envelope fields")
        if envelope["magic"] != BLOB_MAGIC:
            raise BlobFormatError("invalid blob magic")
        if envelope["schema_version"] != BLOB_SCHEMA_VERSION:
            raise BlobFormatError("unsupported blob schema")
        if envelope["key_ref"] != ref.key_ref:
            raise BlobIntegrityError("key_ref mismatch")
        if envelope["labels_hash"] != ref.labels_hash:
            raise BlobAuthenticationError("labels hash does not match reference")
        if envelope["payload_hash"] != ref.payload_hash:
            raise BlobIntegrityError("payload hash does not match reference")
        try:
            nonce = base64.b64decode(envelope["nonce"], validate=True)
            tag = base64.b64decode(envelope["tag"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext"], validate=True)
        except (TypeError, ValueError) as exc:
            raise BlobFormatError("invalid base64 blob field") from exc
        if len(nonce) != 12 or len(tag) != 16:
            raise BlobFormatError("invalid nonce or authentication tag")
        plaintext = self.encryption.decrypt(
            ciphertext,
            key_ref=ref.key_ref,
            aad=_aad(key_ref=ref.key_ref, labels_hash=ref.labels_hash),
            nonce=nonce,
            tag=tag,
        )
        expected_labels_hash = _sha256(_canonical_json(dict(ref.labels or {})))
        if expected_labels_hash != ref.labels_hash:
            raise BlobIntegrityError("labels hash does not match labels")
        if len(plaintext) != ref.size:
            raise BlobIntegrityError("plaintext size mismatch")
        if _sha256(plaintext) != ref.payload_hash:
            raise BlobIntegrityError("payload hash mismatch")
        return plaintext

    def cleanup_orphan_temps(self) -> int:
        removed = 0
        for path in self.root.glob(".blob-*.tmp"):
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                continue
        if removed:
            _fsync_directory(self.root)
        return removed
