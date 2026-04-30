"""EngineCardSender: drop-in replacement for SmartSender using FeishuCardAPIClient directly."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from src.card.delivery.engine import SequenceConflictError, TransportError
from src.card.delivery.feishu_client import FeishuCardAPIClient
from src.card.delivery.sequence import SequenceManager
from src.config import get_settings

logger = logging.getLogger(__name__)


class EngineCardSender:
    """Drop-in replacement for SmartSender that uses FeishuCardAPIClient directly.

    Provides the same interface as SmartSender (check_throttle, update_stream_state,
    check_plan_throttle, update_plan_state, send) but bypasses the handler layer
    and talks to Feishu API via FeishuCardAPIClient.

    Key behaviors:
    - is_update=True + has current_message_id → PATCH via update_card()
    - PATCH fails (300317 or transport error) → re-anchor by creating new message
    - is_update=False or no current_message_id → create new card via create_card()
    - throttle=True → respect min_interval/min_chars thresholds
    - payload_guard → applied before send/patch to truncate oversized cards
    """

    def __init__(
        self,
        client: FeishuCardAPIClient,
        chat_id: str,
        reply_to_message_id: str,
        *,
        settings: Any = None,
        initial_message_id: str | None = None,
        payload_guard: Callable[[str], str] | None = None,
    ) -> None:
        self._client = client
        self.chat_id = chat_id
        self.reply_to_message_id = reply_to_message_id
        self.settings = settings or get_settings()

        self.current_message_id: str | None = initial_message_id
        self.thread_root_message_id: str | None = initial_message_id

        self._payload_guard = payload_guard
        self._sequences = SequenceManager()

        # Throttling state
        self.last_stream_ts: float = 0.0
        self.last_stream_text_len: int = 0
        self.last_plan_ts: float = 0.0
        self.last_plan_content: str = ""

    # ------------------------------------------------------------------
    # Throttle interface
    # ------------------------------------------------------------------

    def check_throttle(
        self,
        text_len: int,
        force: bool = False,
        min_interval: Optional[float] = None,
        min_new_chars: Optional[int] = None,
    ) -> bool:
        """Return True if update should proceed, False if throttled."""
        if force:
            return True

        now = time.monotonic()
        if min_interval is None:
            min_interval = self.settings.deep_stream_interval
        if min_new_chars is None:
            min_new_chars = self.settings.deep_stream_min_chars

        if (now - self.last_stream_ts) < min_interval and (text_len - self.last_stream_text_len) < min_new_chars:
            return False
        return True

    def update_stream_state(self, text_len: int) -> None:
        """Update throttle state after a stream update."""
        self.last_stream_ts = time.monotonic()
        self.last_stream_text_len = text_len

    def check_plan_throttle(self, plan_content: str, force: bool = False, min_interval: float = 1.5) -> bool:
        """Return True if plan update should proceed."""
        if force:
            return True
        now = time.monotonic()
        if plan_content and (plan_content != self.last_plan_content or (now - self.last_plan_ts) > min_interval):
            return True
        return False

    def update_plan_state(self, plan_content: str) -> None:
        """Update plan throttle state."""
        self.last_plan_ts = time.monotonic()
        self.last_plan_content = plan_content

    # ------------------------------------------------------------------
    # Send interface
    # ------------------------------------------------------------------

    def send(
        self,
        card_content: str,
        msg_type: str = "interactive",
        is_update: bool = False,
        throttle: bool = False,
        request_id: Optional[str] = None,
    ) -> Optional[str]:
        """Smart send/patch with auto re-anchoring. Returns message_id."""
        # Apply payload guard for interactive cards
        if msg_type == "interactive" and self._payload_guard:
            card_content = self._payload_guard(card_content)

        # Throttle check: if throttle requested and we have a current message,
        # skip this update (caller should have checked check_throttle() already,
        # but this is a safety net for the send path).
        if throttle and self.current_message_id and is_update:
            if not self.check_throttle(len(card_content), force=False):
                return self.current_message_id

        # 1. Try update existing card
        if is_update and self.current_message_id:
            try:
                card_json = json.loads(card_content) if isinstance(card_content, str) else card_content
                seq = self._sequences.next_sequence(self.current_message_id)
                self._client.update_card(self.current_message_id, card_json, sequence=seq)
                return self.current_message_id
            except SequenceConflictError as e:
                # Raise floor and re-anchor
                self._sequences.raise_floor(self.current_message_id, e.next_floor)
                logger.warning(
                    "EngineCardSender: Sequence conflict for %s (floor=%d), re-anchoring...",
                    self.current_message_id, e.next_floor,
                )
            except TransportError as e:
                logger.warning(
                    "EngineCardSender: Transport error for %s (%s), re-anchoring...",
                    self.current_message_id, e,
                )
            except Exception as e:
                logger.warning(
                    "EngineCardSender: Patch failed for %s (%s), re-anchoring...",
                    self.current_message_id, e,
                )

        # 2. Create new message (re-anchor or first send)
        result_id = self._create_card(card_content)

        if result_id:
            self.current_message_id = result_id
            if not self.thread_root_message_id:
                self.thread_root_message_id = result_id

        return result_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_card(self, card_content: str) -> Optional[str]:
        """Create a new card message via FeishuCardAPIClient."""
        try:
            card_json = json.loads(card_content) if isinstance(card_content, str) else card_content
            reply_to = self.thread_root_message_id or self.reply_to_message_id
            message_id, _ = self._client.create_card(
                self.chat_id, card_json, reply_to=reply_to
            )
            return message_id
        except Exception as e:
            logger.error("EngineCardSender: Failed to create card: %s", e)
            return None
