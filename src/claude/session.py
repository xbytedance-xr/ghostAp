import logging
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Callable
from ..config import get_settings
from ..session.base import BaseSession
from ..session.manager import BaseSessionManager

logger = logging.getLogger(__name__)


@dataclass
class ClaudeSession(BaseSession):
    def _generate_session_id(self) -> str:
        return str(uuid.uuid4())

    def _get_cli_name(self) -> str:
        return "claude"

    def _get_execution_timeout(self) -> int:
        return get_settings().claude_execution_timeout

    def _get_max_output_length(self) -> int:
        return get_settings().claude_max_output_length

    def _build_cmd(self, prompt: str, resume: bool) -> list[str]:
        cmd = [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
        ]
        # First message of a new session uses --session-id
        # Subsequent messages or resumed sessions use --resume
        if self.message_count == 1 and not self.is_resumed:
            cmd.extend(["--session-id", self.session_id])
        else:
            cmd.extend(["--resume", self.session_id])
        cmd.append(prompt)
        return cmd

    def _build_resume_cmd(self) -> list[str]:
        return [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--resume", self.session_id,
            "继续之前的对话",
        ]

    def _handle_send_error_recovery(
        self, result: subprocess.CompletedProcess, prompt: str,
        timeout: int, cwd: Optional[str]
    ) -> Optional[subprocess.CompletedProcess]:
        # If resume failed (session expired/not found), create new session and retry
        if result.stderr and "No conversation found with session ID" in result.stderr:
            logger.warning("Claude 会话 %s 已失效，创建新会话重试", self.session_id)
            self.session_id = str(uuid.uuid4())
            self.is_resumed = False
            self.message_count = 1
            cmd = [
                "claude",
                "-p",
                "--dangerously-skip-permissions",
                "--session-id", self.session_id,
                prompt,
            ]
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
        return None

    def _handle_streaming_error_recovery(
        self, stderr: str, prompt: str, timeout: int, cwd: Optional[str],
        on_chunk: Callable[[str], None], chunk_interval: float
    ) -> Optional[tuple[str, str, bool]]:
        # If resume failed (session expired/not found), create new session and retry
        if stderr and "No conversation found with session ID" in stderr:
            logger.warning("Claude 会话 %s 已失效，创建新会话重试", self.session_id)
            self.session_id = str(uuid.uuid4())
            self.is_resumed = False
            self.message_count = 1
            cmd = [
                "claude",
                "-p",
                "--dangerously-skip-permissions",
                "--session-id", self.session_id,
                prompt,
            ]
            return self._run_streaming_process(
                cmd, cwd, timeout, on_chunk, chunk_interval
            )
        return None


class ClaudeSessionManager(BaseSessionManager[ClaudeSession]):
    def __init__(self):
        settings = get_settings()
        super().__init__(ClaudeSession, session_timeout=settings.claude_session_timeout)

    def is_in_claude_mode(self, chat_id: str) -> bool:
        return self.has_active_session(chat_id)
