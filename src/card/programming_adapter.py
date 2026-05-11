"""Programming mode adapter: bridges streaming card pattern to CardSession.

Bridges streaming card pattern to CardSession for
ProgrammingHandler.handle_response(). Supports all programming modes:
Coco/Claude/Aiden/Codex/Gemini/TTADK.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Callable

from src.acp.renderer import ACPEventRenderer
from src.card.delivery.engine import CardDelivery
from src.card.events import CardEvent, CardEventType
from src.card.render.budget import RenderBudget
from src.card.render.live_ticker import LiveTicker
from src.card.session import CardSession
from src.card.session.rotator import SessionRotator
from src.card.state.models import CardMetadata

if TYPE_CHECKING:
    from src.acp.models import ACPEvent, ToolCallInfo
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
    working_dir: str | None = None,
) -> CardMetadata:
    """Build CardMetadata for a programming mode session.

    Args:
        mode_name: One of coco/claude/aiden/codex/gemini/ttadk.
        tool_name: Specific tool name (overrides mode default).
        model_name: Model name to display.
        project_name: Optional project name for header.
        working_dir: Current project/session working directory for v2 header.
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
        working_dir=working_dir,
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
        subagent_session_factory: Callable[..., CardSession] | None = None,
        base_metadata: CardMetadata | None = None,
    ) -> None:
        self._session = session
        self._rotator = SessionRotator(session)
        self._session_factory = session_factory
        self._subagent_session_factory = subagent_session_factory
        self._base_metadata = base_metadata or CardMetadata()
        self._text_active = False
        self._active_text_block_id = "_active_text"
        self._pending_text_block_id: str | None = None
        self._reasoning_active = False
        self._active_reasoning_block_id = "_active_reasoning"
        self._reasoning_turn_seq = 0
        self._last_reasoning_boundary_seq = 0
        self._flush_interval = flush_interval or self._DEFAULT_FLUSH_INTERVAL
        # Text batching state
        self._pending_text = ""
        self._flush_lock = threading.RLock()  # leaf lock: never held while acquiring a LockLevel lock
        self._flush_lock_holder = threading.local()  # per-thread flag for lock ownership assertion
        self._flush_timer: threading.Timer | None = None
        self._latest_plan_event: CardEvent | None = None
        self._agent_sessions: dict[str, CardSession] = {}
        self._agent_summaries: dict[str, dict] = {}
        self._acp_renderer = ACPEventRenderer()
        self._turn_snapshots = ()
        self._text_turn_seq = 0
        self._last_tool_boundary_seq = 0
        self._ticker_factory = LiveTicker
        self._ticker = None
        self._last_ticker_update_at: float | None = None
        self._ticker_update_min_interval = 5.0
        # TimerScheduler callbacks must stay lightweight. In production, ticker
        # metadata dispatch is submitted to the shared delivery pool so the
        # scheduler thread never runs reduce/render/delivery inline. Tests that
        # opt into sync delivery keep synchronous ticker dispatch by default for
        # deterministic assertions, but can force async via this private flag.
        self._ticker_dispatch_async = not getattr(session, "_sync_delivery", False)
        self._ticker_executor_factory = None

    @property
    def session(self) -> CardSession:
        return self._rotator.current

    @property
    def closed(self) -> bool:
        return self._rotator.closed or self._rotator.current.closed

    def start(self) -> None:
        """Start the card (creates initial card in Feishu)."""
        self._rotator.dispatch(CardEvent.started())
        self._rotator.dispatch(CardEvent.text_started("_active_text"))
        self._text_active = True
        self._start_ticker()

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

        self._acp_renderer.ingest_event(acp_event)
        self._turn_snapshots = self._acp_renderer.snapshot_turns()

        card_event = CardEvent.from_acp(acp_event)

        # Text delta: accumulate and schedule flush. ACP turns get stable,
        # per-turn block IDs so a later turn never appends to an earlier one.
        if card_event.type == CardEventType.TEXT_DELTA:
            text = card_event.payload.get("text", "")
            if text:
                if self._reasoning_active:
                    self._rotator.dispatch(CardEvent.reasoning_done(self._active_reasoning_block_id))
                    self._reasoning_active = False
                block_id = self._current_text_block_id()
                with self._flush_lock:
                    self._flush_lock_holder.held = True
                    try:
                        if self._text_active and self._active_text_block_id != block_id:
                            self._flush_now()
                            self._rotator.dispatch(CardEvent.text_done(self._active_text_block_id))
                            self._text_active = False
                        if not self._text_active:
                            self._active_text_block_id = block_id
                            self._rotator.dispatch(CardEvent.text_started(block_id))
                            self._text_active = True
                        self._pending_text_block_id = self._active_text_block_id
                        self._pending_text += text
                        self._schedule_flush()
                    finally:
                        self._flush_lock_holder.held = False
            return

        if card_event.type == CardEventType.REASONING_DELTA:
            self._flush_now()
            if not self._reasoning_active:
                block_id = self._current_reasoning_block_id()
                self._active_reasoning_block_id = block_id
                self._rotator.dispatch(CardEvent.reasoning_started(block_id))
                self._reasoning_active = True
            # Override the block_id in the delta to match the current reasoning block
            card_event = CardEvent(
                type=card_event.type,
                payload={**card_event.payload, "block_id": self._active_reasoning_block_id},
            )
            self._rotator.dispatch(card_event)
            return

        # Structural event: flush pending text first
        self._flush_now()

        if card_event.type == CardEventType.TOOL_STARTED and self._reasoning_active:
            self._rotator.dispatch(CardEvent.reasoning_done(self._active_reasoning_block_id))
            self._reasoning_active = False

        # Tool events mark text as inactive and bump reasoning boundary
        if card_event.type == CardEventType.TOOL_STARTED:
            self._last_tool_boundary_seq += 1
            self._last_reasoning_boundary_seq += 1
            if self._text_active:
                self._rotator.dispatch(CardEvent.text_done(self._active_text_block_id))
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
                        self._active_text_block_id = "_active_text"
                        self._rotator.dispatch(CardEvent.text_started(self._active_text_block_id))
                        self._text_active = True
                    self._pending_text_block_id = self._active_text_block_id
                    self._pending_text += text
                    self._schedule_flush()
                finally:
                    self._flush_lock_holder.held = False

    def finish(self, *, fallback_text: str = "") -> None:
        """Complete the session normally.

        Args:
            fallback_text: If provided and the card contains no streamed text,
                this text is injected as a completion summary so the user sees
                the answer instead of a blank completed card.
        """
        self._flush_now()
        if self._reasoning_active:
            self._rotator.dispatch(CardEvent.reasoning_done(self._active_reasoning_block_id))
            self._reasoning_active = False
        if self._text_active:
            self._rotator.dispatch(CardEvent.text_done(self._active_text_block_id))
            self._text_active = False
        self._finish_agent_sessions(failed=False)
        # If no text was streamed into the card, use fallback_text as completion
        # summary so the user sees the answer instead of a blank card.
        summary = ""
        if fallback_text:
            state = self._rotator.current.state
            has_text = any(b.kind == "text" and b.content for b in state.blocks) if state else False
            if not has_text:
                summary = fallback_text
        self._rotator.dispatch(CardEvent.completed(summary=summary))
        self._stop_ticker()

    def fail(self, error: str = "") -> None:
        """Mark the session as failed."""
        self._cancel_timer()
        if self._text_active:
            # Flush any pending text before failing
            pending = ""
            pending_block_id = self._active_text_block_id
            with self._flush_lock:
                pending = self._pending_text
                pending_block_id = self._pending_text_block_id or self._active_text_block_id
                self._pending_text = ""
                self._pending_text_block_id = None
            if pending:
                self._rotator.dispatch(CardEvent.text_delta(pending_block_id, pending))
            self._rotator.dispatch(CardEvent.text_done(self._active_text_block_id))
            self._text_active = False
        if self._reasoning_active:
            self._rotator.dispatch(CardEvent.reasoning_done(self._active_reasoning_block_id))
            self._reasoning_active = False
        self._finish_agent_sessions(failed=True, error=error)
        self._rotator.dispatch(CardEvent.failed(error))
        self._stop_ticker()

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
        block_id = self._active_text_block_id
        with self._flush_lock:
            pending = self._pending_text
            block_id = self._pending_text_block_id or self._active_text_block_id
            self._pending_text = ""
            self._pending_text_block_id = None
        if pending and not self._rotator.current.closed:
            self._rotator.dispatch(CardEvent.text_delta(block_id, pending))

    def _current_text_block_id(self) -> str:
        """Return the stable text block ID for the current ACP turn."""
        if self._text_turn_seq == 0:
            self._text_turn_seq = 1
            return "_active_text"
        if self._last_tool_boundary_seq >= self._text_turn_seq:
            self._text_turn_seq = self._last_tool_boundary_seq + 1
        return f"_turn_{self._text_turn_seq}_text"

    def _current_reasoning_block_id(self) -> str:
        """Return a unique reasoning block ID for the current ACP turn.

        Mirrors ``_current_text_block_id`` to ensure each reasoning segment
        (between tool boundaries) gets its own block, preventing the
        block_index last-wins lookup from collapsing all reasoning panels
        into the same content.
        """
        if self._reasoning_turn_seq == 0:
            self._reasoning_turn_seq = 1
            return "_active_reasoning"
        if self._last_reasoning_boundary_seq >= self._reasoning_turn_seq:
            self._reasoning_turn_seq = self._last_reasoning_boundary_seq + 1
        return f"_turn_{self._reasoning_turn_seq}_reasoning"

    def _cancel_timer(self) -> None:
        """Cancel any pending flush timer."""
        with self._flush_lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None

    def _start_ticker(self) -> None:
        if self._ticker is not None:
            return
        from src.config import get_settings

        interval = get_settings().card.ticker_interval
        self._ticker = self._ticker_factory(
            session_id=self._rotator.current.session_id,
            on_frame=self._on_ticker_frame,
            interval=interval,
        )
        self._ticker.start()

    def _stop_ticker(self) -> None:
        ticker = self._ticker
        self._ticker = None
        if ticker is not None:
            ticker.stop()

    def _on_ticker_frame(self, frame: str) -> None:
        if not frame or self._rotator.current.closed:
            return
        current_frame = None
        current_state = self._rotator.current.state
        if current_state is not None:
            current_frame = current_state.metadata.live_ticker_frame
        if frame == current_frame:
            return
        now = time.monotonic()
        if (
            self._last_ticker_update_at is not None
            and now - self._last_ticker_update_at < self._ticker_update_min_interval
        ):
            return
        self._last_ticker_update_at = now
        if self._ticker_dispatch_async:
            try:
                executor = self._ticker_executor_factory() if self._ticker_executor_factory else None
                if executor is None:
                    from src.card.delivery.pool import get_delivery_pool

                    executor = get_delivery_pool()
                executor.submit(self._dispatch_ticker_frame, frame)
                return
            except RuntimeError:
                logger.debug("Ticker dispatch skipped because delivery pool is unavailable")
                return
            except Exception:
                logger.exception("Failed to submit ticker dispatch; dropping frame")
                return

        self._dispatch_ticker_frame(frame)

    def _dispatch_ticker_frame(self, frame: str) -> None:
        if not frame or self._rotator.current.closed:
            return
        self._rotator.dispatch(CardEvent.tool_model_changed(live_ticker_frame=frame))

    def _handle_plan_update(self, acp_event: "ACPEvent") -> None:
        """Update the in-card task list in place.

        Plan/task changes never spawn a new Feishu card — the whole task list
        lives in one streaming card and is updated as the agent works through it.
        A new continuation card is only created when the current card nears the
        Feishu element/byte limit (handled by render-time pagination).
        """
        card_event = CardEvent.from_acp(acp_event)
        self._latest_plan_event = card_event
        self._rotator.dispatch(card_event)
        for session in self._agent_sessions.values():
            if not session.closed:
                session.dispatch(card_event)

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
                self._update_agent_summary(tool_call, status="failed")
            else:
                session.dispatch(CardEvent.completed())
                self._update_agent_summary(tool_call, status="completed")
        return True

    def _ensure_agent_task_session(self, tool_call: "ToolCallInfo") -> CardSession:
        existing = self._agent_sessions.get(tool_call.id)
        if existing is not None and not existing.closed:
            return existing

        if self._session_factory is None:
            return self._rotator.current

        task_label = self._extract_agent_task_label(tool_call)
        branch_id = chr(ord("a") + len(self._agent_sessions))
        parent_seq = str(self._rotator.current.sequence)
        tool_name = self._extract_agent_tool_name(tool_call)
        metadata = replace(
            self._base_metadata,
            unit_id=tool_call.id,
            unit_kind="task",
            unit_label=task_label,
            tool_name=tool_name,
            card_sequence=f"{parent_seq}.{branch_id}",
            session_started_at=self._rotator.current.session_started_at,
            is_subagent=True,
            parent_card_seq=parent_seq,
            bridge_phrase=None,
        )
        if self._subagent_session_factory is not None:
            session = self._subagent_session_factory(
                self._rotator.current,
                branch_id=branch_id,
                tool_name=tool_name,
                metadata=metadata,
            )
        else:
            session = self._session_factory(metadata)
        session.dispatch(CardEvent.started())
        if self._latest_plan_event is not None:
            session.dispatch(self._latest_plan_event)
        self._agent_sessions[tool_call.id] = session
        self._update_agent_summary(tool_call, status="running", session=session)
        return session

    def _update_agent_summary(self, tool_call: "ToolCallInfo", *, status: str, session: CardSession | None = None) -> None:
        existing = self._agent_summaries.get(tool_call.id, {})
        current_session = session or self._agent_sessions.get(tool_call.id)
        metadata = getattr(current_session, "_metadata", None)
        summary = {
            **existing,
            "label": self._extract_agent_task_label(tool_call),
            "tool": self._extract_agent_tool_name(tool_call),
            "status": status,
        }
        if metadata is not None:
            summary["sequence"] = metadata.card_sequence
            if metadata.model_name:
                summary["model"] = metadata.model_name
        self._agent_summaries[tool_call.id] = summary
        if not self._rotator.current.closed:
            self._rotator.dispatch(CardEvent.tool_model_changed(subagents=tuple(self._agent_summaries.values())))

    def _finish_agent_sessions(self, *, failed: bool, error: str = "") -> None:
        summary_changed = False
        for tool_id, session in list(self._agent_sessions.items()):
            if session.closed:
                continue
            if failed:
                session.dispatch(CardEvent.failed(error))
                terminal_status = "failed"
            else:
                session.dispatch(CardEvent.completed())
                terminal_status = "completed"
            existing = self._agent_summaries.get(tool_id)
            if existing is not None and existing.get("status") != terminal_status:
                self._agent_summaries[tool_id] = {**existing, "status": terminal_status}
                summary_changed = True
        self._agent_sessions.clear()
        if summary_changed and not self._rotator.current.closed:
            try:
                self._rotator.dispatch(CardEvent.tool_model_changed(subagents=tuple(self._agent_summaries.values())))
            except Exception:
                logger.exception("Failed to publish final subagent summary; continuing parent terminal transition")

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

    @staticmethod
    def _extract_agent_tool_name(tool_call: "ToolCallInfo") -> str:
        content = (tool_call.content or "").strip()
        for line in content.splitlines():
            marker = "子代理："
            if marker in line:
                name = line.split(marker, 1)[1].strip()
                if name:
                    return name[:40]
        return "subagent"
