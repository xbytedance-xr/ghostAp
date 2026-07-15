from types import SimpleNamespace

import pytest

from src.autonomous.membership.lark import (
    LarkMembershipAPI,
    MembershipRemoteRejected,
    MembershipRemoteUnknown,
)
from src.autonomous.membership.models import MembershipOperation


class _ChatMembers:
    def __init__(self, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def create(self, request):
        self.calls.append(("create", request))
        if self.error:
            raise self.error
        return self.response

    def delete(self, request):
        self.calls.append(("delete", request))
        if self.error:
            raise self.error
        return self.response

    def is_in_chat(self, request):
        self.calls.append(("is_in_chat", request))
        if self.error:
            raise self.error
        return self.response


def _client(chat_members: _ChatMembers):
    return SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(chat_members=chat_members)))


def _response(*, success=True, data=None, code=0):
    return SimpleNamespace(
        success=lambda: success,
        data=data,
        code=code,
        msg="safe remote message",
    )


@pytest.mark.parametrize(
    ("operation", "method"),
    [(MembershipOperation.ADD, "create"), (MembershipOperation.REMOVE, "delete")],
)
def test_mutation_uses_app_id_and_one_bot(
    operation: MembershipOperation,
    method: str,
) -> None:
    data = SimpleNamespace(invalid_id_list=[])
    if operation is MembershipOperation.ADD:
        data.not_existed_id_list = []
        data.pending_approval_id_list = []
    members = _ChatMembers(_response(data=data))
    api = LarkMembershipAPI(_client(members), employee_client_provider=lambda *_: None)

    confirmed = api.mutate(operation, chat_id="oc_team", app_id="cli_employee")

    assert confirmed is True
    called_method, request = members.calls[0]
    assert called_method == method
    assert request.chat_id == "oc_team"
    assert request.member_id_type == "app_id"
    assert request.request_body.id_list == ["cli_employee"]
    if operation is MembershipOperation.ADD:
        assert request.succeed_type == 2


def test_success_with_invalid_bot_is_rejected() -> None:
    members = _ChatMembers(
        _response(
            data=SimpleNamespace(
                invalid_id_list=["cli_employee"],
                not_existed_id_list=[],
                pending_approval_id_list=[],
            )
        )
    )
    api = LarkMembershipAPI(_client(members), employee_client_provider=lambda *_: None)

    with pytest.raises(MembershipRemoteRejected, match="invalid_member"):
        api.mutate(MembershipOperation.ADD, chat_id="oc_team", app_id="cli_employee")


def test_transport_exception_is_unknown_not_rejected() -> None:
    api = LarkMembershipAPI(
        _client(_ChatMembers(error=TimeoutError("secret transport detail"))),
        employee_client_provider=lambda *_: None,
    )

    with pytest.raises(MembershipRemoteUnknown, match="membership_mutation_unknown"):
        api.mutate(MembershipOperation.REMOVE, chat_id="oc_team", app_id="cli_employee")


@pytest.mark.parametrize(
    "data",
    [
        None,
        SimpleNamespace(invalid_id_list=""),
        SimpleNamespace(invalid_id_list=[], not_existed_id_list=[]),
    ],
)
def test_mutation_requires_complete_typed_success_evidence(data: object) -> None:
    api = LarkMembershipAPI(
        _client(_ChatMembers(_response(data=data))),
        employee_client_provider=lambda *_: None,
    )

    with pytest.raises(MembershipRemoteUnknown, match="membership_mutation_unknown"):
        api.mutate(MembershipOperation.ADD, chat_id="oc_team", app_id="cli_employee")


def test_reconciliation_uses_target_employee_client() -> None:
    employee_members = _ChatMembers(
        _response(data=SimpleNamespace(is_in_chat=True))
    )
    calls: list[tuple[str, str, str]] = []

    def provider(agent_id: str, app_id: str, credential_ref: str):
        calls.append((agent_id, app_id, credential_ref))
        return _client(employee_members)

    api = LarkMembershipAPI(
        _client(_ChatMembers()),
        employee_client_provider=provider,
    )

    assert api.is_member(
        chat_id="oc_team",
        agent_id="agt_1",
        app_id="cli_employee",
        credential_ref="cred_1",
    ) is True
    assert calls == [("agt_1", "cli_employee", "cred_1")]
    method, request = employee_members.calls[0]
    assert method == "is_in_chat"
    assert request.chat_id == "oc_team"


@pytest.mark.parametrize("value", [None, "yes", 1])
def test_reconciliation_rejects_non_boolean_evidence(value: object) -> None:
    response = _response(data=SimpleNamespace(is_in_chat=value))
    api = LarkMembershipAPI(
        _client(_ChatMembers()),
        employee_client_provider=lambda *_: _client(_ChatMembers(response)),
    )

    with pytest.raises(MembershipRemoteUnknown, match="membership_observation_unknown"):
        api.is_member(
            chat_id="oc_team",
            agent_id="agt_1",
            app_id="cli_employee",
            credential_ref="cred_1",
        )


def test_reconciliation_classifies_permission_denial_without_remote_message() -> None:
    response = _response(success=False, code=99991672)
    api = LarkMembershipAPI(
        _client(_ChatMembers()),
        employee_client_provider=lambda *_: _client(_ChatMembers(response)),
    )

    with pytest.raises(
        MembershipRemoteUnknown,
        match="membership_observation_permission_denied",
    ) as exc_info:
        api.is_member(
            chat_id="oc_team",
            agent_id="agt_1",
            app_id="cli_employee",
            credential_ref="cred_1",
        )

    assert "safe remote message" not in str(exc_info.value)
