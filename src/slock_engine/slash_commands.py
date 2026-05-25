"""Slash command parser for Slock Engine.

Handles /slock subcommands and team/role/task management commands.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


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
    ROLE_INFO_USAGE = "role_info_usage"
    ROLE_MOVE = "role_move"

    # Task management
    TASK_LIST = "task_list"
    TASK_STATUS = "task_status"
    TASK_ASSIGN = "task_assign"  # Deprecated: NLI-only, auto-routed via dispatch loop

    # Discussion
    DISCUSSION = "discussion"
    STOP_DISCUSSION = "stop_discussion"
    DISCUSSION_HISTORY = "discussion_history"
    DISCUSSION_LIST = "discussion_list"
    COUNCIL = "council"

    # Memory management
    MEMORY = "memory"
    MEMORY_LIST = "memory_list"
    MEMORY_GROUP = "memory_group"

    # Plan management
    PLAN_LIST = "plan_list"
    PLAN_DETAIL = "plan_detail"

    # Missing name indicators
    NEW_TEAM_MISSING_NAME = "new_team_missing_name"
    NEW_ROLE_MISSING_NAME = "new_role_missing_name"

    # Non-technical / casual message (filtered out)
    CHITCHAT = "chitchat"

    # Unknown
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SlockCommandResult:
    """Result of is_slock_command check. Supports bool() for backward compatibility."""

    is_command: bool
    action: Optional[SlockCommandAction] = None

    def __bool__(self) -> bool:
        return self.is_command


NEEDS_ACTIVATION = SlockCommandResult(is_command=False, action=SlockCommandAction.ACTIVATE)


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
    "/p": "/plan",
}


def get_all_command_prefixes() -> set[str]:
    """Return all recognized slock command prefixes (canonical + aliases).

    Useful for validating command fix suggestions in card actions.
    """
    canonical = {"/slock", "/slocks", "/new-team", "/new-role", "/council", "/role", "/task", "/team", "/plan"}
    return canonical | set(_ALIASES.keys())


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
        return SlockCommand(action=SlockCommandAction.NEW_TEAM_MISSING_NAME)

    # /new-role <name>
    if cmd == "/new-role":
        if remainder:
            return SlockCommand(action=SlockCommandAction.NEW_ROLE, args=remainder)
        return SlockCommand(action=SlockCommandAction.NEW_ROLE_MISSING_NAME)

    # /council <question>
    if cmd == "/council":
        return SlockCommand(action=SlockCommandAction.COUNCIL, args=remainder)

    # /discuss [stop|history|<topic>] [@agent1 @agent2 ...]
    if cmd == "/discuss":
        return _parse_discuss_args(remainder)

    # /memory [list|group|@agent_name]
    if cmd == "/memory":
        if remainder:
            parts = remainder.split(None, 1)
            subcmd = parts[0].lower()
            if subcmd == "list":
                return SlockCommand(action=SlockCommandAction.MEMORY_LIST)
            if subcmd == "group":
                return SlockCommand(action=SlockCommandAction.MEMORY_GROUP)
            # Otherwise treat as agent name
            target = remainder.lstrip("@").strip()
            return SlockCommand(action=SlockCommandAction.MEMORY, target=target)
        return SlockCommand(action=SlockCommandAction.MEMORY, target="")

    # /role <subcommand>
    if cmd == "/role":
        return _parse_role_subcommand(remainder)

    # /plan [list|<plan_id>]
    if cmd == "/plan":
        if not remainder or remainder.strip().lower() == "list":
            return SlockCommand(action=SlockCommandAction.PLAN_LIST)
        return SlockCommand(action=SlockCommandAction.PLAN_DETAIL, target=remainder.strip())

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


def _parse_discuss_args(text: str) -> SlockCommand:
    """Parse /discuss arguments supporting subcommands and @mentions.

    Syntax:
        /discuss stop [thread_id]     → STOP_DISCUSSION
        /discuss history [n]          → DISCUSSION_HISTORY
        /discuss list                 → DISCUSSION_LIST
        /discuss                      → DISCUSSION_LIST (shows active discussions)
        /discuss <topic> [@agent ...]  → DISCUSSION with extra=comma-separated mentions
    """
    import re

    if not text:
        return SlockCommand(action=SlockCommandAction.DISCUSSION_LIST)

    parts = text.split(None, 1)
    subcmd = parts[0].lower()
    sub_args = parts[1].strip() if len(parts) > 1 else ""

    # /discuss stop [thread_id]
    if subcmd == "stop":
        return SlockCommand(action=SlockCommandAction.STOP_DISCUSSION, args=sub_args)

    # /discuss history [n]
    if subcmd == "history":
        return SlockCommand(action=SlockCommandAction.DISCUSSION_HISTORY, args=sub_args)

    # /discuss list
    if subcmd == "list":
        return SlockCommand(action=SlockCommandAction.DISCUSSION_LIST, args=sub_args)

    # /discuss <topic> @agent1 @agent2
    # Extract @mentions from the full text
    mentions = re.findall(r"@(\w+)", text)
    if mentions:
        # Remove @mentions from topic text
        topic = re.sub(r"\s*@\w+", "", text).strip()
        # Store mentions as comma-separated in extra field
        return SlockCommand(
            action=SlockCommandAction.DISCUSSION,
            args=topic,
            extra=",".join(m.lower() for m in mentions),
        )

    return SlockCommand(action=SlockCommandAction.DISCUSSION, args=text)


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
        if sub_args:
            return SlockCommand(action=SlockCommandAction.ROLE_INFO, target=sub_args)
        return SlockCommand(action=SlockCommandAction.ROLE_INFO_USAGE)
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
        "multi word task" @role
        "multi word task" "Role Name"
        "multi word task" role
        simple_task role
        simple task role   (last word = role, rest = content)
        fix the login bug @coder  (@role explicit syntax)
    """
    import re
    import shlex

    if not text:
        return ("", "")

    # Priority 1: Explicit @role syntax (unambiguous)
    at_match = re.search(r"@(\S+)", text)
    if at_match:
        role = at_match.group(1)
        content = text[: at_match.start()].strip() + " " + text[at_match.end() :].strip()
        content = content.strip()
        return (content, role)

    # Priority 2: shlex parsing with quote support
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
        # Deprecated: tasks are now auto-routed; return UNKNOWN with hint
        return SlockCommand(action=SlockCommandAction.UNKNOWN, args="[deprecated] /task assign 已移除，任务会自动分配给最合适的角色")
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
) -> SlockCommandResult:
    """Check if text is a slock-related command.

    /slock and /new-team are always captured globally.
    /role, /task, /team, /new-role are only captured when the chat is a
    managed slock chat (manager.is_managed_chat(chat_id) is True).

    If *intent_result* is provided (an IntentResult from the NLI router) and
    has high confidence for a management action, this also returns True — even
    without a slash prefix.

    Returns:
        SlockCommandResult with is_command=True when text is a slock command.
        SlockCommandResult with is_command=False when text is not a slock command.
        NEEDS_ACTIVATION when the command needs activation first.
    """
    if not text:
        return SlockCommandResult(is_command=False)
    normalized = text.strip().lower()

    # Always capture /slock and /new-team regardless of chat state
    if normalized.startswith(("/slock", "/new-team")):
        return SlockCommandResult(is_command=True)

    # Team-internal commands require managed chat context
    if normalized.startswith(("/new-role", "/role", "/task", "/team", "/council", "/discuss", "/memory", "/plan")):
        if manager is not None and chat_id:
            if manager.is_managed_chat(chat_id):
                return SlockCommandResult(is_command=True)
            return NEEDS_ACTIVATION
        # No manager context — conservative: don't capture
        return SlockCommandResult(is_command=False)

    # NLI fallback: high-confidence intent classification also counts
    if intent_result is not None:
        from src.config import get_settings
        nli_threshold = get_settings().slock_nli_confidence_threshold
        if (
            intent_result.action != SlockCommandAction.UNKNOWN
            and intent_result.confidence >= nli_threshold
        ):
            return SlockCommandResult(is_command=True, action=intent_result.action)

    return SlockCommandResult(is_command=False)
