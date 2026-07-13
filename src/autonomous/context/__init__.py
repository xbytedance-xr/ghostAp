"""Employee-scoped, fail-closed context contracts and assembly."""

from .assembler import EmployeeThreadContext
from .models import (
    AssembledContext,
    ContextLayer,
    ContextLayerMetrics,
    ContextMessage,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeMessageScope,
    MessageRevision,
    MessageSourceError,
    ThreadContextConfig,
    ThreadWatermark,
    TrimmingRecord,
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
    "ContextLayer",
    "ContextLayerMetrics",
    "ContextMessage",
    "ContextUnavailableError",
    "ContextUnavailableReason",
    "CredentialResolver",
    "EmployeeClientBuilder",
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
