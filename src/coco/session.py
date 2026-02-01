import subprocess
import time
from dataclasses import dataclass
from typing import Optional
from ..config import get_settings
from ..session.base import BaseSession
from ..session.manager import BaseSessionManager


@dataclass
class CocoSession(BaseSession):
    def _generate_session_id(self) -> str:
        return f"feishu_{self.chat_id}_{int(self.created_at)}"

    def _get_cli_name(self) -> str:
        return "coco"

    def _get_execution_timeout(self) -> int:
        return get_settings().coco_execution_timeout

    def _get_max_output_length(self) -> int:
        return get_settings().coco_max_output_length

    def _build_cmd(self, prompt: str, resume: bool) -> list[str]:
        cmd = [
            "coco",
            "-p",
            "-y",
            "--session-id", self.session_id,
        ]
        if resume and not self.is_resumed:
            cmd.extend(["--resume", self.session_id])
            self.is_resumed = True
        cmd.append(prompt)
        return cmd

    def _build_resume_cmd(self) -> list[str]:
        return [
            "coco",
            "-p",
            "-y",
            "--resume", self.session_id,
            "继续之前的对话",
        ]

    def _handle_send_error_recovery(
        self, result: subprocess.CompletedProcess, prompt: str,
        timeout: int, cwd: Optional[str]
    ) -> Optional[subprocess.CompletedProcess]:
        return None  # Coco has no retry logic


class CocoSessionManager(BaseSessionManager[CocoSession]):
    def __init__(self):
        settings = get_settings()
        super().__init__(CocoSession, session_timeout=settings.coco_session_timeout)

    def is_in_coco_mode(self, chat_id: str) -> bool:
        return self.has_active_session(chat_id)
