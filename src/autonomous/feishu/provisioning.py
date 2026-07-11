"""Feishu capability probing and feature visibility gating.

Features are only surfaced when the underlying Feishu API is available
and the tenant has the required gray-release access.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProbeStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ERROR = "error"
    NOT_PROBED = "not_probed"


@dataclass
class CapabilityProbe:
    capability: str
    status: ProbeStatus = ProbeStatus.NOT_PROBED
    error: str = ""
    last_probed_at: float = 0.0


@dataclass
class FeishuCapabilities:
    """Tracks which Feishu capabilities are available for this tenant.

    Features hidden when gray probe fails:
    - meeting_join: VC/meeting integration
    - visible_employee: multi-bot provisioning
    - comment_thread: document comment threads
    - mirror_group: collaboration mirror groups
    """

    meeting_join: CapabilityProbe = field(
        default_factory=lambda: CapabilityProbe("meeting_join")
    )
    visible_employee: CapabilityProbe = field(
        default_factory=lambda: CapabilityProbe("visible_employee")
    )
    comment_thread: CapabilityProbe = field(
        default_factory=lambda: CapabilityProbe("comment_thread")
    )
    mirror_group: CapabilityProbe = field(
        default_factory=lambda: CapabilityProbe("mirror_group")
    )

    def available_actions(self) -> list[str]:
        actions: list[str] = []
        if self.meeting_join.status is ProbeStatus.AVAILABLE:
            actions.append("meeting")
        if self.visible_employee.status is ProbeStatus.AVAILABLE:
            actions.append("visible_employee")
        if self.comment_thread.status is ProbeStatus.AVAILABLE:
            actions.append("comment")
        if self.mirror_group.status is ProbeStatus.AVAILABLE:
            actions.append("mirror")
        return actions

    def probe_all(self, probe_fn: Any = None) -> None:
        """Run capability probes. Default: all unavailable."""
        for cap in (
            self.meeting_join,
            self.visible_employee,
            self.comment_thread,
            self.mirror_group,
        ):
            if probe_fn:
                try:
                    result = probe_fn(cap.capability)
                    cap.status = (
                        ProbeStatus.AVAILABLE
                        if result
                        else ProbeStatus.UNAVAILABLE
                    )
                except Exception as exc:
                    cap.status = ProbeStatus.ERROR
                    cap.error = str(exc)
            else:
                cap.status = ProbeStatus.UNAVAILABLE


def unavailable(error: str = "") -> CapabilityProbe:
    """Create an unavailable probe result."""
    return CapabilityProbe(
        capability="",
        status=ProbeStatus.UNAVAILABLE,
        error=error,
    )
