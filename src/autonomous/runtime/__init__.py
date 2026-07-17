"""Agent Runtime - structured execution with broker-mediated model and tool calls.

Exports the primary runtime classes and the sandboxed runner infrastructure.
"""

from .employee_actor import (
    EmployeeActor,
    EmployeeActorStatus,
    EmployeeAssignment,
    EmployeeAssignmentTerminal,
    EmployeeCancellationOutcome,
)
from .employee_session import EmployeeSessionBootstrap, EmployeeSessionKey
from .employee_supervisor import EmployeeActorSnapshot, EmployeeRuntimeSupervisor
from .runner import RunResult, SandboxRunner
from .runtime import (
    AgentRuntime,
    ContextSnapshot,
    RuntimeResult,
    ToolProposal,
    TurnInput,
    TurnOutput,
    TurnRecord,
)
from .worker import execute_task

__all__ = [
    "AgentRuntime",
    "ContextSnapshot",
    "EmployeeSessionBootstrap",
    "EmployeeSessionKey",
    "EmployeeActor",
    "EmployeeActorSnapshot",
    "EmployeeActorStatus",
    "EmployeeAssignment",
    "EmployeeAssignmentTerminal",
    "EmployeeCancellationOutcome",
    "EmployeeRuntimeSupervisor",
    "RunResult",
    "RuntimeResult",
    "SandboxRunner",
    "ToolProposal",
    "TurnInput",
    "TurnOutput",
    "TurnRecord",
    "execute_task",
]
