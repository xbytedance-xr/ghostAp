"""Employee-scoped, fail-closed context contracts and assembly."""

from .assembler import EmployeeThreadContext
from .models import (
    AssembledContext,
    AuthorizedContextRequest,
    ContextLayer,
    ContextLayerMetrics,
    ContextMessage,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeExecutionInput,
    EmployeeMessageScope,
    MessageRevision,
    MessageSourceError,
    ThreadContextConfig,
    ThreadWatermark,
    TrimmingRecord,
)
from .service import (
    AuthorizedGroupMemoryReader,
    ContextPreparingExecutionPort,
    EmployeeContextService,
    EmployeeExecutionAuthorityFence,
)
from .source import (
    CredentialResolver,
    EmployeeClientBuilder,
    EmployeeMessageSourceFactory,
    EmployeeScopedMessageSource,
    MessagePage,
    ResolvedThread,
)

# Temporary public compatibility name. The contract itself is now strict and
# employee-scoped; production callers must not use the removed loose arguments.
FeishuMessageSource = EmployeeScopedMessageSource

__all__ = [
    "AssembledContext",
    "AuthorizedContextRequest",
    "AuthorizedGroupMemoryReader",
    "ContextLayer",
    "ContextLayerMetrics",
    "ContextMessage",
    "ContextUnavailableError",
    "ContextUnavailableReason",
    "CredentialResolver",
    "ContextPreparingExecutionPort",
    "EmployeeContextService",
    "EmployeeClientBuilder",
    "EmployeeExecutionAuthorityFence",
    "EmployeeExecutionInput",
    "EmployeeMessageSourceFactory",
    "EmployeeMessageScope",
    "EmployeeScopedMessageSource",
    "EmployeeThreadContext",
    "FeishuMessageSource",
    "MessagePage",
    "MessageRevision",
    "MessageSourceError",
    "ResolvedThread",
    "ThreadContextConfig",
    "ThreadWatermark",
    "TrimmingRecord",
]
