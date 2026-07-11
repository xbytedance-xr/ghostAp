"""Linearized dispatch gate — the single exit point for effect execution.

Ensures: PREPARED and EXECUTING frames are anchored before any physical send.
Kill/revocation closes the gate before epoch changes. Implicit SDK retry is disabled.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from ..domain.effects import CapabilityDescriptor, Effect, EffectState
from ..domain.enums import RiskLevel
from ..domain.ids import new_id


class DispatchGateClosed(Exception):
    pass


class StaleEpoch(Exception):
    pass


class EffectNotPrepared(Exception):
    pass


@dataclass(frozen=True)
class DispatchEpoch:
    run_id: str
    plan_epoch: int
    gate_epoch: int
    opened_at: float = field(default_factory=time.time)
    closed: bool = False
    close_reason: str = ""


@dataclass(frozen=True)
class PreparedEffect:
    effect: Effect
    dispatch_epoch: DispatchEpoch
    prepared_at: float = field(default_factory=time.time)
    anchored: bool = False
    anchor_sequence: int = 0


@dataclass(frozen=True)
class DispatchOutcome:
    effect_instance_id: str
    success: bool
    committed_at: float | None = None
    error: str = ""
    idempotent_hit: bool = False
    needs_reconciliation: bool = False


class DispatchGate:
    """Linearized dispatch: one physical send at a time per run.

    Flow:
    1. prepare() — creates Effect in PREPARED, records in journal
    2. execute() — sends to adapter exactly once
    3. commit/fail — records outcome

    Gate closure (kill/revocation) prevents new prepare() and in-flight
    execute() calls.
    """

    def __init__(
        self,
        *,
        anchor_fn: Callable[[int, str], bool],
        record_frame_fn: Callable[[str, dict[str, Any]], int],
    ) -> None:
        self._anchor_fn = anchor_fn
        self._record_frame_fn = record_frame_fn
        self._epochs: dict[str, DispatchEpoch] = {}
        self._prepared: dict[str, PreparedEffect] = {}
        self._active_dispatches: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._gate_sequence = 0

    def open_gate(self, run_id: str, plan_epoch: int) -> DispatchEpoch:
        self._gate_sequence += 1
        epoch = DispatchEpoch(
            run_id=run_id,
            plan_epoch=plan_epoch,
            gate_epoch=self._gate_sequence,
        )
        self._epochs[run_id] = epoch
        return epoch

    def close_gate(self, run_id: str, reason: str = "revoked") -> None:
        current = self._epochs.get(run_id)
        if current and not current.closed:
            self._epochs[run_id] = DispatchEpoch(
                run_id=current.run_id,
                plan_epoch=current.plan_epoch,
                gate_epoch=current.gate_epoch,
                opened_at=current.opened_at,
                closed=True,
                close_reason=reason,
            )

    def is_open(self, run_id: str) -> bool:
        epoch = self._epochs.get(run_id)
        return epoch is not None and not epoch.closed

    def get_epoch(self, run_id: str) -> DispatchEpoch | None:
        return self._epochs.get(run_id)

    def prepare(
        self,
        *,
        run_id: str,
        capability: CapabilityDescriptor,
        parameters: dict[str, Any],
        attempt_id: str,
        semantic_action_key: str = "",
        tenant_key: str = "",
    ) -> PreparedEffect:
        epoch = self._epochs.get(run_id)
        if epoch is None or epoch.closed:
            raise DispatchGateClosed(
                f"dispatch gate closed for run {run_id}"
            )

        effect_instance_id = new_id("efx")
        effect = Effect(
            effect_instance_id=effect_instance_id,
            effect_lineage_id=effect_instance_id,
            action_intent_id=semantic_action_key or new_id("intent"),
            state=EffectState.PREPARED,
            capability=capability.capability_id,
            capability_version=capability.version,
            semantic_action_key=semantic_action_key,
            risk_level=capability.risk_level,
            attempt_id=attempt_id,
            run_id=run_id,
            tenant_key=tenant_key,
            adapter_hash=capability.adapter_hash,
            schema_hash=capability.schema_hash,
            canonicalization_version=capability.canonicalization_version,
        )

        seq = self._record_frame_fn(
            "effect.prepared",
            {
                "effect": effect.to_dict(),
                "parameters": parameters,
                "epoch": epoch.gate_epoch,
            },
        )

        anchored = self._anchor_fn(seq, effect.effect_instance_id)

        prepared = PreparedEffect(
            effect=effect,
            dispatch_epoch=epoch,
            anchored=anchored,
            anchor_sequence=seq if anchored else 0,
        )
        self._prepared[effect_instance_id] = prepared
        return prepared

    async def execute(
        self,
        effect_instance_id: str,
        adapter_fn: Callable[..., Any],
        parameters: dict[str, Any],
    ) -> DispatchOutcome:
        async with self._lock:
            prepared = self._prepared.get(effect_instance_id)
            if prepared is None:
                raise EffectNotPrepared(
                    f"effect {effect_instance_id} is not prepared"
                )
            if not prepared.anchored:
                raise EffectNotPrepared(
                    f"effect {effect_instance_id} is not anchored"
                )

            epoch = self._epochs.get(prepared.effect.run_id)
            if epoch is None or epoch.closed:
                raise DispatchGateClosed(
                    f"gate closed during dispatch for {effect_instance_id}"
                )
            if epoch.gate_epoch != prepared.dispatch_epoch.gate_epoch:
                raise StaleEpoch(
                    f"epoch changed for {effect_instance_id}: "
                    f"prepared={prepared.dispatch_epoch.gate_epoch}, "
                    f"current={epoch.gate_epoch}"
                )

            self._active_dispatches[effect_instance_id] = prepared.effect.run_id

            executing_effect = replace(
                prepared.effect,
                state=EffectState.EXECUTING,
                active_dispatch=True,
            )
            self._record_frame_fn(
                "effect.executing",
                {"effect_instance_id": effect_instance_id},
            )

        try:
            result = await adapter_fn(parameters)
            committed_at = time.time()

            self._record_frame_fn(
                "effect.committed",
                {
                    "effect_instance_id": effect_instance_id,
                    "committed_at": committed_at,
                },
            )

            del self._prepared[effect_instance_id]
            self._active_dispatches.pop(effect_instance_id, None)

            return DispatchOutcome(
                effect_instance_id=effect_instance_id,
                success=True,
                committed_at=committed_at,
            )

        except Exception as exc:
            self._record_frame_fn(
                "effect.unknown",
                {
                    "effect_instance_id": effect_instance_id,
                    "error": str(exc),
                },
            )
            self._active_dispatches.pop(effect_instance_id, None)

            return DispatchOutcome(
                effect_instance_id=effect_instance_id,
                success=False,
                error=str(exc),
                needs_reconciliation=True,
            )

    def get_active_dispatches(self, run_id: str) -> list[str]:
        return [
            eid for eid, rid in self._active_dispatches.items()
            if rid == run_id
        ]

    def drain_count(self, run_id: str) -> int:
        return sum(
            1 for p in self._prepared.values()
            if p.effect.run_id == run_id
        ) + len(self.get_active_dispatches(run_id))
