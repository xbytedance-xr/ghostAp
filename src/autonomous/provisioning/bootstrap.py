"""Production bootstrap for the Autonomous Agent Department system."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DepartmentBootstrapResult:
    """Outcome of department system startup."""

    data_plane_ready: bool = False
    context_ready: bool = False
    provisioning_ready: bool = False
    channel_ready: bool = False
    router_ready: bool = False
    response_ready: bool = False
    employee_count: int = 0
    errors: list[str] | None = None
    dormant: bool = False

    @property
    def healthy(self) -> bool:
        return (
            not self.dormant
            and self.data_plane_ready
            and self.context_ready
            and self.provisioning_ready
            and self.channel_ready
            and self.router_ready
            and self.response_ready
            and not self.errors
        )


class AgentDepartmentBootstrap:
    """Wires and validates all autonomous department subsystems.

    Startup order:
    1. Data plane (keyring, BlobStore, Journal, projections)
    2. Thread context (message source protocol)
    3. Provisioning (hire saga, slash commands, channel)
    4. Router (message routing, execution port)
    5. Response channel (employee delivery outbox)

    Readiness is reported only after Journal/anchor/blob verification
    and projection rebuild complete.
    """

    def __init__(
        self,
        *,
        settings: Any,
        visible_employee_limit: int = 0,
        component_probes: Mapping[str, Callable[[], bool]] | None = None,
    ) -> None:
        self._settings = settings
        self._visible_limit = visible_employee_limit
        self._component_probes = dict(component_probes or {})
        self._started = False
        self._last_result: DepartmentBootstrapResult | None = None

    def start(self) -> DepartmentBootstrapResult:
        """Initialize all subsystems. Fail-closed on any critical error."""
        result = DepartmentBootstrapResult(errors=[])
        if self._visible_limit == 0:
            result.dormant = True
            logger.info(
                "[Department] autonomous_visible_employee_limit=0; "
                "visible employee subsystem dormant and not ready"
            )
            self._started = True
            self._last_result = result
            return result
        required = {
            "data_plane": "data_plane_ready",
            "context": "context_ready",
            "provisioning": "provisioning_ready",
            "channel": "channel_ready",
            "router": "router_ready",
            "response": "response_ready",
        }
        if set(self._component_probes) != set(required):
            result.errors.append("missing_component_probes")
            self._last_result = result
            return result
        for name, attribute in required.items():
            try:
                ready = self._component_probes[name]() is True
            except Exception as exc:
                result.errors.append(f"{name}_probe_failed:{type(exc).__name__}")
                logger.error("[Department] %s probe failed: %s", name, type(exc).__name__)
                ready = False
            setattr(result, attribute, ready)
            if not ready:
                result.errors.append(f"{name}_unavailable")
        self._started = True
        self._last_result = result
        if result.healthy:
            logger.info("[Department] bootstrap complete, limit=%d", self._visible_limit)
        return result

    @property
    def is_ready(self) -> bool:
        return bool(self._started and self._last_result and self._last_result.healthy)

    def shutdown(self) -> None:
        """Graceful shutdown of all subsystems."""
        self._started = False
        self._last_result = None
        logger.info("[Department] shutdown complete")
