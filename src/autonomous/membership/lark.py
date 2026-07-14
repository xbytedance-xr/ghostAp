"""Official lark-oapi adapter for Bot group membership."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import MembershipOperation


class MembershipRemoteError(RuntimeError):
    """Safe base error that never embeds SDK transport details."""


class MembershipRemoteRejected(MembershipRemoteError):
    """Feishu returned a known rejection or unusable ID result."""


class MembershipRemoteUnknown(MembershipRemoteError):
    """The remote result cannot be proven and must be reconciled."""


EmployeeClientProvider = Callable[[str, str, str], Any]


class LarkMembershipAPI:
    """Mutate with the Manager client; observe with the employee client."""

    def __init__(
        self,
        manager_client: Any,
        *,
        employee_client_provider: EmployeeClientProvider,
    ) -> None:
        if manager_client is None or not callable(employee_client_provider):
            raise TypeError("membership Lark clients are required")
        self._manager = manager_client
        self._employee_client_provider = employee_client_provider

    def mutate(
        self,
        operation: MembershipOperation | str,
        *,
        chat_id: str,
        app_id: str,
    ) -> None:
        try:
            operation_value = MembershipOperation(operation)
        except (TypeError, ValueError):
            raise ValueError("invalid membership operation") from None
        try:
            if operation_value is MembershipOperation.ADD:
                response = self._create(chat_id, app_id)
            else:
                response = self._delete(chat_id, app_id)
            self._require_mutation_success(response)
        except MembershipRemoteError:
            raise
        except Exception:
            raise MembershipRemoteUnknown("membership_mutation_unknown") from None

    def is_member(
        self,
        *,
        chat_id: str,
        agent_id: str,
        app_id: str,
        credential_ref: str,
    ) -> bool:
        try:
            from lark_oapi.api.im.v1 import IsInChatChatMembersRequest

            client = self._employee_client_provider(
                agent_id,
                app_id,
                credential_ref,
            )
            request = IsInChatChatMembersRequest.builder().chat_id(chat_id).build()
            response = client.im.v1.chat_members.is_in_chat(request)
            if not response.success():
                raise MembershipRemoteUnknown("membership_observation_unknown")
            value = getattr(getattr(response, "data", None), "is_in_chat", None)
            if type(value) is not bool:
                raise MembershipRemoteUnknown("membership_observation_unknown")
            return value
        except MembershipRemoteError:
            raise
        except Exception:
            raise MembershipRemoteUnknown("membership_observation_unknown") from None

    def _create(self, chat_id: str, app_id: str) -> Any:
        from lark_oapi.api.im.v1 import (
            CreateChatMembersRequest,
            CreateChatMembersRequestBody,
        )

        body = CreateChatMembersRequestBody.builder().id_list([app_id]).build()
        request = (
            CreateChatMembersRequest.builder()
            .chat_id(chat_id)
            .member_id_type("app_id")
            .succeed_type(2)
            .request_body(body)
            .build()
        )
        return self._manager.im.v1.chat_members.create(request)

    def _delete(self, chat_id: str, app_id: str) -> Any:
        from lark_oapi.api.im.v1 import (
            DeleteChatMembersRequest,
            DeleteChatMembersRequestBody,
        )

        body = DeleteChatMembersRequestBody.builder().id_list([app_id]).build()
        request = (
            DeleteChatMembersRequest.builder()
            .chat_id(chat_id)
            .member_id_type("app_id")
            .request_body(body)
            .build()
        )
        return self._manager.im.v1.chat_members.delete(request)

    @staticmethod
    def _require_mutation_success(response: Any) -> None:
        if response is None or not callable(getattr(response, "success", None)):
            raise MembershipRemoteUnknown("membership_mutation_unknown")
        if response.success() is not True:
            code = getattr(response, "code", None)
            if isinstance(code, int):
                raise MembershipRemoteRejected(f"remote_rejected_{code}")
            raise MembershipRemoteRejected("remote_rejected")
        data = getattr(response, "data", None)
        for field in (
            "invalid_id_list",
            "not_existed_id_list",
            "pending_approval_id_list",
        ):
            values = getattr(data, field, None)
            if values:
                raise MembershipRemoteRejected("invalid_member")


__all__ = [
    "LarkMembershipAPI",
    "MembershipRemoteError",
    "MembershipRemoteRejected",
    "MembershipRemoteUnknown",
]
