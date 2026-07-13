"""Typed command boundary between the main Bot and employee provisioning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmployeeHireRequest:
    employee_name: str
    tool: str
    model: str
    effort: str
    chat_id: str
    message_id: str
    requester_principal_id: str


class EmployeeHireService(Protocol):
    def start_hire(self, request: EmployeeHireRequest) -> None:
        """Start a durable hire workflow and deliver its link asynchronously."""
        ...


__all__ = ["EmployeeHireRequest", "EmployeeHireService"]
