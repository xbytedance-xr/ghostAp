"""ACPStreamBridge: normalizes ACP streaming into CardEvent sequences.

Public component in the session layer. Implements the StreamBridge protocol.
Used by Deep/Spec renderers for consistent ACP event → CardEvent conversion.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.acp.models import ACPEvent
    from src.card.protocols import Dispatchable


class ACPStreamBridge:
    """Normalize ACP streaming into programming-mode-like card blocks.

    Reused by Deep/Spec renderers so text, reasoning and tool panels follow
    the same event sequencing as direct programming mode.

    Implements the StreamBridge protocol defined in src.card.protocols.

    Thread-safety: a lock serializes on_event / bind / close_open_blocks so
    that concurrent calls (e.g. orchestrator broadcast timer vs. event loop)
    cannot interleave state mutations and dispatch calls.
    """

    def __init__(self, dispatchable: Dispatchable) -> None:
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._dispatchable: Dispatchable = dispatchable
        self._text_active: bool = False
        self._reasoning_active: bool = False
        # Per-turn counters generate unique block IDs at hard stream
        # boundaries. Soft alternation between text and reasoning keeps the
        # same logical blocks so Feishu cards do not fragment into one-token
        # markdown/panel rows.
        self._text_turn_seq: int = 0
        self._reasoning_turn_seq: int = 0
        self._active_text_block_id: str = "_active_text"
        self._active_reasoning_block_id: str = "_active_reasoning"

    def bind(self, dispatchable: Dispatchable) -> None:
        """Rebind to a new dispatchable target; closes open blocks first."""
        with self._lock:
            self._close_open_blocks_locked()
            self._dispatchable = dispatchable
            self._text_active = False
            self._reasoning_active = False

    def on_event(self, acp_event: ACPEvent) -> None:
        """Process an ACP event and dispatch corresponding CardEvents."""
        from src.acp import ACPEventType
        from src.card.events import CardEvent, card_event_from_acp

        with self._lock:
            if acp_event.event_type == ACPEventType.THOUGHT_CHUNK:
                self._ensure_reasoning_block_locked()
            elif acp_event.event_type == ACPEventType.TEXT_CHUNK:
                self._ensure_text_block_locked()
            elif acp_event.event_type == ACPEventType.TOOL_CALL_START:
                self._close_open_blocks_locked()

            # Override block_id in the converted CardEvent to match our per-turn ID
            ce = card_event_from_acp(acp_event)
            if acp_event.event_type == ACPEventType.THOUGHT_CHUNK and ce.payload.get("block_id"):
                ce = CardEvent(type=ce.type, payload={**ce.payload, "block_id": self._active_reasoning_block_id})
            elif acp_event.event_type == ACPEventType.TEXT_CHUNK and ce.payload.get("block_id"):
                ce = CardEvent(type=ce.type, payload={**ce.payload, "block_id": self._active_text_block_id})
            self._dispatchable.dispatch(ce)

    def close_open_blocks(self) -> None:
        """Close any currently open reasoning or text blocks."""
        with self._lock:
            self._close_open_blocks_locked()

    def _close_open_blocks_locked(self) -> None:
        """Internal: close open blocks while holding self._lock."""
        from src.card.events import CardEvent

        if self._reasoning_active:
            self._dispatchable.dispatch(CardEvent.reasoning_done(self._active_reasoning_block_id))
            self._reasoning_active = False
        if self._text_active:
            self._dispatchable.dispatch(CardEvent.text_done(self._active_text_block_id))
            self._text_active = False

    def _ensure_text_block_locked(self) -> None:
        """Open the current logical text block if needed."""
        from src.card.events import CardEvent

        if self._text_active:
            return
        self._text_turn_seq += 1
        bid = (
            f"_turn_{self._text_turn_seq}_text"
            if self._text_turn_seq > 1
            else "_active_text"
        )
        self._active_text_block_id = bid
        self._dispatchable.dispatch(CardEvent.text_started(bid))
        self._text_active = True

    def _ensure_reasoning_block_locked(self) -> None:
        """Open the current logical reasoning block if needed."""
        from src.card.events import CardEvent

        if self._reasoning_active:
            return
        self._reasoning_turn_seq += 1
        bid = (
            f"_turn_{self._reasoning_turn_seq}_reasoning"
            if self._reasoning_turn_seq > 1
            else "_active_reasoning"
        )
        self._active_reasoning_block_id = bid
        self._dispatchable.dispatch(CardEvent.reasoning_started(bid))
        self._reasoning_active = True
