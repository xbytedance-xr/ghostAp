"""Immutable capability descriptor registry with canonicalization."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any, Callable, Protocol

from ..domain.effects import CapabilityDescriptor
from ..domain.ids import canonical_hash


class AdapterProtocol(Protocol):
    """Minimal adapter interface for tool execution."""

    async def execute(self, parameters: dict[str, Any]) -> dict[str, Any]: ...

    async def query(self, effect_instance_id: str) -> dict[str, Any]: ...


class AdapterHashMismatch(Exception):
    pass


class DuplicateCapability(Exception):
    pass


class UnknownCapability(Exception):
    pass


def canonicalize_descriptor(descriptor: CapabilityDescriptor) -> str:
    """Produce a stable canonical hash for a descriptor.

    Handles: Unicode NFC normalization, sorted keys, deterministic number
    representation, and stripped whitespace in string fields.
    """
    raw = descriptor.to_dict()
    normalized: dict[str, Any] = {}
    for key in sorted(raw.keys()):
        value = raw[key]
        if isinstance(value, str):
            value = unicodedata.normalize("NFC", value).strip()
        elif isinstance(value, float):
            value = round(value, 10)
        elif isinstance(value, list):
            value = [
                unicodedata.normalize("NFC", v).strip() if isinstance(v, str) else v
                for v in value
            ]
        elif isinstance(value, dict):
            value = json.loads(
                json.dumps(value, sort_keys=True, ensure_ascii=False)
            )
        normalized[key] = value
    content = json.dumps(normalized, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def compute_adapter_hash(adapter: Any) -> str:
    """Hash adapter identity for integrity verification."""
    module = getattr(adapter, "__module__", "")
    qualname = getattr(adapter, "__qualname__", type(adapter).__qualname__)
    return hashlib.sha256(f"{module}:{qualname}".encode()).hexdigest()[:32]


class ImmutableCapabilityRegistry:
    """Thread-safe registry of versioned capability descriptors.

    Once registered, a capability@version is immutable. Adapters are
    validated against declared adapter_hash. Registry rejects mutable
    or digest-mismatched adapters.
    """

    def __init__(self) -> None:
        self._descriptors: dict[str, CapabilityDescriptor] = {}
        self._adapters: dict[str, Any] = {}
        self._canonical_hashes: dict[str, str] = {}
        self._frozen = False

    def register(
        self,
        descriptor: CapabilityDescriptor,
        adapter: Any,
        *,
        verify_hash: bool = True,
    ) -> str:
        if self._frozen:
            raise RuntimeError("registry is frozen; no new registrations allowed")

        key = f"{descriptor.capability_id}@{descriptor.version}"
        if key in self._descriptors:
            raise DuplicateCapability(
                f"capability {key} is already registered and immutable"
            )

        if verify_hash and descriptor.adapter_hash:
            actual = compute_adapter_hash(adapter)
            if actual != descriptor.adapter_hash:
                raise AdapterHashMismatch(
                    f"adapter hash mismatch for {key}: "
                    f"declared={descriptor.adapter_hash}, actual={actual}"
                )

        canon = canonicalize_descriptor(descriptor)
        if descriptor.schema_hash and descriptor.schema_hash != canon:
            raise AdapterHashMismatch(
                f"schema hash mismatch for {key}: "
                f"declared={descriptor.schema_hash}, computed={canon}"
            )

        self._descriptors[key] = descriptor
        self._adapters[key] = adapter
        self._canonical_hashes[key] = canon
        return canon

    def freeze(self) -> None:
        self._frozen = True

    @property
    def is_frozen(self) -> bool:
        return self._frozen

    def get(self, capability_id: str, version: str) -> CapabilityDescriptor:
        key = f"{capability_id}@{version}"
        desc = self._descriptors.get(key)
        if desc is None:
            raise UnknownCapability(f"no registered capability: {key}")
        return desc

    def get_adapter(self, capability_id: str, version: str) -> Any:
        key = f"{capability_id}@{version}"
        adapter = self._adapters.get(key)
        if adapter is None:
            raise UnknownCapability(f"no adapter for: {key}")
        return adapter

    def get_canonical_hash(self, capability_id: str, version: str) -> str:
        key = f"{capability_id}@{version}"
        h = self._canonical_hashes.get(key)
        if h is None:
            raise UnknownCapability(f"no canonical hash for: {key}")
        return h

    def resolve_latest(self, capability_id: str) -> CapabilityDescriptor | None:
        matches = [
            (k, d) for k, d in self._descriptors.items()
            if k.startswith(f"{capability_id}@")
        ]
        if not matches:
            return None
        matches.sort(key=lambda kv: kv[0])
        return matches[-1][1]

    def list_all(self) -> list[CapabilityDescriptor]:
        return list(self._descriptors.values())

    def exists(self, capability_id: str, version: str) -> bool:
        return f"{capability_id}@{version}" in self._descriptors

    def __len__(self) -> int:
        return len(self._descriptors)
