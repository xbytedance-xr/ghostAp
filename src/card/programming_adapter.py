"""Programming mode adapter: bridges streaming card pattern to CardSession.

Provides a drop-in replacement for the StreamingCardManager usage in
ProgrammingHandler.handle_response(). Supports all programming modes:
Coco/Claude/Aiden/Codex/Gemini/TTADK.
"""

from __future__ import annotations

import logging
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

    Provides simplified methods matching the streaming card pattern:
    - start() → dispatch STARTED + TEXT_STARTED
    - on_event(acp_event) → dispatch converted event
    - on_text(text) → dispatch TEXT_DELTA
    - finish() → dispatch TEXT_DONE + COMPLETED
    - fail(error) → dispatch FAILED
    """

    def __init__(self, session: CardSession) -> None:
        self._session = session
        self._text_active = False

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

        Handles text resumption: if a tool completes and text resumes,
        automatically starts a new text block.
        """
        card_event = CardEvent.from_acp(acp_event)

        # If we get a text delta but no active text block, start one
        if card_event.type == CardEventType.TEXT_DELTA and not self._text_active:
            self._session.dispatch(CardEvent.text_started("_active_text"))
            self._text_active = True

        # Tool events mark text as inactive
        if card_event.type == CardEventType.TOOL_STARTED:
            if self._text_active:
                self._session.dispatch(CardEvent.text_done("_active_text"))
                self._text_active = False

        self._session.dispatch(card_event)

    def on_text(self, text: str) -> None:
        """Append text directly (for simple text-only streams)."""
        if not self._text_active:
            self._session.dispatch(CardEvent.text_started("_active_text"))
            self._text_active = True
        self._session.dispatch(CardEvent.text_delta("_active_text", text))

    def finish(self) -> None:
        """Complete the session normally."""
        if self._text_active:
            self._session.dispatch(CardEvent.text_done("_active_text"))
            self._text_active = False
        self._session.dispatch(CardEvent.completed())

    def fail(self, error: str = "") -> None:
        """Mark the session as failed."""
        if self._text_active:
            self._session.dispatch(CardEvent.text_done("_active_text"))
            self._text_active = False
        self._session.dispatch(CardEvent.failed())

    def update_tool_model(self, tool_name: str | None = None, model_name: str | None = None) -> None:
        """Update the displayed tool/model in header subtitle."""
        self._session.dispatch(CardEvent.tool_model_changed(tool_name, model_name))
