"""ACPStreamBridge: normalizes ACP streaming into CardEvent sequences.

Public component in the session layer. Implements the StreamBridge protocol.
Used by Deep/Loop/Spec renderers for consistent ACP event → CardEvent conversion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.acp.models import ACPEvent
    from src.card.protocols import Dispatchable


class ACPStreamBridge:
    """Normalize ACP streaming into programming-mode-like card blocks.

    Reused by Deep/Loop/Spec renderers so text, reasoning and tool panels follow
    the same event sequencing as direct programming mode.

    Implements the StreamBridge protocol defined in src.card.protocols.
    """

    def __init__(self, dispatchable: Dispatchable) -> None:
        self._dispatchable: Dispatchable = dispatchable
        self._text_active: bool = False
        self._reasoning_active: bool = False

    def bind(self, dispatchable: Dispatchable) -> None:
        """Rebind to a new dispatchable target; closes open blocks first."""
        self.close_open_blocks()
        self._dispatchable = dispatchable
        self._text_active = False
        self._reasoning_active = False

    def on_event(self, acp_event: ACPEvent) -> None:
        """Process an ACP event and dispatch corresponding CardEvents."""
        from src.acp import ACPEventType
        from src.card.events import CardEvent, card_event_from_acp

        if acp_event.event_type == ACPEventType.THOUGHT_CHUNK:
            if self._text_active:
                self._dispatchable.dispatch(CardEvent.text_done("_active_text"))
                self._text_active = False
            if not self._reasoning_active:
                self._dispatchable.dispatch(CardEvent.reasoning_started("_active_reasoning"))
                self._reasoning_active = True
        elif acp_event.event_type == ACPEventType.TEXT_CHUNK:
            if self._reasoning_active:
                self._dispatchable.dispatch(CardEvent.reasoning_done("_active_reasoning"))
                self._reasoning_active = False
            if not self._text_active:
                self._dispatchable.dispatch(CardEvent.text_started("_active_text"))
                self._text_active = True
        elif acp_event.event_type == ACPEventType.TOOL_CALL_START:
            self.close_open_blocks()

        self._dispatchable.dispatch(card_event_from_acp(acp_event))

    def close_open_blocks(self) -> None:
        """Close any currently open reasoning or text blocks."""
        from src.card.events import CardEvent

        if self._reasoning_active:
            self._dispatchable.dispatch(CardEvent.reasoning_done("_active_reasoning"))
            self._reasoning_active = False
        if self._text_active:
            self._dispatchable.dispatch(CardEvent.text_done("_active_text"))
            self._text_active = False
