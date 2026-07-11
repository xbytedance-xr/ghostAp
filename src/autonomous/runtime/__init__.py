"""Agent Runtime - structured execution with broker-mediated model and tool calls.

Exports the primary runtime classes and the sandboxed runner infrastructure.
"""

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
    "RunResult",
    "RuntimeResult",
    "SandboxRunner",
    "ToolProposal",
    "TurnInput",
    "TurnOutput",
    "TurnRecord",
    "execute_task",
]
