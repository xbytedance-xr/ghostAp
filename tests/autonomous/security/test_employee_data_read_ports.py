from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.autonomous.data.ports import MemoryQuerySpec
from src.autonomous.data.projection import DataProjectionState
from src.autonomous.data.query import (
    AuthenticatedDataRequest,
    EmployeeDataRequestContextFactory,
    EmployeeDataSubject,
    EmployeeMemoryQuery,
    QueryDeniedError,
)


def _request(*, principal: str = "ou_member", chat_id: str = "oc_team") -> AuthenticatedDataRequest:
    return AuthenticatedDataRequest(
        principal_id=principal,
        tenant_key="tenant_1",
        receiving_bot_app_id="employee_bot",
        chat_id=chat_id,
        chat_type="group",
        thread_root_id="om_root",
        requested_agent_id="agt_alpha",
    )


def _subject(*, groups: tuple[str, ...] = ("oc_team",)) -> EmployeeDataSubject:
    return EmployeeDataSubject(
        tenant_key="tenant_1",
        agent_id="agt_alpha",
        owner_principal_id="ou_owner",
        member_groups=groups,
    )


def test_nonempty_chat_id_is_not_membership_proof() -> None:
    factory = EmployeeDataRequestContextFactory(
        admin_principal_ids=frozenset(),
        main_bot_app_id="main_bot",
        subject_resolver=lambda *_args: _subject(groups=()),
    )

    context = factory.resolve(_request(), DataProjectionState())

    assert context.is_same_chat_member is False


def test_employee_memory_denies_before_materialized_read() -> None:
    facade = SimpleNamespace(read_l1=lambda *_args, **_kwargs: pytest.fail("L1 read before ACL"))
    facade.read_memory_summary = lambda *_args, **_kwargs: pytest.fail(
        "summary read before ACL"
    )
    query = EmployeeMemoryQuery(
        memory_facade=facade,
        state=DataProjectionState(),
        context_factory=EmployeeDataRequestContextFactory(
            admin_principal_ids=frozenset(),
            main_bot_app_id="main_bot",
            subject_resolver=lambda *_args: _subject(groups=()),
        ),
    )

    with pytest.raises(QueryDeniedError):
        query.query(_request(), MemoryQuerySpec(agent_id="agt_alpha"))


def test_employee_channel_memory_is_scoped_to_authenticated_chat() -> None:
    calls: list[tuple[str, str, str, str]] = []

    class _Facade:
        def read_memory_summary(self, agent_id, tenant_key, chat_id, thread_root_id=""):
            calls.append((agent_id, tenant_key, chat_id, thread_root_id))
            return "team summary"

        def read_l1(self, *_args, **_kwargs):
            pytest.fail("employee Channel must not read full L1")

    query = EmployeeMemoryQuery(
        memory_facade=_Facade(),
        state=DataProjectionState(),
        context_factory=EmployeeDataRequestContextFactory(
            admin_principal_ids=frozenset(),
            main_bot_app_id="main_bot",
            subject_resolver=lambda *_args: _subject(),
        ),
    )

    result = query.query(
        _request(),
        MemoryQuerySpec(
            agent_id="agt_alpha",
            chat_id="forged_chat",
            thread_root_id="forged_root",
        ),
    )

    assert result.content == "team summary"
    assert result.scope == "summary"
    assert calls == [("agt_alpha", "tenant_1", "oc_team", "om_root")]


def test_full_l1_requires_admin_in_main_bot_dm() -> None:
    class _Facade:
        def read_l1(self, agent_id, tenant_key, **_kwargs):
            return f"secret:{tenant_key}:{agent_id}"

        def read_memory_summary(self, *_args, **_kwargs):
            pytest.fail("admin DM requested full L1")

    factory = EmployeeDataRequestContextFactory(
        admin_principal_ids=frozenset({"ou_admin"}),
        main_bot_app_id="main_bot",
        subject_resolver=lambda *_args: _subject(),
    )
    query = EmployeeMemoryQuery(
        memory_facade=_Facade(),
        state=DataProjectionState(),
        context_factory=factory,
    )
    request = AuthenticatedDataRequest(
        principal_id="ou_admin",
        tenant_key="tenant_1",
        receiving_bot_app_id="main_bot",
        chat_id="oc_dm",
        chat_type="p2p",
        thread_root_id="",
        requested_agent_id="agt_alpha",
    )

    result = query.query(
        request,
        MemoryQuerySpec(agent_id="agt_alpha", full_l1=True),
    )

    assert result.scope == "full_l1"
    assert result.content == "secret:tenant_1:agt_alpha"
