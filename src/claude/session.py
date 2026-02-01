import subprocess
import time
import re
import uuid
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable, Generator
from ..config import get_settings


@dataclass
class ClaudeSession:
    chat_id: str
    session_id: str = field(default="")
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    last_query: str = ""
    is_resumed: bool = False

    def __post_init__(self):
        if not self.session_id:
            self.session_id = str(uuid.uuid4())

    def send_prompt(self, prompt: str, timeout: Optional[int] = None, cwd: Optional[str] = None, resume: bool = False) -> str:
        self.last_active = time.time()
        self.message_count += 1
        self.last_query = prompt

        settings = get_settings()
        if timeout is None:
            timeout = settings.coco_execution_timeout

        try:
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

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

            # If resume failed (session expired/not found), create new session and retry
            if result.stderr and "No conversation found with session ID" in result.stderr:
                print(f"⚠️ Claude 会话 {self.session_id} 已失效，创建新会话重试")
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
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=cwd,
                )

            output = result.stdout.strip()
            if result.stderr:
                output += f"\n\n⚠️ stderr:\n{result.stderr.strip()}"

            output = self._clean_output(output)

            settings = get_settings()
            max_len = settings.coco_max_output_length
            if len(output) > max_len:
                output = output[:max_len] + f"\n\n... (输出被截断，共 {len(output)} 字符)"

            return output if output else "✅ 执行完成（无输出）"

        except subprocess.TimeoutExpired:
            return f"⏱️ Claude 执行超时（{timeout}秒）"
        except FileNotFoundError:
            return "❌ 未找到 claude 命令，请确保已安装 Claude Code CLI"
        except Exception as e:
            return f"❌ Claude 执行异常: {str(e)}"

    def send_prompt_streaming(
        self,
        prompt: str,
        on_chunk: Callable[[str], None],
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        resume: bool = False,
        chunk_interval: float = 0.3
    ) -> str:
        self.last_active = time.time()
        self.message_count += 1
        self.last_query = prompt

        settings = get_settings()
        if timeout is None:
            timeout = settings.coco_execution_timeout

        try:
            cmd = [
                "claude",
                "-p",
                "--dangerously-skip-permissions",
            ]

            if self.message_count == 1 and not self.is_resumed:
                cmd.extend(["--session-id", self.session_id])
            else:
                cmd.extend(["--resume", self.session_id])

            cmd.append(prompt)

            full_output, stderr_text, timed_out = self._run_streaming_process(
                cmd, cwd, timeout, on_chunk, chunk_interval
            )

            if timed_out:
                return f"⏱️ Claude 执行超时（{timeout}秒）"

            # If resume failed (session expired/not found), create new session and retry
            if stderr_text and "No conversation found with session ID" in stderr_text:
                print(f"⚠️ Claude 会话 {self.session_id} 已失效，创建新会话重试")
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
                full_output, stderr_text, timed_out = self._run_streaming_process(
                    cmd, cwd, timeout, on_chunk, chunk_interval
                )

                if timed_out:
                    return f"⏱️ Claude 执行超时（{timeout}秒）"

            if stderr_text:
                full_output += f"\n\n⚠️ stderr:\n{stderr_text.strip()}"

            output = self._clean_output(full_output.strip())

            max_len = settings.coco_max_output_length
            if len(output) > max_len:
                output = output[:max_len] + f"\n\n... (输出被截断，共 {len(output)} 字符)"

            on_chunk(output)

            return output if output else "✅ 执行完成（无输出）"

        except FileNotFoundError:
            error_msg = "❌ 未找到 claude 命令，请确保已安装 Claude Code CLI"
            on_chunk(error_msg)
            return error_msg
        except Exception as e:
            error_msg = f"❌ Claude 执行异常: {str(e)}"
            on_chunk(error_msg)
            return error_msg

    def _run_streaming_process(self, cmd, cwd, timeout, on_chunk, chunk_interval):
        """Run command with streaming output. Returns (full_output, stderr_text, timed_out).

        Uses os.read() instead of readline() to avoid buffering issues in
        non-TTY environments — Claude CLI may not flush per-line, causing
        readline() to block until the process finishes.
        """
        import os as _os
        import select

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

            # Wait for data with a short timeout so we can check overall timeout
            ready, _, _ = select.select([stdout_fd], [], [], 0.2)
            if ready:
                chunk = _os.read(stdout_fd, 4096)
                if not chunk:
                    # EOF
                    break
                full_output += chunk.decode("utf-8", errors="replace")
                current_time = time.time()
                if current_time - last_update_time >= chunk_interval:
                    cleaned = self._clean_output(full_output.strip())
                    if cleaned:
                        on_chunk(cleaned)
                    last_update_time = current_time
            else:
                # No data ready; check if process has exited
                if process.poll() is not None:
                    # Drain any remaining output
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

        settings = get_settings()
        timeout = settings.coco_execution_timeout

        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--dangerously-skip-permissions",
                    "--resume", self.session_id,
                    "继续之前的对话"
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )

            output = result.stdout.strip()
            output = self._clean_output(output)

            return output if output else "✅ 会话已恢复"

        except subprocess.TimeoutExpired:
            return f"⏱️ 恢复会话超时"
        except FileNotFoundError:
            return "❌ 未找到 claude 命令"
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
    def from_snapshot(cls, data: dict) -> "ClaudeSession":
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
        output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
        output = re.sub(r'\x1b\][^\x07]*\x07', '', output)
        output = re.sub(r'\x1b[\[\]\\^][^\x07\x1b]*', '', output)
        return output.strip()


class ClaudeSessionManager:
    def __init__(self):
        self._sessions: dict[str, ClaudeSession] = {}
        settings = get_settings()
        self._session_timeout = settings.coco_session_timeout

    def start_session(self, chat_id: str, session_id: Optional[str] = None) -> ClaudeSession:
        if session_id:
            session = ClaudeSession(chat_id=chat_id, session_id=session_id)
        else:
            session = ClaudeSession(chat_id=chat_id)
        self._sessions[chat_id] = session
        return session

    def resume_session(self, chat_id: str, session_id: str) -> ClaudeSession:
        session = ClaudeSession(
            chat_id=chat_id,
            session_id=session_id,
            is_resumed=True
        )
        self._sessions[chat_id] = session
        return session

    def get_session(self, chat_id: str) -> Optional[ClaudeSession]:
        session = self._sessions.get(chat_id)
        if session:
            if time.time() - session.last_active > self._session_timeout:
                self.end_session(chat_id)
                return None
        return session

    def end_session(self, chat_id: str) -> Optional[dict]:
        if chat_id in self._sessions:
            session = self._sessions[chat_id]
            snapshot = session.to_snapshot()
            del self._sessions[chat_id]
            return snapshot
        return None

    def is_in_claude_mode(self, chat_id: str) -> bool:
        return self.get_session(chat_id) is not None

    def get_session_info(self, chat_id: str) -> Optional[str]:
        session = self.get_session(chat_id)
        if not session:
            return None

        duration = int(time.time() - session.created_at)
        minutes = duration // 60
        seconds = duration % 60

        resumed_info = " (已恢复)" if session.is_resumed else ""

        return (
            f"📊 Claude 会话信息{resumed_info}:\n"
            f"- 会话ID: {session.session_id}\n"
            f"- 消息数: {session.message_count}\n"
            f"- 持续时间: {minutes}分{seconds}秒"
        )
