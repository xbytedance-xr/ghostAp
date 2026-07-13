"""Authorized Context composition and pre-execution contracts."""

from __future__ import annotations

from typing import Any, Callable, Protocol

from ..data.facades import MemoryAccessError, MemoryConflictError, MemoryIntegrityError
from ..workforce.registry import (
    ProjectedBindingError,
    ProjectedContextBinding,
    ProjectedCredentialError,
)
from .assembler import EmployeeThreadContext
from .models import (
    AssembledContext,
    AuthorizedContextRequest,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeExecutionInput,
    ThreadContextConfig,
)
from .source import EmployeeMessageSourceFactory


class EmployeeContextPort(Protocol):
    def assemble(self, request: AuthorizedContextRequest) -> AssembledContext: ...


class EmployeeExecutionDelegate(Protocol):
    def execute(self, execution_input: EmployeeExecutionInput) -> str: ...


class EmployeeExecutionAuthorityFence(Protocol):
    """Atomically revalidate authority while invoking one execution callback."""

    def run_if_current(
        self,
        request: AuthorizedContextRequest,
        action: Callable[[], str],
    ) -> str: ...


class EmployeeGenerationAuthorityPort(Protocol):
    def is_current(self, request: AuthorizedContextRequest) -> bool: ...


class RequesterChatAclPort(Protocol):
    def is_authorized(self, request: AuthorizedContextRequest) -> bool: ...


class GroupMemoryPort(Protocol):
    def read_group_memory(self, chat_id: str) -> str: ...


RegistryProvider = Callable[[], Any]


class AuthorizedGroupMemoryReader:
    """ACL wrapper around the legacy Slock full-L2 read port."""

    def __init__(
        self,
        *,
        registry_provider: RegistryProvider,
        requester_acl: RequesterChatAclPort,
        backend: GroupMemoryPort,
    ) -> None:
        self._registry_provider = registry_provider
        self._requester_acl = requester_acl
        self._backend = backend

    def read(self, request: AuthorizedContextRequest) -> str:
        if not isinstance(request, AuthorizedContextRequest):
            raise TypeError("request must be AuthorizedContextRequest")
        failure_reason: ContextUnavailableReason | None = None
        try:
            binding = _resolve_binding(self._registry_provider, request)
            if binding is None:
                raise ContextUnavailableError(ContextUnavailableReason.SCOPE)
            if not self._requester_acl.is_authorized(request):
                raise ContextUnavailableError(
                    ContextUnavailableReason.PERMISSION
                )
            content = self._backend.read_group_memory(request.chat_id)
            if not isinstance(content, str):
                raise ContextUnavailableError(ContextUnavailableReason.MEMORY)
            return content
        except ContextUnavailableError as exc:
            failure_reason = exc.reason
        except ProjectedCredentialError:
            failure_reason = ContextUnavailableReason.CREDENTIALS
        except ProjectedBindingError:
            failure_reason = ContextUnavailableReason.SCOPE
        except Exception:
            failure_reason = ContextUnavailableReason.MEMORY
        raise ContextUnavailableError(failure_reason) from None


