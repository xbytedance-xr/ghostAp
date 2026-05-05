"""Chat lock gate — ingress-level interception middleware.

Extracted from ``FeishuWSClient`` to decouple chat-lock interception logic
from the main WebSocket client class.  ``ws_client.py`` calls
``gate.check()`` / ``gate.check_card_action()`` at the message/card-action
entry points — a single-line decision replacing ~110 lines of inline code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

from ..card.ui_text import UI_TEXT

if TYPE_CHECKING:
    from ..chat_lock import ChatLockManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocol: narrow interface injected by ws_client
# ---------------------------------------------------------------------------

@runtime_checkable
class GateHost(Protocol):
    """Minimal interface that ChatLockGate depends on.

    Satisfied by ``FeishuWSClient`` without creating a circular import.
    """

    def _reply_text(self, message_id: str, text: str) -> Optional[str]: ...

    def _add_reaction(self, message_id: str, emoji_type: str) -> None: ...

    def _get_handler(self, name: str) -> Any: ...


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class CheckResult:
    """Outcome of a gate check."""
    blocked: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# ChatLockGate
# ---------------------------------------------------------------------------

class ChatLockGate:
    """Ingress gate for chat-lock interception.

    Parameters
    ----------
    chat_lock_manager:
        The ``ChatLockManager`` singleton (may be ``None`` if not configured).
    dedup_cache:
        A ``MessageCache`` instance for deduplicating lock intercept cards
        within a 30-second window per (chat, sender).
    host:
        Narrow protocol reference to the ``FeishuWSClient`` for reply/reaction
        fallback paths.
    """

    def __init__(
        self,
        chat_lock_manager: Optional[ChatLockManager],
        dedup_cache: Any,  # MessageCache
        host: GateHost,
    ) -> None:
        self._clm = chat_lock_manager
        self._dedup = dedup_cache
        self._host = host

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        chat_id: str,
        sender_id: str,
        message_id: str,
        *,
        command: str | None = None,
        raw_text: str = "",
    ) -> bool:
        """Check whether a *message* should be blocked by the chat lock.

        Returns ``True`` if blocked (caller should ``return``).
        Uses fail-close semantics: exceptions → non-admin in locked chat blocked.
        """
        return self._fail_close_check(
            chat_id, sender_id, message_id,
            command=command, raw_text=raw_text,
            is_card_action=False, action_type="",
        )

    def check_card_action(
        self,
        chat_id: str,
        sender_id: str,
        message_id: str,
        *,
        action_type: str = "",
    ) -> bool:
        """Check whether a *card action* should be blocked by the chat lock.

        Returns ``True`` if blocked.
        """
        return self._fail_close_check(
            chat_id, sender_id, message_id,
            is_card_action=True, action_type=action_type,
        )

    def close(self) -> None:
        """Stop the dedup cache cleanup thread."""
        try:
            self._dedup.stop_cleanup_thread()
        except Exception:
            logger.debug("failed to stop dedup cleanup thread", exc_info=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _should_send_intercept(self, chat_id: str, sender_id: str) -> bool:
        """Return True if a lock intercept card should be sent (dedup)."""
        key = f"lock_block:{chat_id}:{sender_id}"
        return not self._dedup.is_duplicate(key)

    def _fail_close_check(
        self,
        chat_id: str,
        sender_id: str,
        message_id: str,
        *,
        command: str | None = None,
        raw_text: str = "",
        is_card_action: bool = False,
        action_type: str = "",
    ) -> bool:
        """Fail-close chat-lock interception (shared by message + card paths)."""
        clm = self._clm
        if clm is None:
            return False

        _is_admin = clm.is_admin(sender_id)

        try:
            if self._try_block(
                chat_id, sender_id, message_id,
                command=command, raw_text=raw_text,
                is_card_action=is_card_action, action_type=action_type,
            ):
                return True
        except Exception as exc:
            logger.warning("ChatLock interception error (fail-close): %s", str(exc))
            if not _is_admin and clm.is_locked(chat_id):
                if not is_card_action:
                    if self._should_send_intercept(chat_id, sender_id):
                        _handler = self._host._get_handler("system")
                        if _handler:
                            _handler.send_chat_lock_intercept_card(message_id, chat_id, clm)
                        else:
                            try:
                                self._host._reply_text(message_id, UI_TEXT["chat_locked_fallback"])
                            except Exception:
                                logger.debug("failed to reply lock message", exc_info=True)
                return True

        return False

    def _try_block(
        self,
        chat_id: str,
        sender_id: str,
        message_id: str,
        *,
        command: str | None = None,
        raw_text: str = "",
        is_card_action: bool = False,
        action_type: str = "",
    ) -> bool:
        """Core blocking decision + card dispatch."""
        clm = self._clm
        if clm is None:
            return False

        if is_card_action:
            blocked = clm.should_block_card_action(chat_id, sender_id, action_type)
        else:
            blocked = clm.should_block(chat_id, sender_id, command=command, raw_text=raw_text)

        if not blocked:
            return False

        _handler = self._host._get_handler("system")
        if self._should_send_intercept(chat_id, sender_id):
            if _handler:
                _handler.send_chat_lock_intercept_card(message_id, chat_id, clm)
            else:
                try:
                    self._host._reply_text(message_id, UI_TEXT["chat_locked_fallback"])
                except Exception:
                    logger.debug("failed to reply lock fallback message", exc_info=True)
        else:
            if _handler:
                _handler.send_chat_lock_throttled_reply(message_id, chat_id, clm)
            else:
                try:
                    from .emoji import EmojiReaction
                    self._host._add_reaction(message_id, EmojiReaction.on_chat_locked())
                except Exception:
                    logger.debug("failed to add lock reaction", exc_info=True)
        return True
