"""Claude Code CLI session backend."""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from ..acp.models import ACPEvent, ACPEventType, PromptResult
from ..config import get_settings
from ..utils.errors import get_error_detail
from ..utils.retry import RetryPolicy, prompt_with_retry

logger = logging.getLogger(__name__)


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
            logger.debug("SyncClaudeCLISession.describe_agent: failed", exc_info=True)
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

            args.append("--")
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
                from ..utils.env import build_clean_env
                env = build_clean_env()

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

                # Watchdog thread: terminates the process on timeout or cancel
                # since blocking readline cannot check these conditions.
                terminated_reason: list[str] = []
                proc_ref = self._proc

                def _watchdog():
                    while True:
                        try:
                            if proc_ref.poll() is not None:
                                return
                        except Exception:
                            return
                        if self._cancel_event.is_set():
                            terminated_reason.append("cancelled")
                            try:
                                proc_ref.terminate()
                            except Exception:
                                pass
                            return
                        if deadline and time.monotonic() > deadline:
                            terminated_reason.append("timeout")
                            try:
                                proc_ref.terminate()
                            except Exception:
                                pass
                            return
                        self._cancel_event.wait(timeout=1.0)

                watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
                watchdog_thread.start()

                for line in self._proc.stdout:
                    if terminated_reason:
                        break
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

                if terminated_reason:
                    return (1, "".join(chunks), "", terminated_reason[0])

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

        except (subprocess.SubprocessError, OSError, TimeoutError) as e:
            self.is_resumed = True
            return PromptResult(stop_reason="error", text=f"❌ Claude 执行异常: {get_error_detail(e)}")

    def send_prompt_with_retry(
        self,
        text: str,
        on_event: Optional[Callable[[ACPEvent], None]] = None,
        timeout: Optional[int] = None,
        retry_policy: Optional[RetryPolicy] = None,
        before_retry: Optional[Callable[[int, Exception], None]] = None,
        total_timeout: Optional[float] = None,
    ) -> PromptResult:
        return prompt_with_retry(
            lambda: self.send_prompt(text, on_event=on_event, timeout=timeout),
            self._cancel_event,
            retry_policy=retry_policy,
            before_retry=before_retry,
            total_timeout=total_timeout,
        )

    def cancel(self) -> None:
        """Signal cancellation — the streaming loop will terminate the process."""
        self._cancel_event.set()
        proc = self._proc
        if proc:
            try:
                proc.terminate()
            except Exception:
                logger.debug("SyncClaudeCLISession.cancel: terminate failed", exc_info=True)

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
