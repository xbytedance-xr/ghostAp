"""Compatibility facade for the employee execution gateway.

New code belongs under :mod:`src.autonomous.gateway`; this import path remains
stable for the Phase 3 integration boundary.
"""

from ..gateway.context_prompt import (
    RENDER_CONTRACT_DIGEST,
    RenderedEmployeePrompt,
    render_employee_context,
)
from ..gateway.coordinator import (
    EmployeeCancellationOutcome,
    EmployeeDispatchCoordinator,
    EmployeeDispatchError,
    FinalizedEmployeeAttempt,
    PreparedEmployeeDispatch,
)
from ..gateway.env_scope import (
    EmployeeEnvironmentAuthority,
    EmployeeProcessEnvironmentMaterial,
)
from ..gateway.models import (
    AgentExecutionSpec,
    DispatchBinding,
    DispatchPermit,
    DispatchPermitConsumedError,
    GatewayExecutionResult,
    GatewayExecutionStatus,
)
from ..gateway.slock import (
    DispatchPermitAuthorityError,
    EmployeeActionRequiredError,
    EmployeeSlockGateway,
)

__all__ = [
    "AgentExecutionSpec",
    "EmployeeDispatchCoordinator",
    "EmployeeCancellationOutcome",
    "EmployeeDispatchError",
    "FinalizedEmployeeAttempt",
    "PreparedEmployeeDispatch",
    "RENDER_CONTRACT_DIGEST",
    "RenderedEmployeePrompt",
    "DispatchBinding",
    "DispatchPermit",
    "DispatchPermitConsumedError",
    "GatewayExecutionResult",
    "GatewayExecutionStatus",
    "DispatchPermitAuthorityError",
    "EmployeeActionRequiredError",
    "EmployeeSlockGateway",
    "EmployeeEnvironmentAuthority",
    "EmployeeProcessEnvironmentMaterial",
    "render_employee_context",
]
