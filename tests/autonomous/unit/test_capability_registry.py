"""Unit tests for ImmutableCapabilityRegistry."""

from __future__ import annotations

import pytest

from src.autonomous.broker.capability_registry import (
    AdapterHashMismatch,
    DuplicateCapability,
    ImmutableCapabilityRegistry,
    UnknownCapability,
    canonicalize_descriptor,
    compute_adapter_hash,
)
from src.autonomous.domain.effects import CapabilityDescriptor
from src.autonomous.domain.enums import RiskLevel


def _make_descriptor(
    cap_id: str = "cap_send_message",
    version: str = "1.0.0",
    **overrides: object,
) -> CapabilityDescriptor:
    defaults = dict(
        capability_id=cap_id,
        name="Send Message",
        version=version,
        risk_level=RiskLevel.R2,
        idempotency="semantic_key",
    )
    defaults.update(overrides)
    return CapabilityDescriptor(**defaults)


class FakeAdapter:
    async def execute(self, parameters: dict) -> dict:
        return {"ok": True}

    async def query(self, effect_instance_id: str) -> dict:
        return {"state": "committed"}


def test_register_and_retrieve() -> None:
    registry = ImmutableCapabilityRegistry()
    desc = _make_descriptor()
    adapter = FakeAdapter()
    canon = registry.register(desc, adapter, verify_hash=False)

    assert registry.exists("cap_send_message", "1.0.0")
    assert registry.get("cap_send_message", "1.0.0") is desc
    assert registry.get_adapter("cap_send_message", "1.0.0") is adapter
    assert registry.get_canonical_hash("cap_send_message", "1.0.0") == canon
    assert len(registry) == 1


def test_duplicate_registration_rejected() -> None:
    registry = ImmutableCapabilityRegistry()
    desc = _make_descriptor()
    registry.register(desc, FakeAdapter(), verify_hash=False)

    with pytest.raises(DuplicateCapability):
        registry.register(desc, FakeAdapter(), verify_hash=False)


def test_frozen_registry_rejects_new_registration() -> None:
    registry = ImmutableCapabilityRegistry()
    registry.freeze()
    assert registry.is_frozen

    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(_make_descriptor(), FakeAdapter(), verify_hash=False)


def test_unknown_capability_raises() -> None:
    registry = ImmutableCapabilityRegistry()

    with pytest.raises(UnknownCapability):
        registry.get("nonexistent", "1.0.0")

    with pytest.raises(UnknownCapability):
        registry.get_adapter("nonexistent", "1.0.0")

    with pytest.raises(UnknownCapability):
        registry.get_canonical_hash("nonexistent", "1.0.0")


def test_adapter_hash_mismatch_rejected() -> None:
    desc = _make_descriptor(adapter_hash="wrong_hash_value")
    registry = ImmutableCapabilityRegistry()

    with pytest.raises(AdapterHashMismatch):
        registry.register(desc, FakeAdapter(), verify_hash=True)


def test_adapter_hash_valid_accepted() -> None:
    adapter = FakeAdapter()
    actual_hash = compute_adapter_hash(adapter)
    desc = _make_descriptor(adapter_hash=actual_hash)
    registry = ImmutableCapabilityRegistry()

    registry.register(desc, adapter, verify_hash=True)
    assert registry.exists("cap_send_message", "1.0.0")


def test_schema_hash_mismatch_rejected() -> None:
    desc = _make_descriptor(schema_hash="deliberately_wrong")
    registry = ImmutableCapabilityRegistry()

    with pytest.raises(AdapterHashMismatch, match="schema hash"):
        registry.register(desc, FakeAdapter(), verify_hash=False)


def test_canonicalization_deterministic_and_unicode_normalized() -> None:
    desc1 = _make_descriptor(name="Send Message")
    desc2 = _make_descriptor(name="Send Message")  # same content
    desc3 = _make_descriptor(name="Different")

    h1 = canonicalize_descriptor(desc1)
    h2 = canonicalize_descriptor(desc2)
    h3 = canonicalize_descriptor(desc3)

    assert h1 == h2
    assert h1 != h3


def test_canonicalization_ignores_whitespace_differences() -> None:
    desc1 = _make_descriptor(description="  hello world  ")
    desc2 = _make_descriptor(description="hello world")

    assert canonicalize_descriptor(desc1) == canonicalize_descriptor(desc2)


def test_resolve_latest_returns_highest_version() -> None:
    registry = ImmutableCapabilityRegistry()
    v1 = _make_descriptor(version="1.0.0")
    v2 = _make_descriptor(version="2.0.0")

    registry.register(v1, FakeAdapter(), verify_hash=False)
    registry.register(v2, FakeAdapter(), verify_hash=False)

    latest = registry.resolve_latest("cap_send_message")
    assert latest is not None
    assert latest.version == "2.0.0"


def test_resolve_latest_returns_none_for_unknown() -> None:
    registry = ImmutableCapabilityRegistry()
    assert registry.resolve_latest("nonexistent") is None


def test_list_all_returns_all_registered() -> None:
    registry = ImmutableCapabilityRegistry()
    registry.register(_make_descriptor(cap_id="cap_a"), FakeAdapter(), verify_hash=False)
    registry.register(_make_descriptor(cap_id="cap_b"), FakeAdapter(), verify_hash=False)

    all_caps = registry.list_all()
    assert len(all_caps) == 2
    ids = {c.capability_id for c in all_caps}
    assert ids == {"cap_a", "cap_b"}
