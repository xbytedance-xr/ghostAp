"""Fault injection for employee Thread Context and the zero-dispatch gate."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.autonomous.context import (
    ContextPreparingExecutionPort,
    ContextUnavailableError,
    ContextUnavailableReason,
    MessagePage,
    ThreadContextConfig,
)
from src.autonomous.context.lark_source import LarkEmployeeMessageSourceFactory
from src.autonomous.workforce.credential_vault import (
    CredentialKeyring,
    CredentialVault,
)
from tests.autonomous.contract.test_lark_thread_message_source import (
    _Client,
    _message,
    _MessageAPI,
    _principal,
    _Response,
    _scope,
    _Vault,
)
from tests.autonomous.integration.test_employee_context_service import (
    _composition,
    _Delegate,
    _Fence,
    _GroupBackend,
    _request,
)
from tests.autonomous.unit.test_employee_thread_context import (
    _FakeSource,
    _msg,
)


class _SourceFactory:
    def __init__(self, source) -> None:
        self.source = source
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


class _DispatchSpies(_Delegate):
    def __init__(self) -> None:
        super().__init__()
        self.task_commits = []
        self.acp_calls = []

    def execute(self, execution_input):
        self.task_commits.append(execution_input.request.current_message_id)
        self.acp_calls.append(execution_input.request.current_message_id)
        return super().execute(execution_input)


def _port(*, source_factory, backend=None, config=None):
    built = _composition(
        source_factory=source_factory,
        backend=backend,
        config=config,
    )
    delegate = _DispatchSpies()
    port = ContextPreparingExecutionPort(
        context_service=built.service,
        authority_fence=_Fence(),
        delegate=delegate,
    )
    return built, delegate, port


def _assert_zero_dispatch(delegate: _DispatchSpies) -> None:
    assert delegate.calls == []
    assert delegate.task_commits == []
    assert delegate.acp_calls == []


class _InTraversalMutationSource(_FakeSource):
    """Mutate the source after page one and before page two is returned."""

    def __init__(self, kind: str) -> None:
        super().__init__(traversals=[])
        self._kind = kind
        self._traversal = -1
        self.events: list[tuple[int, str]] = []
        self._root = _msg("om_root", "root", create=1_000, position=0)
        self._before = _msg("om_before", "before", create=2_000, position=1)
        self._current = _msg("om_current", "current", create=4_000, position=3)

    def list_thread_messages(
        self,
        *,
        page_token: str = "",
        page_size: int = 50,
    ) -> MessagePage:
        del page_size
        self.thread_calls += 1
        if not page_token:
            self._traversal += 1
            self.events.append((self._traversal, "page1"))
            return MessagePage((self._root, self._before), True, "next")

        assert page_token == "next"
        inject_mutation = self._traversal % 2 == 0
        messages = []
        if inject_mutation:
            self.events.append((self._traversal, self._kind))
            if self._kind == "insert":
                messages.append(
                    _msg("om_inserted", "inserted", create=3_000, position=2)
                )
            elif self._kind == "edit":
                messages.append(
                    replace(
                        self._before,
                        text="edited",
                        update_time_ms=2_500,
                        edited=True,
                    )
                )
            else:
                messages.append(
                    replace(
                        self._before,
                        text="",
                        update_time_ms=2_500,
                        deleted=True,
                    )
                )
        else:
            self.events.append((self._traversal, "baseline"))
        messages.append(self._current)
        return MessagePage(tuple(messages), False)


@pytest.mark.parametrize("mutation", ["insert", "edit", "delete"])
def test_paging_mutation_fails_revision_and_never_dispatches(
    mutation: str,
) -> None:
    source = _InTraversalMutationSource(mutation)
    source_factory = _SourceFactory(source)
    built, delegate, port = _port(source_factory=source_factory)

    with pytest.raises(ContextUnavailableError) as raised:
        port.execute(_request(), tool="codex", model="gpt", effort="high")

    assert raised.value.reason is ContextUnavailableReason.REVISION
    _assert_zero_dispatch(delegate)
    assert built.source_factory.close_calls == 1
    assert source.thread_calls == 8
    assert source.events == [
        (0, "page1"),
        (0, mutation),
        (1, "page1"),
        (1, "baseline"),
        (2, "page1"),
        (2, mutation),
        (3, "page1"),
        (3, "baseline"),
    ]


def _lark_factory(*, list_responses):
    api = _MessageAPI(
        get_responses=[_Response(items=[_message()])],
        list_responses=list_responses,
    )
    client = _Client(api)
    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=_Vault(),
        client_builder=lambda **_: client,
    )
    return factory


def test_repeated_sdk_page_token_never_dispatches() -> None:
    root = _message(
        "om_root",
        root_id="",
        position=0,
        message_position=10,
    )
    current = _message()
    source_factory = _lark_factory(
        list_responses=[
            _Response(items=[root], has_more=True, page_token="repeat"),
            _Response(items=[current], has_more=True, page_token="repeat"),
        ]
    )
    _built, delegate, port = _port(source_factory=source_factory)

    with pytest.raises(ContextUnavailableError) as raised:
        port.execute(_request(), tool="codex", model="gpt", effort="high")

    assert raised.value.reason is ContextUnavailableReason.PAGINATION
    _assert_zero_dispatch(delegate)
    source_factory.close()


@pytest.mark.parametrize(
    "data",
    [
        None,
        SimpleNamespace(items=None, has_more=False, page_token=""),
        SimpleNamespace(items=[], page_token=""),
        SimpleNamespace(items=[], has_more=True, page_token=None),
    ],
)
def test_partial_success_sdk_page_never_dispatches(data) -> None:
    response = SimpleNamespace(
        code=0,
        success=lambda: True,
        data=data,
    )
    source_factory = _lark_factory(list_responses=[response])
    _built, delegate, port = _port(source_factory=source_factory)

    with pytest.raises(ContextUnavailableError) as raised:
        port.execute(_request(), tool="codex", model="gpt", effort="high")

    assert raised.value.reason in {
        ContextUnavailableReason.SOURCE,
        ContextUnavailableReason.PAGINATION,
    }
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    _assert_zero_dispatch(delegate)
    source_factory.close()


class _SlowSource(_FakeSource):
    def list_thread_messages(self, **kwargs):
        time.sleep(0.02)
        return super().list_thread_messages(**kwargs)


def test_page_timeout_never_dispatches() -> None:
    thread = [
        _msg("om_root", "root", create=1_000, position=0),
        _msg("om_current", "current", create=3_000, position=1),
    ]
    source_factory = _SourceFactory(
        _SlowSource(
            traversals=[
                [MessagePage(tuple(thread), False)],
                [MessagePage(tuple(thread), False)],
            ]
        )
    )
    _built, delegate, port = _port(
        source_factory=source_factory,
        config=ThreadContextConfig(fetch_timeout_seconds=0.001),
    )

    with pytest.raises(ContextUnavailableError) as raised:
        port.execute(_request(), tool="codex", model="gpt", effort="high")

    assert raised.value.reason is ContextUnavailableReason.DEADLINE
    _assert_zero_dispatch(delegate)


class _FailingGroupBackend(_GroupBackend):
    def read_group_memory(self, chat_id: str) -> str:
        super().read_group_memory(chat_id)
        raise RuntimeError("unsafe group backend detail")


def test_group_read_failure_stops_before_source_and_dispatch() -> None:
    source_factory = _SourceFactory(
        _FakeSource(traversals=[]),
    )
    built, delegate, port = _port(
        source_factory=source_factory,
        backend=_FailingGroupBackend(),
    )

    with pytest.raises(ContextUnavailableError) as raised:
        port.execute(_request(), tool="codex", model="gpt", effort="high")

    assert raised.value.reason is ContextUnavailableReason.MEMORY
    assert built.source_factory.calls == []
    _assert_zero_dispatch(delegate)


def test_two_key_rotation_drains_inflight_source_and_uses_fresh_client(
    tmp_path,
) -> None:
    root = tmp_path / "credentials"
    old = CredentialVault(
        root,
        CredentialKeyring(keys={"old": b"o" * 32}, active_key_id="old"),
    )
    receipt = old.put(
        "agt_1",
        "cli_1",
        "employee-rotation-secret",
        "hire_1",
        "attempt_1",
    )
    old.close()
    rotated = CredentialVault(
        root,
        CredentialKeyring(
            keys={"old": b"o" * 32, "new": b"n" * 32},
            active_key_id="new",
        ),
    )
    get_entered = threading.Event()
    release_get = threading.Event()
    invalidated = threading.Event()
    first_errors: list[ContextUnavailableReason] = []
    built_secrets: list[str] = []
    clients = []

    class BlockingAPI(_MessageAPI):
        def get(self, request):
            get_entered.set()
            assert release_get.wait(2)
            return super().get(request)

    class Client(_Client):
        def __init__(self, api):
            super().__init__(api)
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    apis = [
        BlockingAPI(get_responses=[_Response(items=[_message()])]),
        _MessageAPI(get_responses=[_Response(items=[_message()])]),
    ]

    def build_client(*, app_id, app_secret, timeout):
        del app_id, timeout
        built_secrets.append(app_secret)
        client = Client(apis[len(clients)])
        clients.append(client)
        return client

    factory = LarkEmployeeMessageSourceFactory._with_client_builder_for_testing(
        credential_resolver=rotated,
        client_builder=build_client,
    )
    principal = _principal(credential_ref=receipt.credential_ref)

    def read_first_source() -> None:
        try:
            with factory.open(scope=_scope(), principal=principal) as source:
                source.resolve_thread()
        except ContextUnavailableError as exc:
            first_errors.append(exc.reason)

    reader = threading.Thread(target=read_first_source)
    reader.start()
    assert get_entered.wait(2)
    invalidator = threading.Thread(
        target=lambda: (
            factory.invalidate_employee("agt_1"),
            invalidated.set(),
        )
    )
    invalidator.start()
    assert not invalidated.wait(0.05)

    release_get.set()
    reader.join(2)
    invalidator.join(2)
    assert not reader.is_alive()
    assert not invalidator.is_alive()
    assert invalidated.is_set()
    assert first_errors == [ContextUnavailableReason.SOURCE]

    rotated_receipt = rotated.rewrap(
        receipt.credential_ref,
        "agt_1",
        "cli_1",
    )
    assert rotated_receipt.key_id == "new"
    assert json.loads(receipt.path.read_text())["key_id"] == "new"
    factory.reactivate_employee("agt_1")
    with factory.open(scope=_scope(), principal=principal) as source:
        assert source.resolve_thread().feishu_thread_id == "omt_1"

    assert built_secrets == [
        "employee-rotation-secret",
        "employee-rotation-secret",
    ]
    assert len(clients) == 2
    assert all(client.close_calls == 1 for client in clients)
    factory.close()
    rotated.close()
