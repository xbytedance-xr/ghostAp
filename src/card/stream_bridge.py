"""ACPStreamBridge: normalizes ACP streaming into CardEvent sequences.

Public component in the session layer. Implements the StreamBridge protocol.
Used by Deep/Spec renderers for consistent ACP event → CardEvent conversion.
"""

from __future__ import annotations

import re
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
        self._text_blocks_by_source: dict[str, str] = {}
        self._reasoning_blocks_by_source: dict[str, str] = {}
        self._active_text_sources: set[str] = set()
        self._active_reasoning_sources: set[str] = set()

    def bind(self, dispatchable: Dispatchable) -> None:
        """Rebind to a new dispatchable target; closes open blocks first."""
        with self._lock:
            self._close_open_blocks_locked()
            self._dispatchable = dispatchable
            self._text_active = False
            self._reasoning_active = False
            self._text_blocks_by_source.clear()
            self._reasoning_blocks_by_source.clear()
            self._active_text_sources.clear()
            self._active_reasoning_sources.clear()

    def on_event(self, acp_event: ACPEvent) -> None:
        """Process an ACP event and dispatch corresponding CardEvents."""
        from src.acp import ACPEventType
        from src.card.events import CardEvent, card_event_from_acp

        with self._lock:
            source_key = self._source_key(acp_event)
            if acp_event.event_type == ACPEventType.THOUGHT_CHUNK:
                reasoning_block_id = self._ensure_reasoning_block_locked(source_key)
            elif acp_event.event_type == ACPEventType.TEXT_CHUNK:
                text_block_id = self._ensure_text_block_locked(source_key)
            elif acp_event.event_type == ACPEventType.TOOL_CALL_START:
                self._close_text_blocks_locked()

            # Override block_id in the converted CardEvent to match our per-turn ID
            ce = card_event_from_acp(acp_event)
            if acp_event.event_type == ACPEventType.THOUGHT_CHUNK and ce.payload.get("block_id"):
                ce = CardEvent(type=ce.type, payload={**ce.payload, "block_id": reasoning_block_id})
            elif acp_event.event_type == ACPEventType.TEXT_CHUNK and ce.payload.get("block_id"):
                ce = CardEvent(type=ce.type, payload={**ce.payload, "block_id": text_block_id})
            self._dispatchable.dispatch(ce)

    def close_open_blocks(self) -> None:
        """Close any currently open reasoning or text blocks."""
        with self._lock:
            self._close_open_blocks_locked()

    def _close_open_blocks_locked(self) -> None:
        """Internal: close open blocks while holding self._lock."""
        from src.card.events import CardEvent

        for source_key in list(self._active_reasoning_sources):
            block_id = self._reasoning_blocks_by_source.get(source_key, self._active_reasoning_block_id)
            self._dispatchable.dispatch(CardEvent.reasoning_done(block_id))
        self._active_reasoning_sources.clear()
        self._reasoning_active = False
        self._close_text_blocks_locked()

    def _close_text_blocks_locked(self) -> None:
        """Close answer text at a tool boundary without fragmenting reasoning."""
        from src.card.events import CardEvent

        for source_key in list(self._active_text_sources):
            block_id = self._text_blocks_by_source.get(source_key, self._active_text_block_id)
            self._dispatchable.dispatch(CardEvent.text_done(block_id))
        self._active_text_sources.clear()
        self._text_active = False

    def _ensure_text_block_locked(self, source_key: str) -> str:
        """Open the current logical text block if needed."""
        from src.card.events import CardEvent

        if source_key in self._active_text_sources:
            return self._text_blocks_by_source.get(source_key, self._active_text_block_id)
        self._text_turn_seq += 1
        bid = self._block_id("text", self._text_turn_seq, source_key)
        self._active_text_block_id = bid
        self._text_blocks_by_source[source_key] = bid
        self._dispatchable.dispatch(CardEvent.text_started(bid))
        self._active_text_sources.add(source_key)
        self._text_active = True
        return bid

    def _ensure_reasoning_block_locked(self, source_key: str) -> str:
        """Open the current logical reasoning block if needed."""
        from src.card.events import CardEvent

        if source_key in self._active_reasoning_sources:
            return self._reasoning_blocks_by_source.get(source_key, self._active_reasoning_block_id)
        bid = self._reasoning_blocks_by_source.get(source_key)
        if bid is None:
            self._reasoning_turn_seq += 1
            bid = self._block_id("reasoning", self._reasoning_turn_seq, source_key)
        self._active_reasoning_block_id = bid
        self._reasoning_blocks_by_source[source_key] = bid
        self._dispatchable.dispatch(CardEvent.reasoning_started(bid))
        self._active_reasoning_sources.add(source_key)
        self._reasoning_active = True
        return bid

    @staticmethod
    def _source_key(acp_event: ACPEvent) -> str:
        source_id = getattr(acp_event, "source_id", None)
        if source_id and isinstance(source_id, str):
            return source_id.strip() or "main"
        return "main"

    @staticmethod
    def _safe_suffix(source_key: str) -> str:
        suffix = re.sub(r"[^a-zA-Z0-9_-]+", "_", source_key).strip("_")
        return suffix[:40] or "main"

    def _block_id(self, kind: str, seq: int, source_key: str) -> str:
        if source_key == "main":
            return f"_turn_{seq}_{kind}" if seq > 1 else f"_active_{kind}"
        return f"_turn_{seq}_{kind}_{self._safe_suffix(source_key)}"
