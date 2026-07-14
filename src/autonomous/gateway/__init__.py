"""Durable visible-employee execution gateway."""

from .context_prompt import (
    RENDER_CONTRACT_DIGEST,
    RenderedEmployeePrompt,
    render_employee_context,
)
from .models import (
    AgentExecutionSpec,
    DispatchBinding,
    DispatchPermit,
    DispatchPermitConsumedError,
    GatewayExecutionResult,
    GatewayExecutionStatus,
)
from .projection import (
    GatewayProjectionError,
    GatewayProjectionState,
    reduce_gateway_frame,
)
from .slock import (
    DispatchPermitAuthorityError,
    EmployeeActionRequiredError,
    EmployeeSlockGateway,
)

__all__ = [
    "AgentExecutionSpec",
    "RENDER_CONTRACT_DIGEST",
    "RenderedEmployeePrompt",
    "DispatchBinding",
    "DispatchPermit",
    "DispatchPermitConsumedError",
    "GatewayExecutionResult",
    "GatewayExecutionStatus",
    "GatewayProjectionError",
    "GatewayProjectionState",
    "DispatchPermitAuthorityError",
    "EmployeeActionRequiredError",
    "EmployeeSlockGateway",
    "reduce_gateway_frame",
    "render_employee_context",
]
