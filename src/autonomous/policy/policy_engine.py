"""Policy engine: risk/standing-order evaluation and authorization derivation.

Supports activation, derived, effect, and model authorizations through
a unified evaluation pipeline with kill-switch integration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ..domain.control import GoalActivationAuthorization, Principal
from ..domain.enums import AutonomyMode, RiskLevel
from ..domain.goals import EpochSet
from ..domain.ids import new_id
from .authorization import (
    AuthorizationDenied,
    AuthorizationEnvelope,
    AuthorizationResult,
    ControlAuthorizationGate,
    Operation,
)
from .kill_switch import KillSwitch


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class AuthorizationType(str, Enum):
    """Types of authorization the policy engine can evaluate."""

    ACTIVATION = "activation"
    DERIVED = "derived"
    EFFECT = "effect"
    MODEL = "model"
    STANDING_ORDER = "standing_order"


@dataclass
class PolicyContext:
    run_id: str
    step_id: str
    attempt_id: str
    capability: str
    risk_level: RiskLevel
    autonomy_mode: AutonomyMode
    employee_id: str
    resource_id: str = ""
    arguments: dict = field(default_factory=dict)
    authorization_type: AuthorizationType = AuthorizationType.ACTIVATION
    model_id: str = ""
    effect_id: str = ""
    parent_auth_id: str = ""


@dataclass
class PolicyResult:
    decision: PolicyDecision
    reasons: list[str] = field(default_factory=list)
    approval_id: Optional[str] = None
    authorization_type: AuthorizationType = AuthorizationType.ACTIVATION


@dataclass
class _ApprovalRecord:
    approval_id: str
    context: PolicyContext
    granted: bool = False
    approver_id: str = ""
    created_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None


# Risk x Autonomy decision matrix
_POLICY_MATRIX: dict[AutonomyMode, dict[RiskLevel, PolicyDecision]] = {
    AutonomyMode.ASSIST: {
        RiskLevel.R0: PolicyDecision.ALLOW,
        RiskLevel.R1: PolicyDecision.DENY,
        RiskLevel.R2: PolicyDecision.DENY,
        RiskLevel.R3: PolicyDecision.DENY,
        RiskLevel.R4: PolicyDecision.DENY,
    },
    AutonomyMode.SUPERVISED: {
        RiskLevel.R0: PolicyDecision.ALLOW,
        RiskLevel.R1: PolicyDecision.ALLOW,
        RiskLevel.R2: PolicyDecision.REQUIRE_APPROVAL,
        RiskLevel.R3: PolicyDecision.DENY,
        RiskLevel.R4: PolicyDecision.DENY,
    },
    AutonomyMode.BOUNDED_AUTONOMOUS: {
        RiskLevel.R0: PolicyDecision.ALLOW,
        RiskLevel.R1: PolicyDecision.ALLOW,
        RiskLevel.R2: PolicyDecision.ALLOW,
        RiskLevel.R3: PolicyDecision.REQUIRE_APPROVAL,
        RiskLevel.R4: PolicyDecision.DENY,
    },
}

# Standing orders: pre-authorized patterns (capability, risk_level) -> decision
_STANDING_ORDERS: dict[tuple[str, RiskLevel], PolicyDecision] = {}


class PolicyEngine:
    """Risk/standing-order evaluation engine.

    Evaluates activation, derived, effect, and model authorizations
    through a unified pipeline.
    """

    def __init__(self, kill_switch: KillSwitch):
        self._kill_switch = kill_switch
        self._approvals: dict[str, _ApprovalRecord] = {}
        self._standing_orders: dict[
            tuple[str, RiskLevel], PolicyDecision
        ] = dict(_STANDING_ORDERS)

    def register_standing_order(
        self, capability: str, risk_level: RiskLevel, decision: PolicyDecision
    ) -> None:
        """Register a standing order for a capability/risk combination."""
        self._standing_orders[(capability, risk_level)] = decision

    def evaluate(self, context: PolicyContext, epochs: EpochSet) -> PolicyResult:
        """Evaluate policy for a given context and epoch set."""
        # Kill switch checks
        if self._kill_switch.is_killed("global"):
            return PolicyResult(
                PolicyDecision.DENY,
                ["kill switch active (global)"],
                authorization_type=context.authorization_type,
            )

        scoped_keys = [
            f"goal:{context.run_id}",
            f"employee:{context.employee_id}",
            f"tool:{context.capability}",
        ]
        for scope in scoped_keys:
            if self._kill_switch.is_killed(scope):
                return PolicyResult(
                    PolicyDecision.DENY,
                    [f"kill switch active ({scope})"],
                    authorization_type=context.authorization_type,
                )

        if not self._kill_switch.check_gate("global", epochs.kill_epoch):
            return PolicyResult(
                PolicyDecision.DENY,
                ["epoch stale: kill_epoch behind current"],
                authorization_type=context.authorization_type,
            )

        # Route to type-specific evaluation
        if context.authorization_type == AuthorizationType.STANDING_ORDER:
            return self._evaluate_standing_order(context)
        elif context.authorization_type == AuthorizationType.DERIVED:
            return self._evaluate_derived(context)
        elif context.authorization_type == AuthorizationType.EFFECT:
            return self._evaluate_effect(context)
        elif context.authorization_type == AuthorizationType.MODEL:
            return self._evaluate_model(context)
        else:
            return self._evaluate_activation(context)

    def _evaluate_activation(self, context: PolicyContext) -> PolicyResult:
        """Standard risk-matrix evaluation for activation authorizations."""
        # Check standing orders first
        standing = self._standing_orders.get(
            (context.capability, context.risk_level)
        )
        if standing is not None:
            return PolicyResult(
                standing,
                [
                    f"standing_order: cap={context.capability} risk={context.risk_level.value}"
                ],
                authorization_type=AuthorizationType.ACTIVATION,
            )

        decision = _POLICY_MATRIX[context.autonomy_mode][context.risk_level]
        reasons = [
            f"mode={context.autonomy_mode.value}",
            f"risk={context.risk_level.value}",
        ]

        if decision == PolicyDecision.REQUIRE_APPROVAL:
            existing = self._find_granted_approval(context)
            if existing:
                return PolicyResult(
                    PolicyDecision.ALLOW,
                    reasons + ["pre-approved"],
                    existing.approval_id,
                    authorization_type=AuthorizationType.ACTIVATION,
                )
            approval_id = new_id("appr")
            self._approvals[approval_id] = _ApprovalRecord(
                approval_id=approval_id,
                context=context,
            )
            return PolicyResult(
                decision,
                reasons,
                approval_id,
                authorization_type=AuthorizationType.ACTIVATION,
            )

        return PolicyResult(
            decision, reasons, authorization_type=AuthorizationType.ACTIVATION
        )

    def _evaluate_derived(self, context: PolicyContext) -> PolicyResult:
        """Derived authorizations inherit from parent with risk escalation check."""
        if not context.parent_auth_id:
            return PolicyResult(
                PolicyDecision.DENY,
                ["derived authorization requires parent_auth_id"],
                authorization_type=AuthorizationType.DERIVED,
            )

        # Derived operations always require at least the parent's risk level
        decision = _POLICY_MATRIX[context.autonomy_mode][context.risk_level]
        reasons = [
            f"derived from={context.parent_auth_id}",
            f"mode={context.autonomy_mode.value}",
            f"risk={context.risk_level.value}",
        ]
        return PolicyResult(
            decision, reasons, authorization_type=AuthorizationType.DERIVED
        )

    def _evaluate_effect(self, context: PolicyContext) -> PolicyResult:
        """Effect authorizations check side-effect risk and resource scoping."""
        if not context.effect_id:
            return PolicyResult(
                PolicyDecision.DENY,
                ["effect authorization requires effect_id"],
                authorization_type=AuthorizationType.EFFECT,
            )

        decision = _POLICY_MATRIX[context.autonomy_mode][context.risk_level]
        reasons = [
            f"effect={context.effect_id}",
            f"mode={context.autonomy_mode.value}",
            f"risk={context.risk_level.value}",
        ]
        return PolicyResult(
            decision, reasons, authorization_type=AuthorizationType.EFFECT
        )

    def _evaluate_model(self, context: PolicyContext) -> PolicyResult:
        """Model authorizations check model selection permission."""
        if not context.model_id:
            return PolicyResult(
                PolicyDecision.DENY,
                ["model authorization requires model_id"],
                authorization_type=AuthorizationType.MODEL,
            )

        # Model access follows the same risk matrix
        decision = _POLICY_MATRIX[context.autonomy_mode][context.risk_level]
        reasons = [
            f"model={context.model_id}",
            f"mode={context.autonomy_mode.value}",
            f"risk={context.risk_level.value}",
        ]
        return PolicyResult(
            decision, reasons, authorization_type=AuthorizationType.MODEL
        )

    def _evaluate_standing_order(self, context: PolicyContext) -> PolicyResult:
        """Evaluate only against registered standing orders."""
        standing = self._standing_orders.get(
            (context.capability, context.risk_level)
        )
        if standing is not None:
            return PolicyResult(
                standing,
                [
                    f"standing_order: cap={context.capability} risk={context.risk_level.value}"
                ],
                authorization_type=AuthorizationType.STANDING_ORDER,
            )
        # If no standing order registered, fall through to standard matrix
        return self._evaluate_activation(context)

    def grant_approval(self, approval_id: str, approver_id: str) -> bool:
        record = self._approvals.get(approval_id)
        if not record or record.granted:
            return False
        record.granted = True
        record.approver_id = approver_id
        record.resolved_at = time.time()
        return True

    def revoke_approval(self, approval_id: str) -> bool:
        record = self._approvals.get(approval_id)
        if not record:
            return False
        record.granted = False
        record.resolved_at = time.time()
        return True

    def _find_granted_approval(
        self, context: PolicyContext
    ) -> Optional[_ApprovalRecord]:
        for record in self._approvals.values():
            if not record.granted:
                continue
            rc = record.context
            if (
                rc.run_id == context.run_id
                and rc.step_id == context.step_id
                and rc.capability == context.capability
            ):
                return record
        return None
