"""Typed dispatch context for card action routing."""

from __future__ import annotations

from typing import Any, Protocol


class ProjectLookup(Protocol):
    def get_project_for_chat(self, project_id: str, chat_id: str) -> Any: ...


class DispatchContext:
    """Narrow dependency surface used by action dispatch registration."""

    def __init__(self, *, project_manager: ProjectLookup) -> None:
        self._project_manager = project_manager

    def resolve_project(self, project_id: str | None, chat_id: str) -> Any:
        return self._project_manager.get_project_for_chat(project_id, chat_id) if project_id else None

