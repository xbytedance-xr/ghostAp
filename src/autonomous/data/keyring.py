"""Versioned employee-data encryption keys and BlobStore composition."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from ..journal.blob_store import AesGcmEncryptionProvider, BlobStore

_KEYRING_FIELDS = frozenset({"version", "keys"})


class EmployeeDataConfigurationError(ValueError):
    """Employee data storage is absent or unsafe."""

    def __init__(self) -> None:
        super().__init__(type(self).__name__)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EmployeeDataConfigurationError()
        result[key] = value
    return result


def _decode_key(value: Any) -> bytes:
    if not isinstance(value, str):
        raise EmployeeDataConfigurationError()
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EmployeeDataConfigurationError() from exc
    if len(decoded) != 32:
        raise EmployeeDataConfigurationError()
    return decoded


@dataclass(frozen=True)
class EmployeeDataKeyring:
    keys: Mapping[str, str | bytes] = field(repr=False)
    active_key_id: str

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.active_key_id, str) or not self.active_key_id:
                raise EmployeeDataConfigurationError()
            decoded: dict[str, bytes] = {}
            for key_id, value in self.keys.items():
                if not isinstance(key_id, str) or not key_id:
                    raise EmployeeDataConfigurationError()
                decoded[key_id] = value if isinstance(value, bytes) and len(value) == 32 else _decode_key(value)
            if not decoded or self.active_key_id not in decoded:
                raise EmployeeDataConfigurationError()
            object.__setattr__(self, "keys", MappingProxyType(decoded))
        except (AttributeError, TypeError, EmployeeDataConfigurationError) as exc:
            raise EmployeeDataConfigurationError() from exc

    @classmethod
    def from_settings(cls, settings: Any) -> EmployeeDataKeyring:
        try:
            raw = settings.autonomous_data_keys.get_secret_value()
            active = settings.autonomous_data_active_key_id
            if not raw or not active:
                raise EmployeeDataConfigurationError()
            payload = json.loads(raw, object_pairs_hook=_duplicate_rejecting_object)
            if not isinstance(payload, dict) or set(payload) != _KEYRING_FIELDS:
                raise EmployeeDataConfigurationError()
            if type(payload["version"]) is not int or payload["version"] != 1:
                raise EmployeeDataConfigurationError()
            if not isinstance(payload["keys"], dict):
                raise EmployeeDataConfigurationError()
            return cls(keys=payload["keys"], active_key_id=active)
        except EmployeeDataConfigurationError:
            raise
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise EmployeeDataConfigurationError() from exc

    def resolve(self, key_ref: str) -> bytes:
        try:
            return self.keys[key_ref]
        except (KeyError, TypeError) as exc:
            raise EmployeeDataConfigurationError() from exc


@dataclass
class EmployeeDataStorage:
    keyring: EmployeeDataKeyring = field(repr=False)
    blob_store: BlobStore = field(repr=False)

    @property
    def active_key_id(self) -> str:
        return self.keyring.active_key_id

    def __enter__(self) -> EmployeeDataStorage:
        self.blob_store.__enter__()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        self.blob_store.close()


def build_employee_data_storage(settings: Any) -> EmployeeDataStorage:
    """Fail-closed composition for the dedicated encrypted data BlobStore."""
    try:
        keyring = EmployeeDataKeyring.from_settings(settings)
        root = settings.autonomous_data_blob_dir
        if not isinstance(root, str) or not root:
            raise EmployeeDataConfigurationError()
        provider = AesGcmEncryptionProvider(keyring.resolve)
        return EmployeeDataStorage(
            keyring=keyring,
            blob_store=BlobStore(root, provider),
        )
    except EmployeeDataConfigurationError:
        raise
    except Exception as exc:
        raise EmployeeDataConfigurationError() from exc
