"""Integration coverage for authority-bound employee Context assembly."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.autonomous.context import (
    AuthorizedContextRequest,
    AuthorizedGroupMemoryReader,
    ContextPreparingExecutionPort,
    ContextUnavailableError,
    ContextUnavailableReason,
    EmployeeContextService,
    ThreadContextConfig,
)
from src.autonomous.data.projection import JournalHead
from src.autonomous.domain import (
    BotPrincipal,
    EmployeeDefinition,
    EmployeeState,
    WorkerType,
)
from src.autonomous.journal.projections import ProjectionState
from src.autonomous.workforce.registry import ProjectedAgentRegistry
from tests.autonomous.unit.test_employee_thread_context import (
    _FakeSource,
    _pages,
    _stable_thread,
)


def _request() -> AuthorizedContextRequest:
    return AuthorizedContextRequest(
        tenant_key="tenant_1",
        agent_id="agt_1",
        bot_principal_id="bot_1",
        app_id="cli_1",
        channel_generation=7,
        chat_id="oc_1",
        thread_root_message_id="om_root",
        feishu_thread_id="omt_1",
        current_message_id="om_current",
        requester_principal_id="ou_1",
        system_prompt_token_reserve=2,
        constraints_digest="a" * 64,
    )


def _state() -> ProjectionState:
    state = ProjectionState()
    state.employees["agt_1"] = EmployeeDefinition(
        agent_id="agt_1",
        tenant_key="tenant_1",
        owner_principal_id="ou_owner",
        name="Atlas",
        tool="codex",
        model="gpt",
        worker_type=WorkerType.VISIBLE,
        state=EmployeeState.ACTIVE,
        bot_principal_id="bot_1",
        member_groups=("oc_1",),
    )
    state.bot_principals["bot_1"] = BotPrincipal(
        bot_principal_id="bot_1",
        tenant_key="tenant_1",
        agent_id="agt_1",
        app_id="cli_1",
        credential_ref="cred_1",
    )
    return state


class _BooleanAuthority:
    def __init__(self, values: list[bool] | None = None) -> None:
        self._values = list(values or [True])
        self.calls: list[AuthorizedContextRequest] = []

    def is_current(self, request: AuthorizedContextRequest) -> bool:
        self.calls.append(request)
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


class _Acl:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[AuthorizedContextRequest] = []

    def is_authorized(self, request: AuthorizedContextRequest) -> bool:
        self.calls.append(request)
        return self.allowed


class _MemoryFacade:
    def __init__(self, content: str | None = "L1") -> None:
        self.content = content
        self.calls: list[tuple[str, str, bool]] = []

    def read_l1(
        self,
        agent_id: str,
        tenant_key: str,
        *,
        allow_unscoped_legacy: bool,
    ) -> str | None:
        self.calls.append((agent_id, tenant_key, allow_unscoped_legacy))
        return self.content


class _DataService:
    def __init__(self, head: JournalHead = JournalHead()) -> None:
        self.head = head

    def get_head(self) -> JournalHead:
        return self.head


class _GroupBackend:
    def __init__(self, content: str = "L2") -> None:
        self.content = content
        self.calls: list[str] = []

    def read_group_memory(self, chat_id: str) -> str:
        self.calls.append(chat_id)
        return self.content


class _SourceFactory:
    def __init__(self) -> None:
        thread = _stable_thread()
        self.source = _FakeSource(
            traversals=[_pages(thread), _pages(thread)],
        )
        self.calls = []
        self.close_calls = 0

    @contextmanager
    def open(self, *, scope, principal):
        self.calls.append((scope, principal))
        self.source.scope = scope
        try:
            yield self.source
        finally:
            self.close_calls += 1


class _Delegate:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, execution_input):
        self.calls.append(execution_input)
        return "task_1"


class _Fence:
    def run_if_current(self, request, action):
        del request
        return action()


def _composition(
    *,
    state: ProjectionState | None = None,
    generation: _BooleanAuthority | None = None,
    acl: _Acl | None = None,
    memory: _MemoryFacade | None = None,
    backend: _GroupBackend | None = None,
    source_factory: _SourceFactory | None = None,
    config: ThreadContextConfig | None = None,
):
    workforce_state = state or _state()

    def registry_provider() -> ProjectedAgentRegistry:
        return ProjectedAgentRegistry(workforce_state)

    generation = generation or _BooleanAuthority()
    acl = acl or _Acl()
    memory = memory or _MemoryFacade()
    backend = backend or _GroupBackend()
    source_factory = source_factory or _SourceFactory()
    data = SimpleNamespace(
        memory_facade=memory,
        service=_DataService(
            JournalHead(
                workforce_state.cursor_sequence,
                workforce_state.cursor_hash,
            )
        ),
    )
    group_reader = AuthorizedGroupMemoryReader(
        registry_provider=registry_provider,
        requester_acl=acl,
        backend=backend,
    )
    service = EmployeeContextService(
        registry_provider=registry_provider,
        generation_authority=generation,
        requester_acl=acl,
        data_composition=data,
        group_memory_reader=group_reader,
        source_factory=source_factory,
        config=config,
    )
    return SimpleNamespace(
        service=service,
        generation=generation,
        acl=acl,
        memory=memory,
        backend=backend,
        source_factory=source_factory,
        data=data,
    )


def test_assembles_once_from_projected_authority_and_canonical_memories() -> None:
    built = _composition()

    snapshot = built.service.assemble(_request())

    assert snapshot.l1_summary == "L1"
    assert snapshot.l2_summary == "L2"
    assert snapshot.system_prompt_tokens_reserved == 2
    assert snapshot.constraints_digest == "a" * 64
    assert built.memory.calls == [("agt_1", "tenant_1", False)]
    assert built.backend.calls == ["oc_1"]
    assert len(built.source_factory.calls) == 1
    scope, principal = built.source_factory.calls[0]
    assert scope == _request().to_message_scope()
    assert principal.bot_principal_id == "bot_1"
    assert built.source_factory.close_calls == 1


def test_missing_l1_and_l2_are_legal_empty_layers() -> None:
    built = _composition(
        memory=_MemoryFacade(None),
        backend=_GroupBackend(""),
    )

    snapshot = built.service.assemble(_request())

    assert snapshot.l1_summary == ""
    assert snapshot.l2_summary == ""
    assert built.source_factory.close_calls == 1


def test_context_close_rejects_new_work_and_drains_admitted_assembly() -> None:
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()

    class BlockingMemory(_MemoryFacade):
        def read_l1(self, agent_id, tenant_key, *, allow_unscoped_legacy):
            entered.set()
            assert release.wait(2)
            return super().read_l1(
                agent_id,
                tenant_key,
                allow_unscoped_legacy=allow_unscoped_legacy,
            )

    built = _composition(memory=BlockingMemory())
    worker = threading.Thread(target=lambda: built.service.assemble(_request()))
    worker.start()
    assert entered.wait(2)
    closer = threading.Thread(target=lambda: (built.service.close(), closed.set()))
    closer.start()
    assert not closed.wait(0.05)
    with pytest.raises(ContextUnavailableError) as raised:
        built.service.assemble(_request())
    assert raised.value.reason is ContextUnavailableReason.SOURCE

    release.set()
    assert closed.wait(2)
    worker.join()
    closer.join()


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda state: state.employees.__setitem__(
            "agt_1", replace(state.employees["agt_1"], state=EmployeeState.DRAFT)
        ), ContextUnavailableReason.SCOPE),
        (lambda state: state.employees.__setitem__(
            "agt_1", replace(state.employees["agt_1"], member_groups=())
        ), ContextUnavailableReason.SCOPE),
        (lambda state: state.bot_principals.__setitem__(
            "bot_1", replace(state.bot_principals["bot_1"], credential_ref="")
        ), ContextUnavailableReason.CREDENTIALS),
    ],
)
def test_projected_authority_failure_prevents_all_external_reads(
    mutate,
    reason: ContextUnavailableReason,
) -> None:
    state = _state()
    mutate(state)
    built = _composition(state=state)

    with pytest.raises(ContextUnavailableError) as raised:
        built.service.assemble(_request())

    assert raised.value.reason is reason
    assert built.memory.calls == []
    assert built.backend.calls == []
    assert built.source_factory.calls == []


@pytest.mark.parametrize(
    ("generation", "acl", "reason"),
    [
        (_BooleanAuthority([False]), _Acl(), ContextUnavailableReason.SCOPE),
        (_BooleanAuthority(), _Acl(False), ContextUnavailableReason.PERMISSION),
    ],
)
def test_request_authority_failure_prevents_all_external_reads(
    generation: _BooleanAuthority,
    acl: _Acl,
    reason: ContextUnavailableReason,
) -> None:
    built = _composition(generation=generation, acl=acl)

    with pytest.raises(ContextUnavailableError) as raised:
        built.service.assemble(_request())

    assert raised.value.reason is reason
    assert built.memory.calls == []
    assert built.backend.calls == []
    assert built.source_factory.calls == []


def test_authority_change_after_snapshot_closes_source_and_never_delegates() -> None:
    generation = _BooleanAuthority([True, True, False])
    built = _composition(generation=generation)
    delegate = _Delegate()
    port = ContextPreparingExecutionPort(
        context_service=built.service,
        authority_fence=_Fence(),
        delegate=delegate,
    )

    with pytest.raises(ContextUnavailableError) as raised:
        port.execute(_request(), tool="codex", model="gpt", effort="high")

    assert raised.value.reason is ContextUnavailableReason.SCOPE
    assert built.source_factory.close_calls == 1
    assert delegate.calls == []


def test_projection_head_mismatch_fails_before_memory_or_source_reads() -> None:
    built = _composition()
    built.data.service.head = JournalHead(1, "b" * 64)

    with pytest.raises(ContextUnavailableError) as raised:
        built.service.assemble(_request())

    assert raised.value.reason is ContextUnavailableReason.MEMORY
    assert built.memory.calls == []
    assert built.backend.calls == []
    assert built.source_factory.calls == []
