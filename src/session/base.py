import os as _os
import select
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Callable

from ..config import get_settings
from ..utils.text import clean_terminal_output, truncate_output


@dataclass
class BaseSession(ABC):
    chat_id: str
    session_id: str = field(default="")
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    last_query: str = ""
    is_resumed: bool = False

    def __post_init__(self):
        if not self.session_id:
            self.session_id = self._generate_session_id()

    # ---- Abstract methods for subclasses ----

    @abstractmethod
    def _generate_session_id(self) -> str:
        ...

    @abstractmethod
    def _get_cli_name(self) -> str:
        ...

    @abstractmethod
    def _build_cmd(self, prompt: str, resume: bool) -> list[str]:
        ...

    @abstractmethod
    def _build_resume_cmd(self) -> list[str]:
        ...

    def _get_execution_timeout(self) -> int:
        return get_settings().coco_execution_timeout

    def _get_max_output_length(self) -> int:
        return get_settings().coco_max_output_length

    def _handle_send_error_recovery(
        self, result: subprocess.CompletedProcess, prompt: str,
        timeout: int, cwd: Optional[str]
    ) -> Optional[subprocess.CompletedProcess]:
        """Subclass hook for error recovery in send_prompt. Return new result or None."""
        return None

    def _handle_streaming_error_recovery(
        self, stderr: str, prompt: str, timeout: int, cwd: Optional[str],
        on_chunk: Callable[[str], None], chunk_interval: float
    ) -> Optional[tuple[str, str, bool]]:
        """Subclass hook for error recovery in streaming. Return (output, stderr, timed_out) or None."""
        return None

    # ---- Shared implementations ----

    def send_prompt(
        self, prompt: str, timeout: Optional[int] = None,
        cwd: Optional[str] = None, resume: bool = False
    ) -> str:
        self.last_active = time.time()
        self.message_count += 1
        self.last_query = prompt

        if timeout is None:
            timeout = self._get_execution_timeout()

        cli_name = self._get_cli_name()

        try:
            cmd = self._build_cmd(prompt, resume)

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

            # Give subclass a chance to recover from errors (e.g. expired session)
            recovered = self._handle_send_error_recovery(result, prompt, timeout, cwd)
            if recovered is not None:
                result = recovered

            output = result.stdout.strip()
            if result.stderr:
                output += f"\n\n⚠️ stderr:\n{result.stderr.strip()}"

            output = self._clean_output(output)
            output = truncate_output(output, self._get_max_output_length())

            return output if output else "✅ 执行完成（无输出）"

        except subprocess.TimeoutExpired:
            return f"⏱️ {cli_name.capitalize()} 执行超时（{timeout}秒）"
        except FileNotFoundError:
            return f"❌ 未找到 {cli_name} 命令，请确保已安装 {cli_name}"
        except Exception as e:
            return f"❌ {cli_name.capitalize()} 执行异常: {str(e)}"

    def send_prompt_streaming(
        self,
        prompt: str,
        on_chunk: Callable[[str], None],
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        resume: bool = False,
        chunk_interval: float = 0.3,
    ) -> str:
        self.last_active = time.time()
        self.message_count += 1
        self.last_query = prompt

        if timeout is None:
            timeout = self._get_execution_timeout()

        cli_name = self._get_cli_name()

        try:
            cmd = self._build_cmd(prompt, resume)

            full_output, stderr_text, timed_out = self._run_streaming_process(
                cmd, cwd, timeout, on_chunk, chunk_interval
            )

            if timed_out:
                return f"⏱️ {cli_name.capitalize()} 执行超时（{timeout}秒）"

            # Give subclass a chance to recover from errors
            recovered = self._handle_streaming_error_recovery(
                stderr_text, prompt, timeout, cwd, on_chunk, chunk_interval
            )
            if recovered is not None:
                full_output, stderr_text, timed_out = recovered
                if timed_out:
                    return f"⏱️ {cli_name.capitalize()} 执行超时（{timeout}秒）"

            if stderr_text:
                full_output += f"\n\n⚠️ stderr:\n{stderr_text.strip()}"

            output = self._clean_output(full_output.strip())
            output = truncate_output(output, self._get_max_output_length())

            on_chunk(output)

            return output if output else "✅ 执行完成（无输出）"

        except FileNotFoundError:
            error_msg = f"❌ 未找到 {cli_name} 命令，请确保已安装 {cli_name}"
            on_chunk(error_msg)
            return error_msg
        except Exception as e:
            error_msg = f"❌ {cli_name.capitalize()} 执行异常: {str(e)}"
            on_chunk(error_msg)
            return error_msg

    def _run_streaming_process(
        self, cmd: list[str], cwd: Optional[str], timeout: int,
        on_chunk: Callable[[str], None], chunk_interval: float
    ) -> tuple[str, str, bool]:
        """Run command with streaming output using os.read()/select.

        Returns (full_output, stderr_text, timed_out).
        """
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
        )

        full_output = ""
        last_update_time = time.time()
        start_time = time.time()
        stdout_fd = process.stdout.fileno()

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                process.kill()
                process.wait()
                return "", "", True

            ready, _, _ = select.select([stdout_fd], [], [], 0.2)
            if ready:
                chunk = _os.read(stdout_fd, 4096)
                if not chunk:
                    break
                full_output += chunk.decode("utf-8", errors="replace")
                current_time = time.time()
                if current_time - last_update_time >= chunk_interval:
                    cleaned = self._clean_output(full_output.strip())
                    if cleaned:
                        on_chunk(cleaned)
                    last_update_time = current_time
            else:
                if process.poll() is not None:
                    remaining = _os.read(stdout_fd, 65536)
                    if remaining:
                        full_output += remaining.decode("utf-8", errors="replace")
                    break

        process.wait()
        stderr_text = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
        return full_output, stderr_text, False

    def resume(self, cwd: Optional[str] = None) -> str:
        self.last_active = time.time()
        self.is_resumed = True

        timeout = self._get_execution_timeout()
        cli_name = self._get_cli_name()

        try:
            result = subprocess.run(
                self._build_resume_cmd(),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

            output = result.stdout.strip()
            output = self._clean_output(output)

            return output if output else "✅ 会话已恢复"

        except subprocess.TimeoutExpired:
            return "⏱️ 恢复会话超时"
        except FileNotFoundError:
            return f"❌ 未找到 {cli_name} 命令"
        except Exception as e:
            return f"❌ 恢复会话异常: {str(e)}"

    def to_snapshot(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "last_query": self.last_query,
            "is_resumed": self.is_resumed,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "BaseSession":
        session = cls(
            chat_id=data["chat_id"],
            session_id=data.get("session_id", ""),
            created_at=data.get("created_at", time.time()),
            last_active=data.get("last_active", time.time()),
            message_count=data.get("message_count", 0),
            last_query=data.get("last_query", ""),
            is_resumed=data.get("is_resumed", False),
        )
        return session

    def _clean_output(self, output: str) -> str:
        return clean_terminal_output(output)
