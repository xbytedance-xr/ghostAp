"""ACP session — manages a single ACP agent process lifecycle.

Wraps the ACP SDK's spawn_agent_process to provide a clean interface
for starting sessions, sending prompts, and receiving structured events.
"""

from __future__ import annotations

import logging
import time
import asyncio
from typing import Any, Callable, Optional

from acp.stdio import spawn_agent_process
from acp.helpers import text_block
from acp.schema import PromptResponse

from .client import GhostAPClient
from .client import ACPHistoryStore
from .models import ACPEvent, ACPEventType, ACPSessionState, PromptResult
from ..config import get_settings

logger = logging.getLogger(__name__)


class ACPSession:
    """Single ACP session — manages one agent process's full lifecycle.

    This is an async class. For synchronous usage, see SyncACPSession.
    """

    def __init__(self, agent_cmd: str, agent_args: list[str], cwd: str):
        self._agent_cmd = agent_cmd
        self._agent_args = agent_args
        self._cwd = cwd
        self._conn = None  # ClientSideConnection
        self._proc = None  # subprocess
        self._ctx_manager = None  # async context manager
        self._session_id: Optional[str] = None
        self._state = ACPSessionState(
            session_id="",
            agent_type=agent_cmd,
            cwd=cwd,
        )
        self._event_handler: Optional[Callable[[ACPEvent], None]] = None

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def state(self) -> ACPSessionState:
        return self._state

    async def start(self) -> str:
        """Start agent process and establish ACP connection. Returns session_id."""
        settings = get_settings()
        client = GhostAPClient(
            on_event=self._dispatch_event,
            auto_approve=settings.acp_permission_auto_approve,
            root_dir=self._cwd,
        )
        self._ctx_manager = spawn_agent_process(
            client,
            self._agent_cmd,
            *self._agent_args,
            cwd=self._cwd,
        )
        self._conn, self._proc = await self._ctx_manager.__aenter__()

        # Initialize protocol
        await self._conn.initialize(protocol_version=1)

        # Create new session
        session_resp = await self._conn.new_session(cwd=self._cwd)
        self._session_id = session_resp.session_id
        self._state.session_id = self._session_id
        self._state.is_active = True

        logger.info("[ACP:%s] Session started: %s", self._agent_cmd, self._session_id[:8])
        return self._session_id

    async def load_session(self, session_id: str) -> None:
        """Load an existing session by ID (for resume)."""
        if not self._conn:
            raise RuntimeError("Connection not established. Call start() first.")
        await self._conn.load_session(cwd=self._cwd, session_id=session_id)
        self._session_id = session_id
        self._state.session_id = session_id
        logger.info("[ACP:%s] Session loaded: %s", self._agent_cmd, session_id[:8])

    async def health_check(self, timeout: float = 2.0) -> bool:
        """Best-effort health check of ACP connection.

        We consider the server healthy only if:
        - underlying process is alive
        - JSON-RPC connection responds to a lightweight request within timeout
        """
        try:
            if not self._proc or self._proc.returncode is not None:
                return False
            if not self._conn:
                return False
            # Use a stable roundtrip request. `session/load` should be supported by agents.
            if not self._session_id:
                return False
            await asyncio.wait_for(
                self._conn.load_session(cwd=self._cwd, session_id=self._session_id),
                timeout=timeout,
            )
            return True
        except Exception:
            return False

    async def prompt(self, text: str, on_event: Optional[Callable[[ACPEvent], None]] = None) -> PromptResult:
        """Send a prompt and stream events. Returns PromptResult when done."""
        if not self._conn or not self._session_id:
            raise RuntimeError("Session not started. Call start() first.")

        start_ts = time.time()

        # Collector aggregates text/tool calls/plan/modified_files.
        collected_tool_calls: dict[str, Any] = {}
        result = PromptResult(stop_reason="")

        def _collector(ev: ACPEvent):
            try:
                if ev.event_type == ACPEventType.TEXT_CHUNK:
                    result.add_text(ev.text or "")
                elif ev.event_type in (ACPEventType.TOOL_CALL_START, ACPEventType.TOOL_CALL_UPDATE, ACPEventType.TOOL_CALL_DONE):
                    if ev.tool_call:
                        # Keep the latest state per tool_call_id
                        collected_tool_calls[ev.tool_call.id] = ev.tool_call
                        for p in (ev.tool_call.locations or []):
                            if p:
                                result.add_modified_file(p)
                elif ev.event_type == ACPEventType.PLAN_UPDATE:
                    result.set_plan(ev.plan)
            except Exception:
                pass
            if on_event:
                try:
                    on_event(ev)
                except Exception as exc:
                    logger.warning("[ACP] on_event callback error: %s", exc)

        self._event_handler = _collector
        self._state.message_count += 1

        self._state.last_active = time.time()

        response: PromptResponse = await self._conn.prompt(
            session_id=self._session_id,
            prompt=[text_block(text)],
        )

        self._event_handler = None

        # Finalize aggregated tool call list (preserve insertion order by first-seen)
        try:
            # If we have a dict keyed by id, the values are the latest states.
            result.tool_calls = list(collected_tool_calls.values())
        except Exception:
            pass

        result.stop_reason = response.stop_reason or "end_turn"

        # Best-effort: attach local tool results (execute/read/write/permission) produced during this prompt.
        try:
            store = ACPHistoryStore()
            entries = store.load(self._session_id, limit=2000)
            end_ts = time.time()
            windowed = [e for e in entries if isinstance(e, dict) and (e.get("ts") or 0) >= start_ts and (e.get("ts") or 0) <= end_ts]
            result.ingest_history(windowed)
        except Exception:
            pass

        return result

    async def cancel(self) -> None:
        """Cancel the current prompt execution."""
        if self._conn and self._session_id:
            await self._conn.cancel(session_id=self._session_id)

    async def close(self) -> None:
        """Close session and terminate agent process."""
        self._state.is_active = False
        if self._ctx_manager:
            try:
                await self._ctx_manager.__aexit__(None, None, None)
            except Exception as e:
                logger.debug("[ACP:%s] Error closing session: %s", self._agent_cmd, e)
            self._ctx_manager = None
            self._conn = None
            self._proc = None
        logger.info("[ACP:%s] Session closed: %s",
                     self._agent_cmd,
                     (self._session_id or "none")[:8])

    def _dispatch_event(self, event: ACPEvent) -> None:
        """Dispatch event to the current handler."""
        if self._event_handler:
            try:
                self._event_handler(event)
            except Exception as e:
                logger.debug("[ACP] Event handler error: %s", e)
