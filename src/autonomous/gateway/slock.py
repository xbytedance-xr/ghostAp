"""One-shot execution gateway over the real activated Slock engine."""

from __future__ import annotations

import threading
from concurrent.futures import CancelledError
from dataclasses import dataclass

from src.acp.employee_selection import compose_employee_model_selection
from src.slock_engine.models import AgentIdentity

from .models import (
    AgentExecutionSpec,
    DispatchBinding,
    DispatchPermit,
    GatewayExecutionResult,
    GatewayExecutionStatus,
)


class DispatchPermitAuthorityError(RuntimeError):
    """A permit was not issued by this live gateway or no longer has authority."""


class EmployeeActionRequiredError(RuntimeError):
    """Execution stopped safely and requires an explicit human decision."""


@dataclass(frozen=True, slots=True)
class _IssuedPermit:
    permit: DispatchPermit
    agent: AgentExecutionSpec


class EmployeeSlockGateway:
    """Mint and consume process-local capabilities for already-anchored attempts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._issued: dict[str, _IssuedPermit] = {}

    def issue_permit(
        self,
        *,
        binding: DispatchBinding,
        prompt: str,
        engine: object,
        agent: AgentIdentity,
        timeout_seconds: float,
        env: dict[str, str],
    ) -> DispatchPermit:
        self._validate_agent_binding(binding, agent)
        frozen_agent = AgentExecutionSpec.from_agent(agent)
        permit = DispatchPermit(
            binding=binding,
            prompt=prompt,
            engine=engine,
            agent=frozen_agent,
            timeout_seconds=timeout_seconds,
            env=env,
        )
        with self._lock:
            if binding.permit_id in self._issued:
                raise DispatchPermitAuthorityError("permit identity already issued")
            self._issued[binding.permit_id] = _IssuedPermit(permit, frozen_agent)
        return permit

    def execute_permit(self, permit: DispatchPermit) -> GatewayExecutionResult:
        if not isinstance(permit, DispatchPermit):
            raise TypeError("permit must be DispatchPermit")
        with self._lock:
            issued = self._issued.get(permit.binding.permit_id)
            if issued is None or issued.permit is not permit:
                raise DispatchPermitAuthorityError("permit was not issued by gateway")
            del self._issued[permit.binding.permit_id]
        permit.claim()
        agent = issued.agent.materialize()
        self._validate_agent_binding(permit.binding, agent)
        runner = getattr(permit.engine, "run_agent_session", None)
        if not callable(runner):
            raise DispatchPermitAuthorityError("permit engine is invalid")
        try:
            output = runner(
                agent,
                permit.prompt,
                timeout=permit.timeout_seconds,
                env=dict(permit.env),
            )
        except TimeoutError:
            return GatewayExecutionResult(
                status=GatewayExecutionStatus.TIMEOUT,
                safe_error_code="slock_session_timeout",
            )
        except CancelledError:
            return GatewayExecutionResult(
                status=GatewayExecutionStatus.CANCELED,
                safe_error_code="slock_session_canceled",
            )
        except EmployeeActionRequiredError:
            return GatewayExecutionResult(
                status=GatewayExecutionStatus.ACTION_REQUIRED,
                safe_error_code="slock_session_action_required",
            )
        except Exception:
            return GatewayExecutionResult(
                status=GatewayExecutionStatus.FAILED,
                safe_error_code="slock_session_failed",
            )
        if not isinstance(output, str) or not output:
            return GatewayExecutionResult(
                status=GatewayExecutionStatus.FAILED,
                safe_error_code="slock_session_failed",
            )
        return GatewayExecutionResult(
            status=GatewayExecutionStatus.COMPLETED,
            output=output,
        )

    @staticmethod
    def _validate_agent_binding(
        binding: DispatchBinding,
        agent: AgentIdentity,
    ) -> None:
        coordinates = (
            agent.agent_id == binding.agent_id,
            agent.agent_type == binding.tool,
            agent.model_name
            == compose_employee_model_selection(
                binding.tool,
                binding.model,
                binding.profile,
                binding.effort,
            ),
            agent.model_profile == binding.profile,
            agent.reasoning_effort == binding.effort,
            tuple(sorted(agent.permissions)) == binding.permissions,
            tuple(sorted(agent.capabilities)) == binding.capabilities,
            agent.security_profile == binding.security_profile == "employee_v1",
        )
        if not all(coordinates):
            raise DispatchPermitAuthorityError("permit agent binding mismatch")


__all__ = [
    "DispatchPermitAuthorityError",
    "EmployeeActionRequiredError",
    "EmployeeSlockGateway",
]
