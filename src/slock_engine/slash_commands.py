"""Slash command parser for Slock Engine.

Handles /slock subcommands and team/role/task management commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SlockCommandAction(Enum):
    """Actions recognized by the slock command parser."""

    # /slock entry and status
    ACTIVATE = "activate"
    STATUS = "status"
    STOP = "stop"
    HELP = "help"

    # Team management
    NEW_TEAM = "new_team"
    TEAM_LIST = "team_list"
    TEAM_STATUS = "team_status"
    TEAM_DISSOLVE = "team_dissolve"

    # Role/Agent management
    NEW_ROLE = "new_role"
    ROLE_LIST = "role_list"
    ROLE_REMOVE = "role_remove"
    ROLE_INFO = "role_info"
    ROLE_MOVE = "role_move"

    # Task management
    TASK_LIST = "task_list"
    TASK_ASSIGN = "task_assign"
    TASK_STATUS = "task_status"

    # Discussion
    DISCUSSION = "discussion"
    COUNCIL = "council"

    # Unknown
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SlockCommand:
    """Parsed slock command result."""

    action: SlockCommandAction
    args: str = ""
    target: str = ""  # target name for role/team/task operations
    extra: str = ""  # additional argument (e.g., role name for task assign)


# Command aliases for quick access
_ALIASES: dict[str, str] = {
    "/r": "/role",
    "/t": "/task",
    "/tm": "/team",
    "/c": "/council",
    "/nr": "/new-role",
    "/nt": "/new-team",
    "/s": "/slock",
}


def parse_slock_command(text: str) -> SlockCommand:
    """Parse a slock-related command from raw text.

    Recognizes:
        /slock [status|help|council]
        /council <question>
        /new-team <name>
        /new-role <name>
        /role list|remove <name>|info <name>
        /task list|assign <task> <role>|status
        /team list|status <name>|dissolve <name>
    """
    normalized = text.strip()
    if not normalized:
        return SlockCommand(action=SlockCommandAction.UNKNOWN)

    # Split into command and arguments
    parts = normalized.split(None, 1)
    cmd = parts[0].lower()
    remainder = parts[1].strip() if len(parts) > 1 else ""

    # Resolve aliases
    if cmd in _ALIASES:
        cmd = _ALIASES[cmd]
        # Reconstruct normalized with resolved command for downstream parsing
        normalized = f"{cmd} {remainder}".strip() if remainder else cmd

    # /slock [subcommand]
    if cmd == "/slock":
        return _parse_slock_subcommand(remainder)

    # /slocks
    if cmd == "/slocks":
        return SlockCommand(action=SlockCommandAction.TEAM_LIST)

    # /new-team <name>
    if cmd == "/new-team":
        if remainder:
            return SlockCommand(action=SlockCommandAction.NEW_TEAM, args=remainder)
        return SlockCommand(action=SlockCommandAction.NEW_TEAM)

    # /new-role <name>
    if cmd == "/new-role":
        if remainder:
            return SlockCommand(action=SlockCommandAction.NEW_ROLE, args=remainder)
        return SlockCommand(action=SlockCommandAction.NEW_ROLE)

    # /council <question>
    if cmd == "/council":
        return SlockCommand(action=SlockCommandAction.COUNCIL, args=remainder)

    # /role <subcommand>
    if cmd == "/role":
        return _parse_role_subcommand(remainder)

    # /task <subcommand>
    if cmd == "/task":
        return _parse_task_subcommand(remainder)

    # /team <subcommand>
    if cmd == "/team":
        return _parse_team_subcommand(remainder)

    return SlockCommand(action=SlockCommandAction.UNKNOWN, args=normalized)


def _parse_slock_subcommand(args: str) -> SlockCommand:
    """Parse /slock subcommands."""
    if not args:
        return SlockCommand(action=SlockCommandAction.ACTIVATE)

    sub = args.split(None, 1)
    subcmd = sub[0].lower()

    if subcmd == "status":
        return SlockCommand(action=SlockCommandAction.STATUS)
    elif subcmd == "stop":
        return SlockCommand(action=SlockCommandAction.STOP)
    elif subcmd == "help":
        return SlockCommand(action=SlockCommandAction.HELP)
    elif subcmd in {"list", "team", "teams"}:
        return SlockCommand(action=SlockCommandAction.TEAM_LIST)
    elif subcmd == "council":
        question = sub[1].strip() if len(sub) > 1 else ""
        return SlockCommand(action=SlockCommandAction.COUNCIL, args=question)
    else:
        # Treat as activate with requirement text
        return SlockCommand(action=SlockCommandAction.ACTIVATE, args=args)


def _parse_role_subcommand(args: str) -> SlockCommand:
    """Parse /role subcommands."""
    if not args:
        return SlockCommand(action=SlockCommandAction.ROLE_LIST)

    sub = args.split(None, 1)
    subcmd = sub[0].lower()
    sub_args = sub[1].strip() if len(sub) > 1 else ""

    if subcmd == "list":
        return SlockCommand(action=SlockCommandAction.ROLE_LIST)
    elif subcmd == "remove":
        return SlockCommand(action=SlockCommandAction.ROLE_REMOVE, target=sub_args)
    elif subcmd == "info":
        return SlockCommand(action=SlockCommandAction.ROLE_INFO, target=sub_args)
    elif subcmd == "move":
        # /role move <agent_name> <target_team>
        # Supports quoted multi-word arguments: /role move "Coder Alpha" "前端团队"
        import shlex

        agent_name = ""
        target_team = ""
        if sub_args:
            try:
                tokens = shlex.split(sub_args)
            except ValueError:
                # Unclosed quote — return empty to let caller show usage hint
                tokens = None

            if tokens and len(tokens) >= 2:
                target_team = tokens[-1]
                agent_name = " ".join(tokens[:-1])
            elif tokens and len(tokens) == 1:
                agent_name = tokens[0]

        return SlockCommand(
            action=SlockCommandAction.ROLE_MOVE,
            target=agent_name,
            args=target_team,
        )
    else:
        # Treat first word as subcommand target
        return SlockCommand(action=SlockCommandAction.ROLE_INFO, target=subcmd)


def _parse_assign_args(text: str) -> tuple[str, str]:
    """Parse /task assign arguments, supporting quoted multi-word values.

    Returns (content, role_name) tuple. Supports:
        "multi word task" "Role Name"
        "multi word task" role
        simple_task role
        simple task role   (last word = role, rest = content)
    """
    import shlex

    if not text:
        return ("", "")

    # Try shlex parsing first for proper quote handling
    try:
        tokens = shlex.split(text)
    except ValueError:
        tokens = None

    if tokens and len(tokens) >= 2:
        # Last token is role, everything else is content
        role = tokens[-1]
        content = " ".join(tokens[:-1])
        return (content, role)
    elif tokens and len(tokens) == 1:
        return (tokens[0], "")

    # Fallback: split on last whitespace
    parts = text.rsplit(None, 1)
    if len(parts) == 2:
        return (parts[0], parts[1])
    return (text, "")


def _parse_task_subcommand(args: str) -> SlockCommand:
    """Parse /task subcommands."""
    if not args:
        return SlockCommand(action=SlockCommandAction.TASK_LIST)

    sub = args.split(None, 1)
    subcmd = sub[0].lower()
    sub_args = sub[1].strip() if len(sub) > 1 else ""

    if subcmd == "list":
        return SlockCommand(action=SlockCommandAction.TASK_LIST)
    elif subcmd == "status":
        return SlockCommand(action=SlockCommandAction.TASK_STATUS)
    elif subcmd == "assign":
        # /task assign <content> <role_name>
        # Supports quoted multi-word arguments:
        #   /task assign "implement login" "Coder Alpha"
        #   /task assign implement_login coder
        content, role = _parse_assign_args(sub_args)
        if content and role:
            return SlockCommand(
                action=SlockCommandAction.TASK_ASSIGN,
                args=content,
                target=role,
            )
        return SlockCommand(action=SlockCommandAction.TASK_ASSIGN, args=sub_args)
    else:
        return SlockCommand(action=SlockCommandAction.TASK_LIST)


def _parse_team_subcommand(args: str) -> SlockCommand:
    """Parse /team subcommands."""
    if not args:
        return SlockCommand(action=SlockCommandAction.TEAM_LIST)

    sub = args.split(None, 1)
    subcmd = sub[0].lower()
    sub_args = sub[1].strip() if len(sub) > 1 else ""

    if subcmd == "list":
        return SlockCommand(action=SlockCommandAction.TEAM_LIST)
    elif subcmd == "status":
        return SlockCommand(action=SlockCommandAction.TEAM_STATUS, target=sub_args)
    elif subcmd == "dissolve":
        return SlockCommand(action=SlockCommandAction.TEAM_DISSOLVE, target=sub_args)
    else:
        return SlockCommand(action=SlockCommandAction.TEAM_STATUS, target=subcmd)


def is_slock_command(
    text: str,
    chat_id: str | None = None,
    manager=None,
    *,
    intent_result=None,
) -> bool:
    """Check if text is a slock-related command.

    /slock and /new-team are always captured globally.
    /role, /task, /team, /new-role are only captured when the chat is a
    managed slock chat (manager.is_managed_chat(chat_id) is True).

    If *intent_result* is provided (an IntentResult from the NLI router) and
    has high confidence for a management action, this also returns True — even
    without a slash prefix.
    """
    if not text:
        return False
    normalized = text.strip().lower()

    # Always capture /slock and /new-team regardless of chat state
    if normalized.startswith(("/slock", "/new-team")):
        return True

    # Team-internal commands require managed chat context
    if normalized.startswith(("/new-role", "/role", "/task", "/team", "/council")):
        if manager is not None and chat_id:
            return manager.is_managed_chat(chat_id)
        # No manager context — conservative: don't capture
        return False

    # NLI fallback: high-confidence intent classification also counts
    if intent_result is not None:
        from src.config import get_settings
        nli_threshold = get_settings().slock_nli_confidence_threshold
        if (
            intent_result.action != SlockCommandAction.UNKNOWN
            and intent_result.confidence >= nli_threshold
        ):
            return True

    return False
