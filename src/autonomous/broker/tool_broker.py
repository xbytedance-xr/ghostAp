"""Tool Broker - the only exit point for external tool execution.

Enforces: permissions, budget, idempotency, cancellation, epoch checks,
parameter validation, and kill switch before every dispatch.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

from ..models import (
    AutonomyMode,
    CapabilityDescriptor,
    Effect,
    EffectState,
    EpochSet,
    RiskLevel,
    _new_id,
)


@dataclass
class DispatchRequest:
    """A request to execute a tool capability."""
    capability: str
    arguments: dict
    run_id: str
    step_id: str
    attempt_id: str
    plan_epoch: int
    employee_id: str
    semantic_action_key: str = ""

    def compute_action_key(self) -> str:
        if self.semantic_action_key:
            return self.semantic_action_key
        content = f"{self.capability}|{sorted(self.arguments.items())}"
        return hashlib.sha256(content.encode()).hexdigest()[:24]


@dataclass
class DispatchResult:
    success: bool
    effect_id: str = ""
    result_data: dict = field(default_factory=dict)
    error: str = ""
    idempotent_hit: bool = False


class CapabilityRegistry:
    """Registry of available tool capabilities."""

    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityDescriptor] = {}
        self._adapters: dict[str, Callable] = {}

    def register(self, descriptor: CapabilityDescriptor, adapter: Callable) -> None:
        self._capabilities[descriptor.capability_id] = descriptor
        self._adapters[descriptor.capability_id] = adapter

    def get(self, capability_id: str) -> Optional[CapabilityDescriptor]:
        return self._capabilities.get(capability_id)

    def get_adapter(self, capability_id: str) -> Optional[Callable]:
        return self._adapters.get(capability_id)

    def list_capabilities(self) -> list[CapabilityDescriptor]:
        return list(self._capabilities.values())

    def exists(self, capability_id: str) -> bool:
        return capability_id in self._capabilities


class ToolBroker:
    """Unique tool execution exit point with full safety checks."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        policy_check_fn: Callable,
        budget_reserve_fn: Callable,
        budget_settle_fn: Callable,
        kill_check_fn: Callable,
        epoch_check_fn: Callable,
    ):
        self._registry = registry
        self._policy_check = policy_check_fn
        self._budget_reserve = budget_reserve_fn
        self._budget_settle = budget_settle_fn
        self._kill_check = kill_check_fn
        self._epoch_check = epoch_check_fn
        self._effect_ledger: dict[str, Effect] = {}
        self._action_intents: dict[str, str] = {}  # action_key -> effect_id

    async def dispatch(self, request: DispatchRequest, epochs: EpochSet) -> DispatchResult:
        """Execute a tool call with full safety pipeline."""

        # 1. Kill switch check
        if not self._kill_check(request.capability):
            return DispatchResult(success=False, error="Kill switch active")

        # 2. Epoch validation
        if not self._epoch_check(request.run_id, request.plan_epoch, epochs):
            return DispatchResult(success=False, error="Stale epoch - operation revoked")

        # 3. Capability existence
        descriptor = self._registry.get(request.capability)
        if not descriptor:
            return DispatchResult(success=False, error=f"Unknown capability: {request.capability}")

        # 4. Policy check
        policy_result = self._policy_check(request, descriptor)
        if policy_result.get("decision") == "deny":
            return DispatchResult(
                success=False,
                error=f"Policy denied: {policy_result.get('reason', '')}",
            )
        if policy_result.get("decision") == "require_approval":
            return DispatchResult(
                success=False,
                error=f"Requires approval: {policy_result.get('approval_id', '')}",
            )

        # 5. Idempotency check
        action_key = request.compute_action_key()
        if action_key in self._action_intents:
            existing_eff_id = self._action_intents[action_key]
            existing = self._effect_ledger.get(existing_eff_id)
            if existing and existing.state == EffectState.COMMITTED:
                return DispatchResult(
                    success=True,
                    effect_id=existing_eff_id,
                    idempotent_hit=True,
                )

        # 6. Budget reservation
        budget_entry = self._budget_reserve(request.run_id, "tool_calls", 1.0)
        if not budget_entry:
            return DispatchResult(success=False, error="Budget exceeded")

        # 7. Create Effect in PREPARED state
        effect = Effect(
            action_intent_id=hashlib.sha256(action_key.encode()).hexdigest()[:16],
            capability=request.capability,
            resource_id=request.arguments.get("resource_id", ""),
            semantic_action_key=action_key,
            risk_level=descriptor.risk_level,
            attempt_id=request.attempt_id,
            run_id=request.run_id,
        )
        self._effect_ledger[effect.effect_id] = effect
        self._action_intents[action_key] = effect.effect_id

        # 8. Execute
        adapter = self._registry.get_adapter(request.capability)
        if not adapter:
            effect = replace(effect, state=EffectState.UNKNOWN_EFFECT, active_dispatch=False)
            self._effect_ledger[effect.effect_id] = effect
            return DispatchResult(success=False, effect_id=effect.effect_id, error="No adapter")

        try:
            effect = replace(effect, state=EffectState.EXECUTING, active_dispatch=True)
            self._effect_ledger[effect.effect_id] = effect
            result = await adapter(request.arguments)
            effect = replace(effect, state=EffectState.COMMITTED, active_dispatch=False, committed_at=time.time())
            self._effect_ledger[effect.effect_id] = effect
            self._budget_settle(budget_entry, 1.0)
            return DispatchResult(
                success=True,
                effect_id=effect.effect_id,
                result_data=result if isinstance(result, dict) else {"output": result},
            )
        except Exception as exc:
            effect = replace(effect, state=EffectState.UNKNOWN_EFFECT, active_dispatch=False)
            self._effect_ledger[effect.effect_id] = effect
            return DispatchResult(
                success=False,
                effect_id=effect.effect_id,
                error=str(exc),
            )

    def get_effect(self, effect_id: str) -> Optional[Effect]:
        return self._effect_ledger.get(effect_id)

    def list_effects(self, run_id: str) -> list[Effect]:
        return [e for e in self._effect_ledger.values() if e.run_id == run_id]

    def get_uncommitted_effects(self, run_id: str) -> list[Effect]:
        return [
            e for e in self._effect_ledger.values()
            if e.run_id == run_id and e.state in (
                EffectState.PREPARED, EffectState.EXECUTING, EffectState.UNKNOWN_EFFECT
            )
        ]
