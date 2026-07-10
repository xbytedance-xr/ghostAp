"""Fail-closed deployment configuration for the autonomous work system."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Protocol


class AutonomousDeploymentMode(str, Enum):
    OFF = "off"
    SHADOW_READ = "shadow_read"
    MANAGER_ONLY = "manager_only"


class EffectiveAutonomy(str, Enum):
    OFF = "off"
    SHADOW_READ = "shadow_read"
    ASSIST = "assist"
    SUPERVISED = "supervised"
    BOUNDED_AUTONOMOUS = "bounded_autonomous"


@dataclass(frozen=True)
class SafetyGateStatus:
    mode: EffectiveAutonomy
    blockers: tuple[str, ...]
    attestations: Mapping[str, bool]


class AutonomousSettings(Protocol):
    autonomous_deployment_mode: AutonomousDeploymentMode | str
    autonomous_write_enabled: bool
    autonomous_manager_acl: str
    autonomous_anchor_provider: str
    autonomous_sandbox_required: bool


_SUPERVISED_ATTESTATIONS = (
    "journal",
    "anchor",
    "worker_sandbox",
    "oracle_sandbox",
    "brokers",
    "p0_gates",
)


def _deployment_mode(value: AutonomousDeploymentMode | str) -> AutonomousDeploymentMode:
    if isinstance(value, AutonomousDeploymentMode):
        return value
    return AutonomousDeploymentMode(value)


def derive_effective_autonomy(
    settings: AutonomousSettings,
    attestations: Mapping[str, bool],
) -> SafetyGateStatus:
    """Derive runtime autonomy without allowing configuration to bypass gates."""
    requested = _deployment_mode(settings.autonomous_deployment_mode)
    frozen_attestations = MappingProxyType(dict(attestations))

    if requested is AutonomousDeploymentMode.OFF:
        return SafetyGateStatus(EffectiveAutonomy.OFF, (), frozen_attestations)

    if requested is AutonomousDeploymentMode.SHADOW_READ:
        blockers = () if attestations.get("journal", False) else ("journal",)
        mode = EffectiveAutonomy.SHADOW_READ if not blockers else EffectiveAutonomy.OFF
        return SafetyGateStatus(mode, blockers, frozen_attestations)

    blockers = [
        gate
        for gate in _SUPERVISED_ATTESTATIONS
        if not attestations.get(gate, False)
    ]
    if not settings.autonomous_write_enabled:
        blockers.append("write_enabled")
    if not settings.autonomous_manager_acl:
        blockers.append("manager_acl")
    if not settings.autonomous_anchor_provider and "anchor" not in blockers:
        blockers.append("anchor")
    if not settings.autonomous_sandbox_required:
        blockers.append("sandbox_required")

    if blockers:
        return SafetyGateStatus(
            EffectiveAutonomy.ASSIST,
            tuple(dict.fromkeys(blockers)),
            frozen_attestations,
        )

    has_bounded_grant = attestations.get("standing_order", False) or attestations.get(
        "capability_grant",
        False,
    )
    mode = (
        EffectiveAutonomy.BOUNDED_AUTONOMOUS
        if has_bounded_grant
        else EffectiveAutonomy.SUPERVISED
    )
    return SafetyGateStatus(mode, (), frozen_attestations)
