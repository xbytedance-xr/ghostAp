"""Synchronous adapter for ACPSession.

Existing GhostAP code is synchronous (threading-based). This adapter runs
an asyncio event loop in a dedicated daemon thread and exposes synchronous
methods that bridge to the async ACPSession.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import threading
import time
from functools import lru_cache
from typing import Any, Callable, Optional

from .models import ACPEvent, ACPSessionState, PromptResult
from .session import ACPSession
from .client import ACPHistoryStore
from ..config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=32)
def _supports_acp_serve(command: str) -> bool:
    """Best-effort detection whether a binary supports `acp serve`.

    We avoid hard-failing on environments where the agent CLI differs.

    Note: Results are cached indefinitely per command name. If a binary is
    upgraded to support ACP after the first probe, a process restart is
    required to pick up the change.
    """
    try:
        p = subprocess.run(
            [command, "acp", "serve", "-h"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        out = (p.stdout or "") + "\n" + (p.stderr or "")
        out_lower = out.lower()
        return "acp" in out_lower and "server" in out_lower
    except Exception:
        return False


def resolve_agent_spec(agent_type: str) -> tuple[str, list[str]]:
    """Resolve (command, args) for spawning an ACP agent process over stdio."""
    agent_type = (agent_type or "").lower()

    # Config override first (allows custom binaries/wrappers)
    settings = get_settings()
    override_cmd, override_args = settings.get_acp_command(agent_type)
    if override_cmd:
        return override_cmd, override_args

    if agent_type == "coco":
        if _supports_acp_serve("coco"):
            return "coco", ["acp", "serve"]
        raise RuntimeError(
            "coco does not appear to support ACP server mode. "
            "Please upgrade coco or set COCO_ACP_CMD/COCO_ACP_ARGS."
        )

    if agent_type == "claude":
        # Some environments provide a Claude ACP agent; if not, fall back.
        if _supports_acp_serve("claude"):
            return "claude", ["acp", "serve"]
        raise RuntimeError(
            "claude does not appear to support ACP server mode. "
            "Please set CLAUDE_ACP_CMD/CLAUDE_ACP_ARGS to an ACP-capable agent binary."
        )

    # Default: treat agent_type as command and try `acp serve` first.
    if _supports_acp_serve(agent_type):
        return agent_type, ["acp", "serve"]
    raise RuntimeError(
        f"{agent_type} does not appear to support ACP server mode. "
        "Please set *_ACP_CMD/*_ACP_ARGS overrides."
    )


def start_session_with_retry(
    agent_type: str,
    cwd: str,
    startup_timeout: float = 60,
) -> SyncACPSession:
    """Start an ACP session with retry and progressive timeout.

    Extracts the retry logic from ACPSessionManager so that Deep/Loop engines
    can benefit from the same robustness without per-chat session management.
    """
    settings = get_settings()
    retries = max(1, int(getattr(settings, "acp_startup_retries", 2) or 2))

    last_err: Exception | None = None
    session: SyncACPSession | None = None

    for attempt in range(1, retries + 1):
        try:
            session = SyncACPSession(agent_type=agent_type, cwd=cwd)
            effective_timeout = float(startup_timeout) * (1.0 + 0.5 * (attempt - 1))
            session.start(startup_timeout=effective_timeout)
            logger.info("[ACP:%s] Engine session started (attempt=%d/%d)",
                        agent_type.upper(), attempt, retries)
            return session
        except Exception as e:
            last_err = e
            logger.warning("[ACP:%s] Engine session start failed (attempt=%d/%d): %s",
                           agent_type.upper(), attempt, retries, e)
            try:
                if session:
                    session.close()
            except Exception:
                pass
            session = None
            if attempt < retries:
                time.sleep(min(2.0, 0.3 * attempt))

    spec = ""
    try:
        spec = f" ({resolve_agent_spec(agent_type)})"
    except Exception:
        pass
    raise RuntimeError(
        f"启动 {agent_type} ACP Server 失败{spec}（已重试 {retries} 次）: {last_err}"
    )


class SyncACPSession:
    """Synchronous wrapper for ACPSession.

    Runs an asyncio event loop in a background thread and provides blocking
    methods for the synchronous codebase.
    """

    def __init__(self, agent_type: str, cwd: str, agent_args: Optional[list[str]] = None, agent_cmd: Optional[str] = None):
        self._agent_type = agent_type
        self._cwd = cwd
        if agent_cmd is not None:
            self._agent_cmd = agent_cmd
            self._agent_args = agent_args or []
        else:
            cmd, args = resolve_agent_spec(agent_type)
            self._agent_cmd = cmd
            self._agent_args = agent_args or args
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._acp_session: Optional[ACPSession] = None
        self._started = threading.Event()

        # Persistent watchdog: monitors active prompt future for process death
        self._active_future: Optional[asyncio.Future] = None
        self._watchdog_stop = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None

        # Public state (compatible with old BaseSession interface)
        self.session_id: str = ""
        self.created_at: float = time.time()
        self.last_active: float = time.time()
        self.message_count: int = 0
        self.last_query: str = ""
        self.is_resumed: bool = False
        # Local history loaded from ~/.ghostap/acp_history/<session_id>.jsonl
        self.history: list[dict] = []

    def describe_agent(self) -> str:
        """Human-readable agent command spec for debugging."""
        try:
            args = " ".join(str(x) for x in (self._agent_args or []))
            return f"cmd={self._agent_cmd} args={args} cwd={self._cwd}"
        except Exception:
            return f"agent={self._agent_type}"

    def start(self, startup_timeout: float = 60) -> str:
        """Start event loop thread + ACP session. Returns session_id.

        Args:
            startup_timeout: Seconds to wait for ACP server process + handshake.
        """
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"acp-{self._agent_type}",
        )
        self._loop_thread.start()
        if not self._started.wait(timeout=min(5.0, float(startup_timeout or 60))):
            # Fail fast: event loop thread did not start.
            self.close()
            raise TimeoutError(f"ACP 事件循环启动超时: agent={self._agent_type}")

        # Start ACP session (spawns agent `acp serve` process and initializes protocol)
        try:
            session_id = self._run_async(self._start_session(), timeout=startup_timeout)
            self.session_id = session_id
            return session_id
        except Exception:
            # Best-effort cleanup on startup failure.
            try:
                self.close()
            except Exception:
                pass
            raise

    def is_server_running(self) -> bool:
        """Best-effort check whether the ACP agent process is still alive."""
        try:
            if not self._acp_session:
                return False
            proc = getattr(self._acp_session, "_proc", None)
            if proc is None:
                return False
            # asyncio.subprocess.Process has `returncode`, while subprocess.Popen has `poll()`.
            rc = getattr(proc, "returncode", None)
            if rc is not None:
                return False
            poll = getattr(proc, "poll", None)
            if callable(poll):
                return poll() is None
            return True
        except Exception:
            return False

    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
        """More accurate ACP server health check.

        - Ensures process is alive
        - Ensures ACP connection can respond to a lightweight request
        """
        if not self.is_server_running():
            return False
        if not self._acp_session:
            return False
        try:
            # Run a lightweight RPC (list_sessions) with a short timeout.
            return bool(self._run_async(self._acp_session.health_check(timeout=healthcheck_timeout), timeout=healthcheck_timeout + 1.0))
        except Exception:
            return False

    async def _start_session(self) -> str:
        self._acp_session = ACPSession(
            agent_cmd=self._agent_cmd,
            agent_args=self._agent_args,
            cwd=self._cwd,
        )
        return await self._acp_session.start()

    def load_session(self, session_id: str) -> None:
        """Load an existing session (for resume)."""
        if not self._acp_session:
            raise RuntimeError("Session not started")
        self._run_async(self._acp_session.load_session(session_id))
        self.session_id = session_id
        self.is_resumed = True
        self.load_local_history(session_id)

    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        """Load persisted local history for a given ACP session id.

        Handles missing/corrupt history files by returning an empty list.
        """
        sid = session_id or self.session_id
        try:
            store = ACPHistoryStore()
            self.history = store.load(sid, limit=limit)
        except Exception:
            self.history = []
        return list(self.history)

    def _start_watchdog(self) -> None:
        """Start a persistent watchdog thread that monitors active prompt futures."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()

        def _watchdog_loop():
            while not self._watchdog_stop.wait(timeout=5.0):
                fut = self._active_future
                if fut is None or fut.done():
                    continue
                if not self.is_server_running():
                    logger.warning("[ACP:%s] Agent process died mid-prompt, cancelling",
                                   self._agent_type)
                    fut.cancel()

        self._watchdog_thread = threading.Thread(
            target=_watchdog_loop, daemon=True, name=f"acp-watchdog-{self._agent_type}",
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        """Stop the persistent watchdog thread."""
        self._watchdog_stop.set()
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2)
        self._watchdog_thread = None

    def send_prompt(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
    ) -> PromptResult:
        """Send prompt synchronously, blocking until completion.

        A persistent watchdog thread monitors for agent process death and
        cancels the future early instead of waiting for the full timeout.
        """
        if not self._acp_session:
            raise RuntimeError("Session not started")

        self.last_active = time.time()
        self.message_count += 1
        self.last_query = text

        future = asyncio.run_coroutine_threadsafe(
            self._acp_session.prompt(text, on_event=on_event),
            self._loop,
        )
        self._active_future = future
        self._start_watchdog()

        try:
            return future.result(timeout=timeout)
        except asyncio.CancelledError:
            raise RuntimeError("ACP agent 进程在执行过程中意外终止")
        except TimeoutError:
            # Cancel the agent process on timeout to free resources
            self.cancel()
            raise
        finally:
            self._active_future = None

    def cancel(self) -> None:
        """Cancel current prompt."""
        if self._acp_session and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._acp_session.cancel(),
                self._loop,
            )

    def close(self) -> None:
        """Close session and stop event loop."""
        self._stop_watchdog()
        if self._acp_session and self._loop:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._acp_session.close(),
                    self._loop,
                )
                future.result(timeout=10)
            except Exception as e:
                logger.debug("Error closing ACP session: %s", e)

        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread and self._loop_thread.is_alive():
                self._loop_thread.join(timeout=5)
            self._loop.close()
            self._loop = None

        self._acp_session = None

    def to_snapshot(self) -> dict:
        """Return session snapshot for persistence."""
        return {
            "session_id": self.session_id,
            "agent_type": self._agent_type,
            "cwd": self._cwd,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "last_query": self.last_query,
            "is_resumed": self.is_resumed,
        }

    def get_session_info(self) -> str:
        """Return human-readable session info."""
        duration = int(time.time() - self.created_at)
        minutes, seconds = divmod(duration, 60)
        agent_name = self._agent_type.capitalize()
        resumed_info = " (已恢复)" if self.is_resumed else ""
        return (
            f"📊 {agent_name} 会话信息{resumed_info}:\n"
            f"- 会话ID: {self.session_id}\n"
            f"- 消息数: {self.message_count}\n"
            f"- 持续时间: {minutes}分{seconds}秒"
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _run_loop(self) -> None:
        """Run asyncio event loop in background thread."""
        asyncio.set_event_loop(self._loop)
        self._started.set()
        self._loop.run_forever()

    def _run_async(self, coro, timeout: float = 60) -> Any:
        """Run async coroutine in background loop, blocking until done."""
        if not self._loop:
            raise RuntimeError("Event loop not started")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)
