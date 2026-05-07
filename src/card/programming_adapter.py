"""Programming mode adapter: bridges streaming card pattern to CardSession.

Bridges streaming card pattern to CardSession for
ProgrammingHandler.handle_response(). Supports all programming modes:
Coco/Claude/Aiden/Codex/Gemini/TTADK.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Callable

from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.render.budget import RenderBudget
from src.card.session import CardSession
from src.card.session.rotator import SessionRotator
from src.card.state.models import CardMetadata

if TYPE_CHECKING:
    from src.acp.models import ACPEvent, PlanInfo, ToolCallInfo
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

_AGENT_TOOL_TITLES = {"agent", "subagent"}


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

    def __init__(
        self,
        session: CardSession,
        *,
        flush_interval: float | None = None,
        session_factory: Callable[[CardMetadata], CardSession] | None = None,
        base_metadata: CardMetadata | None = None,
    ) -> None:
        self._session = session
        self._rotator = SessionRotator(session)
        self._session_factory = session_factory
        self._base_metadata = base_metadata or CardMetadata()
        self._text_active = False
        self._flush_interval = flush_interval or self._DEFAULT_FLUSH_INTERVAL
        # Text batching state
        self._pending_text = ""
        self._flush_lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
        self._flush_lock_holder = threading.local()  # per-thread flag for lock ownership assertion
        self._flush_timer: threading.Timer | None = None
        self._latest_plan_event: CardEvent | None = None
        self._primary_task_signature: tuple[str, ...] = ()
        self._agent_sessions: dict[str, CardSession] = {}

    @property
    def session(self) -> CardSession:
        return self._rotator.current

    @property
    def closed(self) -> bool:
        return self._rotator.closed

    def start(self) -> None:
        """Start the card (creates initial card in Feishu)."""
        self._rotator.dispatch(CardEvent.started())
        self._rotator.dispatch(CardEvent.text_started("_active_text"))
        self._text_active = True

    def on_event(self, acp_event: "ACPEvent") -> None:
        """Process an ACP event (converts to CardEvent internally).

        Text deltas are batched for efficiency. Structural events flush immediately.
        """
        card_event = None
        if getattr(acp_event, "event_type", None).name == "PLAN_UPDATE":
            self._handle_plan_update(acp_event)
            return

        if self._handle_agent_task_event(acp_event):
            return

        card_event = CardEvent.from_acp(acp_event)

        # Text delta: accumulate and schedule flush
        if card_event.type == CardEventType.TEXT_DELTA:
            text = card_event.payload.get("text", "")
            if text:
                with self._flush_lock:
                    self._flush_lock_holder.held = True
                    try:
                        if not self._text_active:
                            self._rotator.dispatch(CardEvent.text_started("_active_text"))
                            self._text_active = True
                        self._pending_text += text
                        self._schedule_flush()
                    finally:
                        self._flush_lock_holder.held = False
            return

        # Structural event: flush pending text first
        self._flush_now()

        # Tool events mark text as inactive
        if card_event.type == CardEventType.TOOL_STARTED:
            if self._text_active:
                self._rotator.dispatch(CardEvent.text_done("_active_text"))
                self._text_active = False

        # Text resumed after tool
        if card_event.type == CardEventType.TEXT_STARTED:
            self._text_active = True

        self._rotator.dispatch(card_event)

    def on_text(self, text: str) -> None:
        """Append text directly (for simple text-only streams)."""
        if text:
            with self._flush_lock:
                self._flush_lock_holder.held = True
                try:
                    if not self._text_active:
                        self._rotator.dispatch(CardEvent.text_started("_active_text"))
                        self._text_active = True
                    self._pending_text += text
                    self._schedule_flush()
                finally:
                    self._flush_lock_holder.held = False

    def finish(self) -> None:
        """Complete the session normally."""
        self._flush_now()
        if self._text_active:
            self._rotator.dispatch(CardEvent.text_done("_active_text"))
            self._text_active = False
        self._rotator.dispatch(CardEvent.completed())
        self._finish_agent_sessions(failed=False)
        self._rotator.close()

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
                self._rotator.dispatch(CardEvent.text_delta("_active_text", pending))
            self._rotator.dispatch(CardEvent.text_done("_active_text"))
            self._text_active = False
        self._rotator.dispatch(CardEvent.failed(error))
        self._finish_agent_sessions(failed=True, error=error)
        self._rotator.close()

    def update_tool_model(self, tool_name: str | None = None, model_name: str | None = None) -> None:
        """Update the displayed tool/model in header subtitle."""
        self._flush_now()
        self._rotator.dispatch(CardEvent.tool_model_changed(tool_name, model_name))

    def get_message_id(self) -> str | None:
        """Get the message_id of the first card page (for message linking)."""
        current = self._rotator.current
        binding = current._delivery.get_binding(current.session_id)
        if binding and binding.pages:
            first_page = binding.pages.get(0)
            if first_page:
                return first_page.message_id
        return None

    def get_final_text(self) -> str:
        """Extract accumulated text content from card state for context recording."""
        self._flush_now()
        state = self._rotator.current.state
        if not state:
            return ""
        parts = []
        for block in state.blocks:
            if block.kind == "text" and block.content:
                parts.append(block.content)
        return "\n".join(parts)

    # ---- Internal flush mechanism ----

    def _schedule_flush(self) -> None:
        """Schedule a flush timer if not already pending.

        IMPORTANT: Must only be called while holding ``_flush_lock``.
        """
        if not getattr(self._flush_lock_holder, "held", False):
            logger.error(
                "_schedule_flush called without holding _flush_lock — "
                "this is an internal state error, please report to maintainers"
            )
            raise RuntimeError("_schedule_flush must be called under _flush_lock")
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
        if pending and not self._rotator.current.closed:
            self._rotator.dispatch(CardEvent.text_delta("_active_text", pending))

    def _cancel_timer(self) -> None:
        """Cancel any pending flush timer."""
        with self._flush_lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None

    def _handle_plan_update(self, acp_event: "ACPEvent") -> None:
        card_event = CardEvent.from_acp(acp_event)
        self._latest_plan_event = card_event
        current_tasks = self._extract_current_tasks(acp_event.plan)
        if current_tasks and current_tasks != self._primary_task_signature:
            self._rotate_primary_session(current_tasks)
        self._rotator.dispatch(card_event)
        for session in self._agent_sessions.values():
            if not session.closed:
                session.dispatch(card_event)

    def _rotate_primary_session(self, current_tasks: tuple[str, ...]) -> None:
        if self._session_factory is None:
            self._primary_task_signature = current_tasks
            return

        self._flush_now()
        if self._text_active:
            self._rotator.dispatch(CardEvent.text_done("_active_text"))
            self._text_active = False

        task_label = self._build_primary_task_label(current_tasks)
        metadata = replace(
            self._base_metadata,
            unit_id=task_label,
            unit_kind="task",
            unit_label=task_label,
            continuation_seq=0,
        )
        new_session = self._rotator.rotate(lambda: self._session_factory(metadata))
        self._primary_task_signature = current_tasks
        if new_session is not None and not new_session.closed:
            new_session.dispatch(CardEvent.started())

    def _handle_agent_task_event(self, acp_event: "ACPEvent") -> bool:
        tool_call = getattr(acp_event, "tool_call", None)
        if tool_call is None or not self._is_agent_task(tool_call):
            return False

        session = self._ensure_agent_task_session(tool_call)
        card_event = CardEvent.from_acp(acp_event)
        session.dispatch(card_event)
        event_name = getattr(acp_event, "event_type", None).name if getattr(acp_event, "event_type", None) else ""
        if event_name == "TOOL_CALL_DONE":
            if tool_call.status == "failed":
                session.dispatch(CardEvent.failed(tool_call.content or tool_call.title))
            else:
                session.dispatch(CardEvent.completed())
        return True

    def _ensure_agent_task_session(self, tool_call: "ToolCallInfo") -> CardSession:
        existing = self._agent_sessions.get(tool_call.id)
        if existing is not None and not existing.closed:
            return existing

        if self._session_factory is None:
            return self._rotator.current

        task_label = self._extract_agent_task_label(tool_call)
        metadata = replace(
            self._base_metadata,
            unit_id=tool_call.id,
            unit_kind="task",
            unit_label=task_label,
            continuation_seq=0,
        )
        session = self._session_factory(metadata)
        session.dispatch(CardEvent.started())
        if self._latest_plan_event is not None:
            session.dispatch(self._latest_plan_event)
        self._agent_sessions[tool_call.id] = session
        return session

    def _finish_agent_sessions(self, *, failed: bool, error: str = "") -> None:
        for tool_id, session in list(self._agent_sessions.items()):
            if session.closed:
                continue
            if failed:
                session.dispatch(CardEvent.failed(error))
            else:
                session.dispatch(CardEvent.completed())
        self._agent_sessions.clear()

    @staticmethod
    def _extract_current_tasks(plan: "PlanInfo | None") -> tuple[str, ...]:
        if plan is None:
            return ()
        return tuple(entry.content.strip() for entry in plan.entries if entry.status == "in_progress" and entry.content.strip())

    @staticmethod
    def _build_primary_task_label(current_tasks: tuple[str, ...]) -> str:
        if len(current_tasks) == 1:
            return current_tasks[0][:60]
        return f"并发任务（{len(current_tasks)}）"

    @staticmethod
    def _is_agent_task(tool_call: "ToolCallInfo") -> bool:
        title = (tool_call.title or "").strip().lower()
        content = (tool_call.content or "").strip()
        if title in _AGENT_TOOL_TITLES:
            return True
        return "子代理：" in content

    @staticmethod
    def _extract_agent_task_label(tool_call: "ToolCallInfo") -> str:
        content = (tool_call.content or "").strip()
        if content:
            first_line = content.splitlines()[0].strip()
            if first_line:
                return first_line[:60]
        title = (tool_call.title or "").strip()
        return title[:60] if title else "子任务"
