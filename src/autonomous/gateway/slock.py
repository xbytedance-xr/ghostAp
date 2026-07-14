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
        self._running: dict[str, _IssuedPermit] = {}
        self._pre_canceled: set[str] = set()
        self._canceled: dict[str, DispatchPermit] = {}
        self._cancel_requested: set[str] = set()

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
            if (
                binding.permit_id in self._issued
                or binding.permit_id in self._running
                or binding.permit_id in self._canceled
            ):
                raise DispatchPermitAuthorityError("permit identity already issued")
            if binding.permit_id in self._pre_canceled:
                self._pre_canceled.remove(binding.permit_id)
                self._canceled[binding.permit_id] = permit
                self._cancel_requested.add(binding.permit_id)
            else:
                self._issued[binding.permit_id] = _IssuedPermit(permit, frozen_agent)
        return permit

    def cancel_attempt(self, binding: DispatchBinding) -> bool:
        """Cancel an issued/running permit or latch cancellation before issue."""

        if not isinstance(binding, DispatchBinding):
            raise TypeError("binding must be DispatchBinding")
        with self._lock:
            permit_id = binding.permit_id
            issued = self._issued.pop(permit_id, None)
            running = self._running.get(permit_id)
            self._cancel_requested.add(permit_id)
            if issued is not None:
                self._canceled[permit_id] = issued.permit
            elif running is None:
                self._pre_canceled.add(permit_id)
        target = running or issued
        if target is None:
            return False
        if running is not None:
            cancel = getattr(running.permit.engine, "cancel_employee_session", None)
            if not callable(cancel):
                cancel = getattr(running.permit.engine, "stop_agent", None)
            if callable(cancel):
                try:
                    cancel(binding.agent_id)
                except Exception:
                    pass
        return True

    def execute_permit(self, permit: DispatchPermit) -> GatewayExecutionResult:
        if not isinstance(permit, DispatchPermit):
            raise TypeError("permit must be DispatchPermit")
        with self._lock:
            canceled = self._canceled.get(permit.binding.permit_id)
            if canceled is permit:
                del self._canceled[permit.binding.permit_id]
                self._cancel_requested.discard(permit.binding.permit_id)
                permit.claim()
                return GatewayExecutionResult(
                    status=GatewayExecutionStatus.CANCELED,
                    safe_error_code="slock_session_canceled",
                )
            issued = self._issued.get(permit.binding.permit_id)
            if issued is None or issued.permit is not permit:
                raise DispatchPermitAuthorityError("permit was not issued by gateway")
            del self._issued[permit.binding.permit_id]
            self._running[permit.binding.permit_id] = issued
        try:
            permit.claim()
            agent = issued.agent.materialize()
            self._validate_agent_binding(permit.binding, agent)
            runner = getattr(permit.engine, "run_agent_session", None)
            if not callable(runner):
                raise DispatchPermitAuthorityError("permit engine is invalid")
        except BaseException:
            with self._lock:
                self._running.pop(permit.binding.permit_id, None)
                self._cancel_requested.discard(permit.binding.permit_id)
            raise
        result: GatewayExecutionResult
        try:
            output = runner(
                agent,
                permit.prompt,
                timeout=permit.timeout_seconds,
                env=dict(permit.env),
            )
        except TimeoutError:
            result = GatewayExecutionResult(
                status=GatewayExecutionStatus.TIMEOUT,
                safe_error_code="slock_session_timeout",
            )
        except CancelledError:
            result = GatewayExecutionResult(
                status=GatewayExecutionStatus.CANCELED,
                safe_error_code="slock_session_canceled",
            )
        except EmployeeActionRequiredError:
            result = GatewayExecutionResult(
                status=GatewayExecutionStatus.ACTION_REQUIRED,
                safe_error_code="slock_session_action_required",
            )
        except Exception:
            result = GatewayExecutionResult(
                status=GatewayExecutionStatus.FAILED,
                safe_error_code="slock_session_failed",
            )
        else:
            if not isinstance(output, str) or not output:
                result = GatewayExecutionResult(
                    status=GatewayExecutionStatus.FAILED,
                    safe_error_code="slock_session_failed",
                )
            else:
                result = GatewayExecutionResult(
                    status=GatewayExecutionStatus.COMPLETED,
                    output=output,
                )
        with self._lock:
            self._running.pop(permit.binding.permit_id, None)
            canceled_after_start = permit.binding.permit_id in self._cancel_requested
            self._cancel_requested.discard(permit.binding.permit_id)
        if canceled_after_start:
            return GatewayExecutionResult(
                status=GatewayExecutionStatus.CANCELED,
                safe_error_code="slock_session_canceled",
            )
        return result

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
