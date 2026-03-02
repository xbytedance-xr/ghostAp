"""Session backends abstraction.

GhostAP currently supports two different ways to talk to an agent:

1) ACP backend (JSON-RPC 2.0 over stdio) — used by Coco.
2) CLI backend (spawn per prompt)       — used by Claude Code CLI.

The handlers expect an ACP-like streaming callback signature. For CLI backend
we downgrade to text-only ACPEvent(TEXT_CHUNK) events so that existing
rendering and streaming cards can be reused.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .acp.models import ACPEvent, ACPEventType, PromptResult
from .acp.sync_adapter import SyncACPSession
from .config import get_settings

logger = logging.getLogger(__name__)


class SyncSession(Protocol):
    """A minimal sync session interface used by handlers."""

    session_id: str
    created_at: float
    last_active: float
    message_count: int
    last_query: str
    is_resumed: bool

    def describe_agent(self) -> str: ...
    def start(self, startup_timeout: float = 60) -> str: ...
    def load_session(self, session_id: str) -> None: ...
    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]: ...
    def send_prompt(self, text: str, on_event: Optional[Callable[[ACPEvent], None]] = None, timeout: Optional[int] = None) -> PromptResult: ...
    def cancel(self) -> None: ...
    def close(self) -> None: ...
    def to_snapshot(self) -> dict: ...
    def get_session_info(self) -> str: ...

    def is_server_running(self) -> bool: ...
    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool: ...


@dataclass
class ClaudeCLIConfig:
    """Configuration knobs for Claude Code CLI backend."""

    command: str = "claude"
    add_dir: bool = True
    bypass_permissions: Optional[bool] = None  # None → use config.claude_cli_skip_permissions


class SyncClaudeCLISession:
    """Claude Code CLI backend.

    - Uses `claude -p` (print and exit) per prompt.
    - Uses `--session-id` for the first prompt and `--resume <id>` afterwards.
    - Emits TEXT_CHUNK ACP events only (no plan/tool events).
    """

    def __init__(self, cwd: str, config: Optional[ClaudeCLIConfig] = None):
        self._cwd = cwd
        self._cfg = config or ClaudeCLIConfig()
        self._proc: Optional[subprocess.Popen] = None
        self._cancel_event = threading.Event()

        self.session_id: str = ""
        self.created_at: float = time.time()
        self.last_active: float = time.time()
        self.message_count: int = 0
        self.last_query: str = ""
        self.is_resumed: bool = False

    def describe_agent(self) -> str:
        try:
            return f"cmd={self._cfg.command} cwd={self._cwd} backend=cli"
        except Exception:
            return "agent=claude backend=cli"

    def start(self, startup_timeout: float = 60) -> str:
        # No long-running server here; just validate executable and mint a session id.
        if not shutil.which(self._cfg.command):
            raise RuntimeError(f"未找到 Claude CLI 可执行文件: {self._cfg.command}")
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        return self.session_id

    def load_session(self, session_id: str) -> None:
        # Claude CLI uses local persistence; we just switch to target session id.
        self.session_id = session_id
        self.is_resumed = True

    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        # Claude CLI manages its own history; GhostAP doesn't parse it here.
        return []

    def is_server_running(self) -> bool:
        # Per-prompt spawn — no persistent server to check.
        return True

    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
        return True

    def _resolve_bypass_permissions(self) -> bool:
        """Resolve whether to skip Claude permissions (config > explicit)."""
        if self._cfg.bypass_permissions is not None:
            return self._cfg.bypass_permissions
        return get_settings().claude_cli_skip_permissions

    def send_prompt(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
    ) -> PromptResult:
        if not self.session_id:
            self.start()

        self._cancel_event.clear()
        self.last_active = time.time()
        self.message_count += 1
        self.last_query = text

        def _build_args(resumed: bool) -> list[str]:
            args: list[str] = [self._cfg.command, "-p"]
            if self._cfg.add_dir:
                args += ["--add-dir", self._cwd]
            if self._resolve_bypass_permissions():
                args.append("--dangerously-skip-permissions")

            if resumed:
                args += ["--resume", self.session_id]
            else:
                args += ["--session-id", self.session_id]

            args.append(text)
            return args

        def _run_once(resumed: bool) -> tuple[int, str, str, str]:
            """Run one claude invocation and return (returncode, stdout, stderr, state)."""
            args = _build_args(resumed)
            chunks: list[str] = []
            try:
                # Claude Code CLI refuses to launch inside another Claude Code session.
                # Our process may run under Claude Code / other wrappers, so we must
                # explicitly unset the guard env to avoid nested-session crash.
                env = os.environ.copy()
                env.pop("CLAUDECODE", None)

                self._proc = subprocess.Popen(
                    args,
                    cwd=self._cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )

                deadline = (time.monotonic() + timeout) if timeout else None
                assert self._proc.stdout is not None
                for line in self._proc.stdout:
                    if self._cancel_event.is_set():
                        self._proc.terminate()
                        self._proc.wait(timeout=5)
                        return (1, "".join(chunks), "", "cancelled")
                    if deadline and time.monotonic() > deadline:
                        self._proc.terminate()
                        self._proc.wait(timeout=5)
                        return (1, "".join(chunks), "", "timeout")
                    chunks.append(line)
                    if on_event:
                        on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=line))

                self._proc.wait(timeout=30)
                rc = int(self._proc.returncode or 0)
                err = (self._proc.stderr.read() or "").strip("\n") if self._proc.stderr else ""
                return (rc, "".join(chunks).strip("\n"), err, "ok")
            finally:
                self._proc = None

        def _is_missing_conversation(err_text: str, out_text: str) -> bool:
            blob = (err_text or "") + "\n" + (out_text or "")
            return "No conversation found with session ID" in blob

        try:
            # First try: follow the normal resume/session-id flow
            rc, out, err, state = _run_once(resumed=self.is_resumed)

            if state == "cancelled":
                self.is_resumed = True
                return PromptResult(stop_reason="cancelled", text=out)
            if state == "timeout":
                self.is_resumed = True
                return PromptResult(stop_reason="cancelled", text="❌ Claude 执行超时，已取消")

            # If resume failed because local conversation doesn't exist, fall back to a fresh session once.
            if self.is_resumed and rc != 0 and _is_missing_conversation(err, out):
                logger.info("[ClaudeCLI] resume failed (missing conversation), fallback to new session")
                self.session_id = str(uuid.uuid4())
                self.is_resumed = False
                rc, out, err, _ = _run_once(resumed=False)

            output = out
            if rc != 0 and err:
                output = (output + "\n" + err).strip("\n")
                if on_event:
                    on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text="\n" + err))

            self.is_resumed = True
            stop_reason = "end_turn" if rc == 0 else "failed"
            return PromptResult(stop_reason=stop_reason, text=output)

        except Exception as e:
            self.is_resumed = True
            return PromptResult(stop_reason="error", text=f"❌ Claude 执行异常: {e}")

    def cancel(self) -> None:
        """Signal cancellation — the streaming loop will terminate the process."""
        self._cancel_event.set()
        proc = self._proc
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    def close(self) -> None:
        # Nothing persistent to close.
        return

    def to_snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent_type": "claude",
            "cwd": self._cwd,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "last_query": self.last_query,
            "is_resumed": self.is_resumed,
            "backend": "cli",
        }

    def get_session_info(self) -> str:
        duration = int(time.time() - self.created_at)
        minutes, seconds = divmod(duration, 60)
        resumed_info = " (已恢复)" if self.is_resumed else ""
        return (
            f"📊 Claude 会话信息{resumed_info} (CLI):\n"
            f"- 会话ID: {self.session_id}\n"
            f"- 消息数: {self.message_count}\n"
            f"- 持续时间: {minutes}分{seconds}秒"
        )


import re as _re

_RATE_LIMIT_PATTERNS = [
    _re.compile(r"rate.?limit", _re.IGNORECASE),
    _re.compile(r"\b429\b"),
    _re.compile(r"too many requests", _re.IGNORECASE),
    _re.compile(r"overloaded", _re.IGNORECASE),
]
_RETRY_AFTER_RE = _re.compile(r"retry[_\- ]?after[:\s=]*(\d+)", _re.IGNORECASE)


def _detect_rate_limit(error: Exception) -> Optional[int]:
    """Detect rate limiting from error.  Returns suggested wait seconds or 0 (detected
    but no explicit wait), or None (not a rate-limit error)."""
    msg = str(error)
    for pat in _RATE_LIMIT_PATTERNS:
        if pat.search(msg):
            m = _RETRY_AFTER_RE.search(msg)
            if m:
                val = int(m.group(1))
                return max(1, min(val, 600))  # clamp to [1, 600]
            return 0  # detected but no explicit wait
    return None


class RateLimitAwareSession:
    """Wraps a SyncSession with rate-limit-aware retry on send_prompt().

    Implements the full SyncSession protocol by explicit delegation (no __getattr__).
    """

    def __init__(
        self,
        inner: SyncSession,
        on_rate_limit: Optional[Callable[[int], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ):
        self._inner = inner
        self._on_rate_limit = on_rate_limit
        self._cancel_event = cancel_event or threading.Event()
        self._settings = get_settings()
        # Rate-limit state visible to status queries
        self.rate_limit_until: Optional[float] = None  # monotonic deadline

    # --- Explicit SyncSession protocol delegation ---

    @property
    def session_id(self) -> str:
        return self._inner.session_id

    @session_id.setter
    def session_id(self, value: str):
        self._inner.session_id = value

    @property
    def created_at(self) -> float:
        return self._inner.created_at

    @property
    def last_active(self) -> float:
        return self._inner.last_active

    @last_active.setter
    def last_active(self, value: float):
        self._inner.last_active = value

    @property
    def message_count(self) -> int:
        return self._inner.message_count

    @property
    def last_query(self) -> str:
        return self._inner.last_query

    @property
    def is_resumed(self) -> bool:
        return self._inner.is_resumed

    def describe_agent(self) -> str:
        return self._inner.describe_agent()

    def start(self, startup_timeout: float = 60) -> str:
        return self._inner.start(startup_timeout=startup_timeout)

    def load_session(self, session_id: str) -> None:
        self._inner.load_session(session_id)

    def load_local_history(self, session_id: Optional[str] = None, limit: int = 200) -> list[dict]:
        return self._inner.load_local_history(session_id=session_id, limit=limit)

    def cancel(self) -> None:
        self._cancel_event.set()
        self._inner.cancel()

    def close(self) -> None:
        self._inner.close()

    def to_snapshot(self) -> dict:
        return self._inner.to_snapshot()

    def get_session_info(self) -> str:
        return self._inner.get_session_info()

    def is_server_running(self) -> bool:
        return self._inner.is_server_running()

    def is_server_healthy(self, healthcheck_timeout: float = 2.0) -> bool:
        return self._inner.is_server_healthy(healthcheck_timeout=healthcheck_timeout)

    # --- Rate-limit-aware send_prompt ---

    def send_prompt(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
    ) -> PromptResult:
        if not self._settings.rate_limit_retry_enabled:
            return self._inner.send_prompt(text, on_event=on_event, timeout=timeout)

        max_retries = self._settings.rate_limit_max_retries
        max_wait = self._settings.rate_limit_max_wait
        base_wait = self._settings.rate_limit_base_wait
        last_error: Optional[Exception] = None

        for attempt in range(max_retries + 1):
            try:
                self.rate_limit_until = None
                return self._inner.send_prompt(text, on_event=on_event, timeout=timeout)
            except Exception as e:
                wait_hint = _detect_rate_limit(e)
                if wait_hint is None or attempt >= max_retries:
                    raise
                last_error = e
                wait_time = min(wait_hint or base_wait, max_wait)
                wait_time = max(wait_time, 1)

                # Notify caller (UI) — swallow callback exceptions
                try:
                    if self._on_rate_limit:
                        self._on_rate_limit(wait_time)
                except Exception:
                    pass

                logger.warning(
                    "[RateLimit] 限速检测，等待 %ds 后重试 (attempt=%d/%d): %s",
                    wait_time, attempt + 1, max_retries, e,
                )

                # Interruptible sleep: check cancel_event every second
                self.rate_limit_until = time.monotonic() + wait_time
                deadline = time.monotonic() + wait_time
                while time.monotonic() < deadline:
                    if self._cancel_event.is_set():
                        self.rate_limit_until = None
                        raise last_error  # re-raise original error on cancel
                    remaining = deadline - time.monotonic()
                    self._cancel_event.wait(timeout=min(remaining, 1.0))
                self.rate_limit_until = None

        # Should not reach here, but just in case
        if last_error:
            raise last_error
        return self._inner.send_prompt(text, on_event=on_event, timeout=timeout)


def close_session_safely(session: Optional[SyncSession]) -> None:
    """Close an ACP/CLI session, ignoring errors."""
    if session:
        try:
            session.close()
        except Exception as e:
            logger.debug("关闭旧ACP session失败: %s", e)


def create_sync_session(agent_type: str, cwd: str, model_name: Optional[str] = None) -> SyncSession:
    """Factory for creating a sync session by backend.

    - coco/default: ACP backend
    - claude: CLI backend
    """
    from .coco_model import get_coco_model_manager

    agent_type = (agent_type or "").lower()
    if agent_type == "claude":
        return SyncClaudeCLISession(cwd=cwd)

    effective_model = model_name
    if not effective_model and agent_type in ("coco", ""):
        effective_model = get_coco_model_manager().get_current_model()

    return SyncACPSession(agent_type=agent_type or "coco", cwd=cwd, model_name=effective_model)


def create_engine_session(
    agent_type: str,
    cwd: str,
    on_rate_limit: Optional[Callable[[int], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    model_name: Optional[str] = None,
) -> SyncSession:
    """Create and start a session for Deep/Loop/Spec engines.

    - Claude: CLI backend (no ACP retry needed)
    - Others: ACP backend with retry and progressive timeout

    If rate_limit_retry_enabled is True in settings, the returned session
    is wrapped with RateLimitAwareSession for automatic retry on throttling.
    """
    from .acp.sync_adapter import start_session_with_retry
    from .coco_model import get_coco_model_manager

    settings = get_settings()
    agent_type = (agent_type or "").lower()

    if agent_type == "claude":
        session: SyncSession = SyncClaudeCLISession(cwd=cwd)
        session.start()
    else:
        effective_model = model_name
        if not effective_model:
            effective_model = get_coco_model_manager().get_current_model()

        session = start_session_with_retry(
            agent_type=agent_type or "coco",
            cwd=cwd,
            startup_timeout=settings.acp_startup_timeout,
            model_name=effective_model,
        )

    if settings.rate_limit_retry_enabled:
        session = RateLimitAwareSession(
            inner=session,
            on_rate_limit=on_rate_limit,
            cancel_event=cancel_event,
        )

    return session
