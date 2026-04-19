"""ACP session — manages a single ACP agent process lifecycle.

Wraps the ACP SDK's spawn_agent_process to provide a clean interface
for starting sessions, sending prompts, and receiving structured events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Callable, Optional

from acp.helpers import text_block
from acp.schema import PromptResponse
from acp.stdio import spawn_agent_process

from ..config import get_settings
from ..utils.async_helpers import safe_wait_for
from .client import ACPHistoryStore, GhostAPClient
from .models import ACPEvent, ACPEventType, ACPSessionState, PromptResult

logger = logging.getLogger(__name__)


class ACPStartupError(RuntimeError):
    """ACP 启动失败的统一可诊断异常（SSOT）。

    继承 RuntimeError 保持向后兼容（已有 except RuntimeError 的捕获链），
    同时标记为 GhostAP 域异常方便统一 log_exception 降级。

    字段协议（稳定）：
    - agent_cmd/agent_args/cwd: 启动命令
    - returncode/stdout_snippet/stderr_snippet: best-effort 诊断片段（应为短文本，便于日志输出/脱敏/截断）
    - fail_phase: 失败阶段（可选但强烈建议设置），用于聚合与排障
    - cause: 原始异常（保留异常链）
    """

    is_ghostap_error = True

    def __init__(
        self,
        message: str,
        *,
        agent_cmd: str,
        agent_args: list[str],
        cwd: str,
        returncode: Optional[int] = None,
        stdout_snippet: str = "",
        stderr_snippet: str = "",
        fail_phase: str = "",
        cause: Exception | None = None,
    ):
        super().__init__(message)
        self.agent_cmd = str(agent_cmd or "")
        self.agent_args = list(agent_args or [])
        self.cwd = str(cwd or "")
        self.returncode = returncode
        self.stdout_snippet = stdout_snippet or ""
        self.stderr_snippet = stderr_snippet or ""
        self.fail_phase = str(fail_phase or "")
        self.__cause__ = cause


async def _read_stream_snippet(stream: object, *, max_bytes: int = 8192, timeout: float = 0.2) -> str:
    """Best-effort read a small snippet from an asyncio stream.

    IMPORTANT: stdout is ACP JSON-RPC in success path. We only use this on startup failures.
    """
    if stream is None:
        return ""
    try:
        max_bytes = int(max_bytes or 0)
    except Exception:
        max_bytes = 8192
    max_bytes = max(0, min(max_bytes, 64 * 1024))
    if max_bytes <= 0:
        return ""
    try:
        timeout = float(timeout or 0)
    except Exception:
        timeout = 0.2
    timeout = max(0.05, min(timeout, 2.0))

    try:
        # asyncio.StreamReader: read(n)
        coro = getattr(stream, "read", None)
        if not callable(coro):
            return ""
        data = await safe_wait_for(coro(max_bytes), timeout=timeout, action="ACP stream read")
        if not data:
            return ""
        if isinstance(data, str):
            return data
        if isinstance(data, (bytes, bytearray)):
            return bytes(data).decode("utf-8", errors="ignore")
        return str(data)
    except Exception:
        return ""


class ACPSession:
    """Single ACP session — manages one agent process's full lifecycle.

    This is an async class. For synchronous usage, see SyncACPSession.
    """

    def __init__(self, agent_cmd: str, agent_args: list[str], cwd: str, env: Optional[dict[str, str]] = None):
        self._agent_cmd = agent_cmd
        self._agent_args = agent_args
        self._cwd = cwd
        self._env_override = dict(env) if isinstance(env, dict) else None
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
        # Raise the stdio stream buffer limit to handle large agent responses.
        # Default asyncio limit (64KB) causes "Separator is found, but chunk is
        # longer than limit" on verbose JSON-RPC messages from long-running tasks.
        transport_kwargs = {}
        buf_limit = getattr(settings, "acp_stream_buffer_limit", 0)
        if buf_limit and buf_limit > 0:
            transport_kwargs["limit"] = buf_limit

        # Claude Code CLI refuses to launch inside another Claude Code session when
        # `CLAUDECODE` is present. Even when we spawn an ACP server (e.g. `claude acp serve`)
        # via an override, we must explicitly drop this guard env to avoid nested-session crash.
        env = dict(self._env_override) if isinstance(self._env_override, dict) else os.environ.copy()
        env.pop("CLAUDECODE", None)

        self._ctx_manager = spawn_agent_process(
            client,
            self._agent_cmd,
            *self._agent_args,
            env=env,
            cwd=self._cwd,
            transport_kwargs=transport_kwargs or None,
        )

        phase = ""
        try:
            phase = "spawn"
            self._conn, self._proc = await self._ctx_manager.__aenter__()

            # Initialize protocol
            phase = "initialize"
            await self._conn.initialize(protocol_version=1)

            # Create new session
            phase = "new_session"
            session_resp = await self._conn.new_session(cwd=self._cwd)
            self._session_id = session_resp.session_id
            self._state.session_id = self._session_id
            self._state.is_active = True

            logger.info("[ACP:%s] Session started: %s", self._agent_cmd, self._session_id[:8])
            return self._session_id
        except Exception as e:
            # Best-effort capture process outputs for debugging. Only for startup failures.
            rc = None
            try:
                rc = getattr(self._proc, "returncode", None)
            except Exception:
                rc = None
            stderr_snip = ""
            stdout_snip = ""
            try:
                stderr_snip = await _read_stream_snippet(getattr(self._proc, "stderr", None))
            except Exception:
                stderr_snip = ""
            # Only read stdout if stderr is empty; stdout may contain useful banner/error.
            if not stderr_snip:
                try:
                    stdout_snip = await _read_stream_snippet(getattr(self._proc, "stdout", None))
                except Exception:
                    stdout_snip = ""

            raise ACPStartupError(
                "ACP 启动失败",
                agent_cmd=self._agent_cmd,
                agent_args=list(self._agent_args or []),
                cwd=self._cwd,
                returncode=rc,
                stdout_snippet=stdout_snip,
                stderr_snippet=stderr_snip,
                fail_phase=phase or "unknown",
                cause=e,
            ) from e

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
            await safe_wait_for(
                self._conn.load_session(cwd=self._cwd, session_id=self._session_id),
                timeout=timeout,
                action="ACP 健康检查",
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
                elif ev.event_type in (
                    ACPEventType.TOOL_CALL_START,
                    ACPEventType.TOOL_CALL_UPDATE,
                    ACPEventType.TOOL_CALL_DONE,
                ):
                    if ev.tool_call:
                        # Keep the latest state per tool_call_id
                        collected_tool_calls[ev.tool_call.id] = ev.tool_call
                        for p in ev.tool_call.locations or []:
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

        # Race guard: some ACP agents (or stdio scheduling) may deliver the final
        # PromptResponse slightly before the last streaming `session/update` messages.
        # If we clear the handler immediately, late TEXT_CHUNKs can be dropped.
        # We keep a tiny grace window only when no text has been observed yet.
        try:
            if not (result.text or ""):
                deadline = time.time() + 0.05
                while time.time() < deadline and not (result.text or ""):
                    await asyncio.sleep(0.005)
        except Exception:
            pass

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
            windowed = [
                e
                for e in entries
                if isinstance(e, dict) and (e.get("ts") or 0) >= start_ts and (e.get("ts") or 0) <= end_ts
            ]
            result.ingest_history(windowed)
        except Exception:
            pass

        return result

    async def set_model(self, model_id: str) -> bool:
        """Switch the model for this session via ACP protocol.

        Calls session/setModel on the running agent. Returns True on success.
        Falls back gracefully if the agent doesn't support the method.
        """
        if not self._conn or not self._session_id:
            raise RuntimeError("Session not started. Call start() first.")
        try:
            await self._conn.set_session_model(model_id=model_id, session_id=self._session_id)
            logger.info("[ACP:%s] Model switched to: %s (session=%s)", self._agent_cmd, model_id, self._session_id[:8])
            return True
        except Exception as e:
            logger.warning("[ACP:%s] set_model failed (agent may not support it): %s", self._agent_cmd, e)
            return False

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
        logger.info("[ACP:%s] Session closed: %s", self._agent_cmd, (self._session_id or "none")[:8])

    def _dispatch_event(self, event: ACPEvent) -> None:
        """Dispatch event to the current handler."""
        if self._event_handler:
            try:
                self._event_handler(event)
            except Exception as e:
                logger.debug("[ACP] Event handler error: %s", e)