class EmployeeContextService:
    """Resolve authority, memories and employee-scoped messages exactly once."""

    def __init__(
        self,
        *,
        registry_provider: RegistryProvider,
        generation_authority: EmployeeGenerationAuthorityPort,
        requester_acl: RequesterChatAclPort,
        data_composition: Any,
        group_memory_reader: AuthorizedGroupMemoryReader,
        source_factory: EmployeeMessageSourceFactory,
        config: ThreadContextConfig | None = None,
    ) -> None:
        dependencies = (
            registry_provider,
            generation_authority,
            requester_acl,
            data_composition,
            group_memory_reader,
            source_factory,
        )
        if any(dependency is None for dependency in dependencies):
            raise ValueError("Context service dependencies must be ready")
        if not hasattr(data_composition, "memory_facade") or not hasattr(
            data_composition, "service"
        ):
            raise ValueError("data_composition read side is incomplete")
        self._registry_provider = registry_provider
        self._generation_authority = generation_authority
        self._requester_acl = requester_acl
        self._data = data_composition
        self._group_memory = group_memory_reader
        self._source_factory = source_factory
        self._config = config or ThreadContextConfig()

    def assemble(self, request: AuthorizedContextRequest) -> AssembledContext:
        if not isinstance(request, AuthorizedContextRequest):
            raise TypeError("request must be AuthorizedContextRequest")
        failure_reason: ContextUnavailableReason | None = None
        try:
            binding = self._authorize(request)
            self._require_data_head(binding)
            l1 = self._data.memory_facade.read_l1(
                request.agent_id,
                request.tenant_key,
                allow_unscoped_legacy=False,
            )
            l2 = self._group_memory.read(request)
            if l1 is not None and not isinstance(l1, str):
                raise ContextUnavailableError(ContextUnavailableReason.MEMORY)
            if not isinstance(l2, str):
                raise ContextUnavailableError(ContextUnavailableReason.MEMORY)
            self._require_same_authority(request, binding)
            with self._source_factory.open(
                scope=request.to_message_scope(),
                principal=binding.principal,
            ) as source:
                snapshot = EmployeeThreadContext(
                    message_source=source,
                    config=self._config,
                ).assemble(
                    l1_summary=l1 or "",
                    l2_summary=l2,
                    system_prompt_token_reserve=(
                        request.system_prompt_token_reserve
                    ),
                    constraints_digest=request.constraints_digest,
                )
            self._require_same_authority(request, binding)
            return snapshot
        except ContextUnavailableError as exc:
            failure_reason = exc.reason
        except ProjectedCredentialError:
            failure_reason = ContextUnavailableReason.CREDENTIALS
        except ProjectedBindingError:
            failure_reason = ContextUnavailableReason.SCOPE
        except (MemoryAccessError, MemoryConflictError, MemoryIntegrityError):
            failure_reason = ContextUnavailableReason.MEMORY
        except Exception:
            failure_reason = ContextUnavailableReason.SOURCE
        raise ContextUnavailableError(failure_reason) from None

    def _authorize(
        self,
        request: AuthorizedContextRequest,
    ) -> ProjectedContextBinding:
        binding = _resolve_binding(self._registry_provider, request)
        if binding is None:
            raise ContextUnavailableError(ContextUnavailableReason.SCOPE)
        if not self._generation_authority.is_current(request):
            raise ContextUnavailableError(ContextUnavailableReason.SCOPE)
        if not self._requester_acl.is_authorized(request):
            raise ContextUnavailableError(ContextUnavailableReason.PERMISSION)
        return binding

    def _require_same_authority(
        self,
        request: AuthorizedContextRequest,
        expected: ProjectedContextBinding,
    ) -> None:
        current = self._authorize(request)
        self._require_data_head(current)
        if current != expected:
            raise ContextUnavailableError(ContextUnavailableReason.SCOPE)

    def _require_data_head(self, binding: ProjectedContextBinding) -> None:
        head = self._data.service.get_head()
        if (
            head.sequence != binding.projection_sequence
            or head.logical_hash != binding.projection_hash
        ):
            raise ContextUnavailableError(ContextUnavailableReason.MEMORY)


def _resolve_binding(
    registry_provider: RegistryProvider,
    request: AuthorizedContextRequest,
) -> ProjectedContextBinding | None:
    registry = registry_provider()
    return registry.context_binding(
        tenant_key=request.tenant_key,
        agent_id=request.agent_id,
        bot_principal_id=request.bot_principal_id,
        app_id=request.app_id,
        chat_id=request.chat_id,
    )


class ContextPreparingExecutionPort:
    """Prepare Context exactly once and delegate only after success."""

    def __init__(
        self,
        *,
        context_service: EmployeeContextPort,
        authority_fence: EmployeeExecutionAuthorityFence,
        delegate: EmployeeExecutionDelegate,
    ) -> None:
        if authority_fence is None:
            raise ValueError("authority_fence is required")
        self._context_service = context_service
        self._authority_fence = authority_fence
        self._delegate = delegate

    def execute(
        self,
        request: AuthorizedContextRequest,
        *,
        tool: str,
        model: str,
        effort: str,
    ) -> str:
        if not isinstance(request, AuthorizedContextRequest):
            raise TypeError("request must be AuthorizedContextRequest")
        snapshot = self._context_service.assemble(request)
        execution_input = EmployeeExecutionInput(
            request=request,
            tool=tool,
            model=model,
            effort=effort,
            context=snapshot,
        )
        return self._authority_fence.run_if_current(
            request,
            lambda: self._delegate.execute(execution_input),
        )


__all__ = [
    "AuthorizedGroupMemoryReader",
    "ContextPreparingExecutionPort",
    "EmployeeContextService",
    "EmployeeContextPort",
    "EmployeeExecutionDelegate",
    "EmployeeExecutionAuthorityFence",
    "EmployeeGenerationAuthorityPort",
    "GroupMemoryPort",
    "RequesterChatAclPort",
]
