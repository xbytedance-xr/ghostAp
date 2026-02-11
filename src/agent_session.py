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
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .acp.models import ACPEvent, ACPEventType, PromptResult
from .acp.sync_adapter import SyncACPSession

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
    bypass_permissions: bool = True


class SyncClaudeCLISession:
    """Claude Code CLI backend.

    - Uses `claude -p` (print and exit) per prompt.
    - Uses `--session-id` for the first prompt and `--resume <id>` afterwards.
    - Emits TEXT_CHUNK ACP events only (no plan/tool events).
    """

    def __init__(self, cwd: str, config: Optional[ClaudeCLIConfig] = None):
        self._cwd = cwd
        self._cfg = config or ClaudeCLIConfig()

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

    def send_prompt(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
    ) -> PromptResult:
        if not self.session_id:
            self.start()

        self.last_active = time.time()
        self.message_count += 1
        self.last_query = text

        args: list[str] = [self._cfg.command, "-p"]
        # Restrict tool access scope (best-effort). Claude Code uses current dir by default,
        # but `--add-dir` makes intent explicit.
        if self._cfg.add_dir:
            args += ["--add-dir", self._cwd]
        if self._cfg.bypass_permissions:
            args.append("--dangerously-skip-permissions")

        if self.is_resumed:
            args += ["--resume", self.session_id]
        else:
            args += ["--session-id", self.session_id]

        args.append(text)

        try:
            p = subprocess.run(
                args,
                cwd=self._cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return PromptResult(stop_reason="cancelled", text="❌ Claude 执行超时，已取消")
        except Exception as e:
            return PromptResult(stop_reason="error", text=f"❌ Claude 执行异常: {e}")

        stdout = (p.stdout or "").strip("\n")
        stderr = (p.stderr or "").strip("\n")
        output = stdout
        if p.returncode != 0 and stderr:
            output = (stdout + "\n" + stderr).strip("\n")

        if output and on_event:
            # Emit once; keep it simple and reliable.
            on_event(ACPEvent(event_type=ACPEventType.TEXT_CHUNK, text=output))

        # After first successful call, mark resumed so later prompts keep the session.
        self.is_resumed = True
        stop_reason = "end_turn" if p.returncode == 0 else "failed"
        return PromptResult(stop_reason=stop_reason, text=output)

    def cancel(self) -> None:
        # Per-prompt run; nothing to cancel from another thread in this backend.
        return

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


def create_sync_session(agent_type: str, cwd: str) -> SyncSession:
    """Factory for creating a sync session by backend.

    - coco/default: ACP backend
    - claude: CLI backend
    """

    agent_type = (agent_type or "").lower()
    if agent_type == "claude":
        return SyncClaudeCLISession(cwd=cwd)
    return SyncACPSession(agent_type=agent_type or "coco", cwd=cwd)
