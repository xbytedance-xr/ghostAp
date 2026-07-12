"""Slash Command Manager: register/diff/cleanup employee bot commands."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlashCommand:
    """One registered slash command definition."""

    name: str
    description: str
    usage_hint: str = ""


EMPLOYEE_COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/task", "Assign a task to this employee", "/task <description>"),
    SlashCommand("/status", "View current work status", "/status"),
    SlashCommand("/history", "View recent execution records", "/history [days]"),
    SlashCommand("/memory", "View memory summary", "/memory"),
    SlashCommand("/stop", "Stop current task execution", "/stop"),
)


class SlashCommandAPIError(RuntimeError):
    """Feishu Slash Command API call failed."""


class SlashCommandAPI(Protocol):
    """Port for Feishu Slash Command OpenAPI."""

    def list_commands(self, app_id: str) -> list[dict[str, Any]]: ...

    def create_command(
        self, app_id: str, *, name: str, description: str, usage_hint: str
    ) -> str: ...

    def delete_command(self, app_id: str, command_id: str) -> bool: ...


@dataclass
class CommandSyncResult:
    """Outcome of slash command synchronization."""

    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class SlashCommandManager:
    """Manages desired slash command state for employee bots."""

    def __init__(self, api: SlashCommandAPI) -> None:
        self._api = api

    def sync_commands(
        self,
        app_id: str,
        desired: tuple[SlashCommand, ...] = EMPLOYEE_COMMANDS,
    ) -> CommandSyncResult:
        """Sync desired commands with server state: GET→diff→POST/DELETE→GET verify."""
        result = CommandSyncResult()
        try:
            existing = self._api.list_commands(app_id)
        except Exception as exc:
            result.errors.append(f"list failed: {exc}")
            return result
        existing_names = {cmd.get("name", ""): cmd for cmd in existing}
        desired_names = {cmd.name for cmd in desired}
        for cmd in desired:
            if cmd.name in existing_names:
                result.unchanged.append(cmd.name)
                continue
            try:
                cmd_id = self._api.create_command(
                    app_id,
                    name=cmd.name,
                    description=cmd.description,
                    usage_hint=cmd.usage_hint,
                )
                result.created.append(cmd.name)
            except Exception as exc:
                result.errors.append(f"create {cmd.name}: {exc}")
        for name, cmd_data in existing_names.items():
            if name not in desired_names:
                cmd_id = cmd_data.get("id", "")
                if cmd_id:
                    try:
                        self._api.delete_command(app_id, cmd_id)
                        result.deleted.append(name)
                    except Exception as exc:
                        result.errors.append(f"delete {name}: {exc}")
        return result

    def cleanup_all(self, app_id: str) -> CommandSyncResult:
        """Remove all slash commands for a fired employee."""
        result = CommandSyncResult()
        try:
            existing = self._api.list_commands(app_id)
        except Exception as exc:
            result.errors.append(f"list failed: {exc}")
            return result
        for cmd_data in existing:
            cmd_id = cmd_data.get("id", "")
            name = cmd_data.get("name", "")
            if cmd_id:
                try:
                    self._api.delete_command(app_id, cmd_id)
                    result.deleted.append(name)
                except Exception as exc:
                    result.errors.append(f"delete {name}: {exc}")
        return result
