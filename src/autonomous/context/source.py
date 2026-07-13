"""Strict employee-scoped Feishu message-source contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .models import ContextMessage, EmployeeMessageScope

if TYPE_CHECKING:
    from ..domain.employees import BotPrincipal


@dataclass(frozen=True)
class ResolvedThread:
    """Validated binding between a root message and a Feishu thread."""

    thread_root_message_id: str
    feishu_thread_id: str
    current_message_id: str


@dataclass(frozen=True)
class MessagePage:
    """One strictly validated page returned by Feishu."""

    messages: tuple[ContextMessage, ...]
    has_more: bool
    page_token: str = ""


class CredentialResolver(Protocol):
    def resolve(self, credential_ref: str, agent_id: str, app_id: str) -> str: ...


class EmployeeClientBuilder(Protocol):
    def __call__(self, *, app_id: str, app_secret: str, timeout: float) -> Any: ...


class EmployeeScopedMessageSource(Protocol):
    scope: EmployeeMessageScope

    def resolve_thread(self) -> ResolvedThread: ...

    def list_thread_messages(
        self, *, page_token: str = "", page_size: int = 50
    ) -> MessagePage: ...

    def list_chat_messages(
        self, *, page_token: str = "", page_size: int = 20
    ) -> MessagePage: ...

    def close(self) -> None: ...

    def __enter__(self) -> EmployeeScopedMessageSource: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class EmployeeMessageSourceFactory(Protocol):
    def open(
        self,
        *,
        scope: EmployeeMessageScope,
        principal: BotPrincipal,
    ) -> EmployeeScopedMessageSource: ...

    def close(self) -> None: ...


__all__ = [
    "CredentialResolver",
    "EmployeeClientBuilder",
    "EmployeeMessageSourceFactory",
    "EmployeeScopedMessageSource",
    "MessagePage",
    "ResolvedThread",
]
