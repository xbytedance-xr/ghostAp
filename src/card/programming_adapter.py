"""Programming mode adapter: bridges streaming card pattern to CardSession.

Provides a drop-in replacement for the StreamingCardManager usage in
ProgrammingHandler.handle_response(). Supports all programming modes:
Coco/Claude/Aiden/Codex/Gemini/TTADK.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.render.budget import RenderBudget
from src.card.session import CardSession
from src.card.state.models import CardMetadata

if TYPE_CHECKING:
    from src.acp.models import ACPEvent
    from src.mode.manager import InteractionMode

logger = logging.getLogger(__name__)

# Mode name → (mode_emoji, display_name)
_MODE_DISPLAY: dict[str, tuple[str, str]] = {
    "coco": ("🤖", "Coco"),
    "claude": ("🧠", "Claude"),
    "aiden": ("⚡", "Aiden"),
    "codex": ("📝", "Codex"),
    "gemini": ("💎", "Gemini"),
    "ttadk": ("🛠️", "TTADK"),
}


def build_programming_metadata(
    mode_name: str,
    *,
    tool_name: str | None = None,
    model_name: str | None = None,
    project_name: str | None = None,
) -> CardMetadata:
    """Build CardMetadata for a programming mode session.

    Args:
        mode_name: One of coco/claude/aiden/codex/gemini/ttadk.
        tool_name: Specific tool name (overrides mode default).
        model_name: Model name to display.
        project_name: Optional project name for header.
    """
    mode_key = mode_name.lower()
    emoji, display = _MODE_DISPLAY.get(mode_key, ("🤖", mode_name))

    return CardMetadata(
        project_name=project_name,
        mode_name=display,
        mode_emoji=emoji,
        tool_name=tool_name or mode_key,
        model_name=model_name,
        engine_type=None,  # Programming mode is not an engine
    )


class ProgrammingCardSession:
    """Wraps CardSession for programming handler's specific needs.

    Includes text batching: TEXT_DELTA events are accumulated and flushed
    at regular intervals (default 0.3s) to avoid overwhelming the Feishu API.
    Structural events (tool start/done, etc.) trigger immediate flush.
    """

    _DEFAULT_FLUSH_INTERVAL = 0.3  # seconds

    def __init__(self, session: CardSession, *, flush_interval: float | None = None) -> None:
        self._session = session
        self._text_active = False
        self._flush_interval = flush_interval or self._DEFAULT_FLUSH_INTERVAL
        # Text batching state
        self._pending_text = ""
        self._flush_lock = threading.Lock()
        self._flush_timer: threading.Timer | None = None

    @property
    def session(self) -> CardSession:
        return self._session

    @property
    def closed(self) -> bool:
        return self._session.closed

    def start(self) -> None:
        """Start the card (creates initial card in Feishu)."""
        self._session.dispatch(CardEvent.started())
        self._session.dispatch(CardEvent.text_started("_active_text"))
        self._text_active = True

    def on_event(self, acp_event: "ACPEvent") -> None:
        """Process an ACP event (converts to CardEvent internally).

        Text deltas are batched for efficiency. Structural events flush immediately.
        """
        card_event = CardEvent.from_acp(acp_event)

        # Text delta: accumulate and schedule flush
        if card_event.type == CardEventType.TEXT_DELTA:
            text = card_event.payload.get("text", "")
            if text:
                with self._flush_lock:
                    if not self._text_active:
                        self._session.dispatch(CardEvent.text_started("_active_text"))
                        self._text_active = True
                    self._pending_text += text
                    self._schedule_flush()
            return

        # Structural event: flush pending text first
        self._flush_now()

        # Tool events mark text as inactive
        if card_event.type == CardEventType.TOOL_STARTED:
            if self._text_active:
                self._session.dispatch(CardEvent.text_done("_active_text"))
                self._text_active = False

        # Text resumed after tool
        if card_event.type == CardEventType.TEXT_STARTED:
            self._text_active = True

        self._session.dispatch(card_event)

    def on_text(self, text: str) -> None:
        """Append text directly (for simple text-only streams)."""
        if text:
            with self._flush_lock:
                if not self._text_active:
                    self._session.dispatch(CardEvent.text_started("_active_text"))
                    self._text_active = True
                self._pending_text += text
                self._schedule_flush()

    def finish(self) -> None:
        """Complete the session normally."""
        self._flush_now()
        if self._text_active:
            self._session.dispatch(CardEvent.text_done("_active_text"))
            self._text_active = False
        self._session.dispatch(CardEvent.completed())

    def fail(self, error: str = "") -> None:
        """Mark the session as failed."""
        self._cancel_timer()
        if self._text_active:
            # Flush any pending text before failing
            pending = ""
            with self._flush_lock:
                pending = self._pending_text
                self._pending_text = ""
            if pending:
                self._session.dispatch(CardEvent.text_delta("_active_text", pending))
            self._session.dispatch(CardEvent.text_done("_active_text"))
            self._text_active = False
        self._session.dispatch(CardEvent.failed())

    def update_tool_model(self, tool_name: str | None = None, model_name: str | None = None) -> None:
        """Update the displayed tool/model in header subtitle."""
        self._flush_now()
        self._session.dispatch(CardEvent.tool_model_changed(tool_name, model_name))

    def get_message_id(self) -> str | None:
        """Get the message_id of the first card page (for message linking)."""
        binding = self._session._delivery.get_binding(self._session.session_id)
        if binding and binding.pages:
            first_page = binding.pages.get(0)
            if first_page:
                return first_page.message_id
        return None

    def get_final_text(self) -> str:
        """Extract accumulated text content from card state for context recording."""
        self._flush_now()
        state = self._session.state
        if not state:
            return ""
        parts = []
        for block in state.blocks:
            if block.kind == "text" and block.content:
                parts.append(block.content)
        return "\n".join(parts)

    # ---- Internal flush mechanism ----

    def _schedule_flush(self) -> None:
        """Schedule a flush timer if not already pending."""
        if self._flush_timer is None:
            self._flush_timer = threading.Timer(self._flush_interval, self._flush_now)
            self._flush_timer.daemon = True
            self._flush_timer.start()

    def _flush_now(self) -> None:
        """Flush pending text immediately."""
        self._cancel_timer()
        pending = ""
        with self._flush_lock:
            pending = self._pending_text
            self._pending_text = ""
        if pending and not self._session.closed:
            self._session.dispatch(CardEvent.text_delta("_active_text", pending))

    def _cancel_timer(self) -> None:
        """Cancel any pending flush timer."""
        with self._flush_lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
