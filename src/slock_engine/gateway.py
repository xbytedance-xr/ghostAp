"""Slock Gateway — single entry point for all slock interactions from the dispatch layer.

External modules (dispatcher, ws_client) should ONLY import from this module,
never from internal slock_engine submodules. This guarantees that slock bugs
or disablement cannot crash the main message pipeline.

Public API:
    - is_slock_command(text, chat_id) -> SlockCommandResult | None
    - classify_message(text, managed_chat) -> SlockClassification
    - attempt_autonomous_resolve(text, message_id, chat_id) -> ResolveOutcome
    - build_activation_denied_card(reason, hint) -> dict
    - build_clarification_card(...) -> dict
    - NEEDS_ACTIVATION sentinel
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SlockMessageClass(str, Enum):
    TASK = "task"
    CHAT = "chat"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class SlockClassification:
    label: SlockMessageClass
    confidence: float = 1.0


@dataclass(frozen=True)
class ResolveOutcome:
    resolved: bool
    resolved_text: Optional[str] = None


# Re-export NEEDS_ACTIVATION so dispatcher doesn't import from slash_commands
from .slash_commands import NEEDS_ACTIVATION  # noqa: E402, F401


def is_slock_command(text: str, chat_id: str = "") -> Any:
    """Check if text is a slock slash command.

    Returns a truthy SlockCommandResult if it is a command,
    NEEDS_ACTIVATION if the command requires activation first,
    or a falsy value otherwise.
    """
    try:
        from .slash_commands import SlockSlashCommandParser
        return SlockSlashCommandParser.parse(text, chat_id=chat_id)
    except Exception as e:
        logger.warning("Slock command parsing failed (non-fatal): %s", e)
        return None


def classify_message(text: str, *, managed_chat: bool = False) -> SlockClassification:
    """Classify whether a message is a task, chat, or uncertain.

    In managed chats, uncertain is promoted to task to reduce friction.
    """
    try:
        from .task_classifier import TaskClassifier
        classification, _ = TaskClassifier.classify_with_uncertainty(
            text or "", managed_chat=managed_chat,
        )
        if managed_chat and classification == "uncertain":
            classification = "task"
        return SlockClassification(label=SlockMessageClass(classification))
    except Exception as e:
        logger.warning("Slock message classification failed (non-fatal): %s", e)
        return SlockClassification(label=SlockMessageClass.CHAT)


def attempt_autonomous_resolve(
    text: str,
    message_id: str,
    chat_id: str,
) -> ResolveOutcome:
    """Attempt autonomous resolution for uncertain messages.

    Returns ResolveOutcome indicating success/failure without exposing
    internal resolver types.
    """
    try:
        from ..utils.async_helpers import run_async
        from .autonomous_resolver import AutonomousResolver, ResolveStatus

        resolver = AutonomousResolver()
        result = run_async(
            resolver.attempt_resolve(
                task_text=text or "",
                context="",
                task_id=message_id,
                channel_id=chat_id,
            )
        )
        if result and result.status == ResolveStatus.RESOLVED:
            return ResolveOutcome(resolved=True, resolved_text=result.resolved_text)
        return ResolveOutcome(resolved=False)
    except Exception as e:
        logger.warning("Autonomous resolution failed (non-fatal): %s", e)
        return ResolveOutcome(resolved=False)


def build_activation_denied_card(reason: str, hint: str = "") -> dict:
    """Build a card for when activation is denied."""
    try:
        from .card_templates.queue_feedback import (
            build_activation_denied_card as _build,
        )
        return _build(reason=reason, hint=hint)
    except Exception as e:
        logger.warning("Failed to build activation denied card: %s", e)
        return {}


def build_clarification_card(
    message_preview: str,
    channel_id: str,
    message_id: str,
    sender_id: str,
) -> dict:
    """Build a card asking user to clarify their intent."""
    try:
        from .card_templates.queue_feedback import (
            build_clarification_card as _build,
        )
        return _build(
            message_preview=message_preview,
            channel_id=channel_id,
            message_id=message_id,
            sender_id=sender_id,
        )
    except Exception as e:
        logger.warning("Failed to build clarification card: %s", e)
        return {}
