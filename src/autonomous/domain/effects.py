"""Effect, evidence, capability, and finalization aggregates."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .enums import EffectDispositionType, EffectState, RiskLevel
from .ids import (
    freeze,
    new_id,
    strict_bool,
    strict_float,
    strict_int,
    strict_str,
    thaw,
)

UNRESOLVED_EFFECT_STATES = frozenset(
    {
        EffectState.PROPOSED,
        EffectState.POLICY_ALLOWED,
        EffectState.PREPARED,
        EffectState.EXECUTING,
        EffectState.UNKNOWN_EFFECT,
        EffectState.RECONCILING,
        EffectState.RETRY_AUTHORIZED,
        EffectState.MANUAL_RECONCILIATION,
        EffectState.COMPENSATING,
        EffectState.COMPENSATION_FAILED,
    }
)


@dataclass(frozen=True)
class Effect:
    effect_id: str = field(default_factory=lambda: new_id("eff"))
    effect_instance_id: str = ""
    effect_lineage_id: str = ""
    action_intent_id: str = ""
    execution_seq: int = 0
    state: EffectState = EffectState.PROPOSED
    capability: str = ""
    capability_version: str = ""
    resource_id: str = ""
    resource_key: str = ""
    semantic_action_key: str = ""
    risk_level: RiskLevel = RiskLevel.R0
    attempt_id: str = ""
    run_id: str = ""
    tenant_key: str = ""
    created_at: float = field(default_factory=time.time)
    committed_at: float | None = None
    evidence_hash: str = ""
    cleanup_grant_id: str = ""
    provider_idempotency_key: str = ""
    adapter_hash: str = ""
    schema_hash: str = ""
    canonicalization_version: str = ""
    active_dispatch: bool = False
    parent_effect_instance_id: str = ""
    aggregate_version: int = 0

    def __post_init__(self) -> None:
        if not self.effect_instance_id:
            object.__setattr__(self, "effect_instance_id", self.effect_id)
        if not self.effect_id:
            object.__setattr__(self, "effect_id", self.effect_instance_id)
        if not self.effect_lineage_id:
            object.__setattr__(
                self,
                "effect_lineage_id",
                self.action_intent_id or self.effect_instance_id,
            )
        if self.active_dispatch is not (self.state is EffectState.EXECUTING):
            raise ValueError(
                "active_dispatch must be true exactly while Effect is executing"
            )

    @property
    def is_unresolved(self) -> bool:
        return self.state in UNRESOLVED_EFFECT_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "effect_instance_id": self.effect_instance_id,
            "effect_lineage_id": self.effect_lineage_id,
            "action_intent_id": self.action_intent_id,
            "execution_seq": self.execution_seq,
            "state": self.state.value,
            "capability": self.capability,
            "capability_version": self.capability_version,
            "resource_id": self.resource_id,
            "resource_key": self.resource_key,
            "semantic_action_key": self.semantic_action_key,
            "risk_level": self.risk_level.value,
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "tenant_key": self.tenant_key,
            "created_at": self.created_at,
            "committed_at": self.committed_at,
            "evidence_hash": self.evidence_hash,
            "cleanup_grant_id": self.cleanup_grant_id,
            "provider_idempotency_key": self.provider_idempotency_key,
            "adapter_hash": self.adapter_hash,
            "schema_hash": self.schema_hash,
            "canonicalization_version": self.canonicalization_version,
            "active_dispatch": self.active_dispatch,
            "parent_effect_instance_id": self.parent_effect_instance_id,
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Effect:
        if "risk_level" not in data:
            raise ValueError("risk_level is required")
        return cls(
            effect_id=strict_str(data.get("effect_id", ""), "effect_id"),
            effect_instance_id=strict_str(
                data.get("effect_instance_id", ""),
                "effect_instance_id",
            ),
            effect_lineage_id=strict_str(
                data.get("effect_lineage_id", ""),
                "effect_lineage_id",
            ),
            action_intent_id=strict_str(
                data.get("action_intent_id", ""),
                "action_intent_id",
            ),
            execution_seq=strict_int(
                data.get("execution_seq", 0),
                "execution_seq",
                minimum=0,
            ),
            state=EffectState(data.get("state", EffectState.PROPOSED.value)),
            capability=strict_str(
                data.get("capability", ""),
                "capability",
            ),
            capability_version=strict_str(
                data.get("capability_version", ""),
                "capability_version",
            ),
            resource_id=strict_str(
                data.get("resource_id", ""),
                "resource_id",
            ),
            resource_key=strict_str(
                data.get("resource_key", ""),
                "resource_key",
            ),
            semantic_action_key=strict_str(
                data.get("semantic_action_key", ""),
                "semantic_action_key",
            ),
            risk_level=RiskLevel(data["risk_level"]),
            attempt_id=strict_str(
                data.get("attempt_id", ""),
                "attempt_id",
            ),
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            tenant_key=strict_str(
                data.get("tenant_key", ""),
                "tenant_key",
            ),
            created_at=strict_float(
                data.get("created_at", 0),
                "created_at",
            ),
            committed_at=data.get("committed_at"),
            evidence_hash=strict_str(
                data.get("evidence_hash", ""),
                "evidence_hash",
            ),
            cleanup_grant_id=strict_str(
                data.get("cleanup_grant_id", ""),
                "cleanup_grant_id",
            ),
            provider_idempotency_key=strict_str(
                data.get("provider_idempotency_key", ""),
                "provider_idempotency_key",
            ),
            adapter_hash=strict_str(
                data.get("adapter_hash", ""),
                "adapter_hash",
            ),
            schema_hash=strict_str(
                data.get("schema_hash", ""),
                "schema_hash",
            ),
            canonicalization_version=strict_str(
                data.get("canonicalization_version", ""),
                "canonicalization_version",
            ),
            active_dispatch=strict_bool(
                data.get("active_dispatch", False),
                "active_dispatch",
            ),
            parent_effect_instance_id=strict_str(
                data.get("parent_effect_instance_id", ""),
                "parent_effect_instance_id",
            ),
            aggregate_version=strict_int(
                data.get("aggregate_version", 0),
                "aggregate_version",
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class Evidence:
    evidence_id: str = field(default_factory=lambda: new_id("evd"))
    tenant_key: str = ""
    run_id: str = ""
    criterion_id: str = ""
    source: str = ""
    content_hash: str = ""
    sensitivity: str = "normal"
    taint_labels: tuple[str, ...] = ()
    freshness_at: float = field(default_factory=time.time)
    blob_ref: Any = None
    lineage_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "taint_labels", tuple(self.taint_labels))
        object.__setattr__(self, "lineage_ids", tuple(self.lineage_ids))
        object.__setattr__(
            self,
            "blob_ref",
            None if self.blob_ref is None else freeze(self.blob_ref),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "tenant_key": self.tenant_key,
            "run_id": self.run_id,
            "criterion_id": self.criterion_id,
            "source": self.source,
            "content_hash": self.content_hash,
            "sensitivity": self.sensitivity,
            "taint_labels": list(self.taint_labels),
            "freshness_at": self.freshness_at,
            "blob_ref": None if self.blob_ref is None else thaw(self.blob_ref),
            "lineage_ids": list(self.lineage_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Evidence:
        return cls(
            evidence_id=strict_str(data["evidence_id"], "evidence_id"),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            run_id=strict_str(data.get("run_id", ""), "run_id"),
            criterion_id=strict_str(
                data.get("criterion_id", ""), "criterion_id"
            ),
            source=strict_str(data.get("source", ""), "source"),
            content_hash=strict_str(
                data.get("content_hash", ""), "content_hash"
            ),
            sensitivity=strict_str(
                data.get("sensitivity", "normal"), "sensitivity"
            ),
            taint_labels=tuple(data.get("taint_labels", ())),
            freshness_at=strict_float(
                data.get("freshness_at", 0), "freshness_at"
            ),
            blob_ref=data.get("blob_ref"),
            lineage_ids=tuple(data.get("lineage_ids", ())),
        )


@dataclass(frozen=True)
class CapabilityDescriptor:
    capability_id: str = ""
    name: str = ""
    version: str = ""
    business_operation_id: str = ""
    description: str = ""
    risk_level: RiskLevel = RiskLevel.R0
    principal_types: tuple[str, ...] = ()
    idempotency: str = "none"
    idempotency_ttl_seconds: int = 0
    query_after_unknown: str = ""
    query_consistency: str = ""
    negative_observation_window_seconds: int = 0
    compensation: str = ""
    resource_key_template: str = ""
    verifier: str = ""
    parameters_schema: Any = field(default_factory=dict)
    output_schema: Any = field(default_factory=dict)
    adapter_hash: str = ""
    schema_hash: str = ""
    canonicalization_version: str = ""
    creates_persistent_execution: bool = False
    stop_capability: str = ""
    idempotent: bool = False
    supports_query: bool = False
    supports_compensation: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "principal_types", tuple(self.principal_types))
        object.__setattr__(self, "parameters_schema", freeze(self.parameters_schema))
        object.__setattr__(self, "output_schema", freeze(self.output_schema))

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "name": self.name,
            "version": self.version,
            "business_operation_id": self.business_operation_id,
            "description": self.description,
            "risk_level": self.risk_level.value,
            "principal_types": list(self.principal_types),
            "idempotency": self.idempotency,
            "idempotency_ttl_seconds": self.idempotency_ttl_seconds,
            "query_after_unknown": self.query_after_unknown,
            "query_consistency": self.query_consistency,
            "negative_observation_window_seconds": (
                self.negative_observation_window_seconds
            ),
            "compensation": self.compensation,
            "resource_key_template": self.resource_key_template,
            "verifier": self.verifier,
            "parameters_schema": thaw(self.parameters_schema),
            "output_schema": thaw(self.output_schema),
            "adapter_hash": self.adapter_hash,
            "schema_hash": self.schema_hash,
            "canonicalization_version": self.canonicalization_version,
            "creates_persistent_execution": self.creates_persistent_execution,
            "stop_capability": self.stop_capability,
            "idempotent": self.idempotent,
            "supports_query": self.supports_query,
            "supports_compensation": self.supports_compensation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CapabilityDescriptor:
        if "risk_level" not in data:
            raise ValueError("risk_level is required")
        return cls(
            capability_id=strict_str(
                data.get("capability_id", ""),
                "capability_id",
            ),
            name=strict_str(data.get("name", ""), "name"),
            version=strict_str(data.get("version", ""), "version"),
            business_operation_id=strict_str(
                data.get("business_operation_id", ""),
                "business_operation_id",
            ),
            description=strict_str(
                data.get("description", ""),
                "description",
            ),
            risk_level=RiskLevel(data["risk_level"]),
            principal_types=tuple(data.get("principal_types", ())),
            idempotency=strict_str(
                data.get("idempotency", "none"),
                "idempotency",
            ),
            idempotency_ttl_seconds=strict_int(
                data.get("idempotency_ttl_seconds", 0),
                "idempotency_ttl_seconds",
                minimum=0,
            ),
            query_after_unknown=strict_str(
                data.get("query_after_unknown", ""),
                "query_after_unknown",
            ),
            query_consistency=strict_str(
                data.get("query_consistency", ""),
                "query_consistency",
            ),
            negative_observation_window_seconds=strict_int(
                data.get("negative_observation_window_seconds", 0),
                "negative_observation_window_seconds",
                minimum=0,
            ),
            compensation=strict_str(
                data.get("compensation", ""),
                "compensation",
            ),
            resource_key_template=strict_str(
                data.get("resource_key_template", ""),
                "resource_key_template",
            ),
            verifier=strict_str(
                data.get("verifier", ""),
                "verifier",
            ),
            parameters_schema=data.get("parameters_schema", {}),
            output_schema=data.get("output_schema", {}),
            adapter_hash=strict_str(
                data.get("adapter_hash", ""),
                "adapter_hash",
            ),
            schema_hash=strict_str(
                data.get("schema_hash", ""),
                "schema_hash",
            ),
            canonicalization_version=strict_str(
                data.get("canonicalization_version", ""),
                "canonicalization_version",
            ),
            creates_persistent_execution=strict_bool(
                data.get("creates_persistent_execution", False),
                "creates_persistent_execution",
            ),
            stop_capability=strict_str(
                data.get("stop_capability", ""),
                "stop_capability",
            ),
            idempotent=strict_bool(
                data.get("idempotent", False),
                "idempotent",
            ),
            supports_query=strict_bool(
                data.get("supports_query", False),
                "supports_query",
            ),
            supports_compensation=strict_bool(
                data.get("supports_compensation", False),
                "supports_compensation",
            ),
        )


@dataclass(frozen=True, init=False)
class EffectDisposition:
    disposition_id: str
    effect_instance_id: str
    disposition: EffectDispositionType
    actor_principal_id: str
    created_at: float
    @classmethod
    def _build(
        cls,
        *,
        disposition_id: str,
        effect_instance_id: str,
        disposition: EffectDispositionType,
        actor_principal_id: str,
        created_at: float,
    ) -> EffectDisposition:
        value = object.__new__(cls)
        object.__setattr__(value, "disposition_id", disposition_id)
        object.__setattr__(value, "effect_instance_id", effect_instance_id)
        object.__setattr__(value, "disposition", disposition)
        object.__setattr__(value, "actor_principal_id", actor_principal_id)
        object.__setattr__(value, "created_at", created_at)
        return value

    @classmethod
    def create(
        cls,
        effect: Effect,
        disposition: EffectDispositionType,
        *,
        actor_principal_id: str,
    ) -> EffectDisposition:
        if effect.is_unresolved:
            raise ValueError("unresolved Effect cannot receive final disposition")
        allowed = {
            EffectDispositionType.RETAINED: {EffectState.COMMITTED},
            EffectDispositionType.COMPENSATED: {EffectState.COMPENSATED},
            EffectDispositionType.ABANDONED_ACCEPTED: {
                EffectState.ABANDONED_ACCEPTED
            },
            EffectDispositionType.FAILED_SAFE: {
                EffectState.FAILED_SAFE,
                EffectState.ABORTED_NO_DISPATCH,
            },
        }
        if effect.state not in allowed[disposition]:
            raise ValueError(
                f"Effect state {effect.state.value} is incompatible with "
                f"{disposition.value}"
            )
        return cls._build(
            disposition_id=new_id("disp"),
            effect_instance_id=effect.effect_instance_id,
            disposition=disposition,
            actor_principal_id=actor_principal_id,
            created_at=time.time(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition_id": self.disposition_id,
            "effect_instance_id": self.effect_instance_id,
            "disposition": self.disposition.value,
            "actor_principal_id": self.actor_principal_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EffectDisposition:
        return cls._build(
            disposition_id=strict_str(
                data["disposition_id"],
                "disposition_id",
            ),
            effect_instance_id=strict_str(
                data["effect_instance_id"],
                "effect_instance_id",
            ),
            disposition=EffectDispositionType(data["disposition"]),
            actor_principal_id=strict_str(
                data["actor_principal_id"],
                "actor_principal_id",
            ),
            created_at=strict_float(data["created_at"], "created_at"),
        )


@dataclass(frozen=True)
class ResourceQuarantine:
    quarantine_id: str = field(default_factory=lambda: new_id("quarantine"))
    tenant_key: str = ""
    resource_key: str = ""
    source_effect_instance_id: str = ""
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    released_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "quarantine_id": self.quarantine_id,
            "tenant_key": self.tenant_key,
            "resource_key": self.resource_key,
            "source_effect_instance_id": self.source_effect_instance_id,
            "reason": self.reason,
            "created_at": self.created_at,
            "released_at": self.released_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceQuarantine:
        released = data.get("released_at")
        return cls(
            quarantine_id=strict_str(
                data["quarantine_id"],
                "quarantine_id",
            ),
            tenant_key=strict_str(data.get("tenant_key", ""), "tenant_key"),
            resource_key=strict_str(
                data.get("resource_key", ""),
                "resource_key",
            ),
            source_effect_instance_id=strict_str(
                data.get("source_effect_instance_id", ""),
                "source_effect_instance_id",
            ),
            reason=strict_str(data.get("reason", ""), "reason"),
            created_at=strict_float(data.get("created_at", 0), "created_at"),
            released_at=(
                None
                if released is None
                else strict_float(released, "released_at")
            ),
        )
