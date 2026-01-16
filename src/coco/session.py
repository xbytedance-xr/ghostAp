import subprocess
import time
import re
from dataclasses import dataclass, field
from typing import Optional
from ..config import get_settings


@dataclass
class CocoSession:
    chat_id: str
    session_id: str = field(default="")
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0

    def __post_init__(self):
        if not self.session_id:
            self.session_id = f"feishu_{self.chat_id}_{int(self.created_at)}"

    def send_prompt(self, prompt: str, timeout: Optional[int] = None, cwd: Optional[str] = None) -> str:
        self.last_active = time.time()
        self.message_count += 1

        settings = get_settings()
        if timeout is None:
            timeout = settings.coco_execution_timeout

        try:
            result = subprocess.run(
                [
                    "coco",
                    "-p",
                    "-y",
                    "--session-id", self.session_id,
                    prompt
                ],
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
            max_len = settings.sandbox_max_output_length
            if len(output) > max_len:
                output = output[:max_len] + f"\n\n... (输出被截断，共 {len(output)} 字符)"

            return output if output else "✅ 执行完成（无输出）"

        except subprocess.TimeoutExpired:
            return f"⏱️ Coco 执行超时（{timeout}秒）"
        except FileNotFoundError:
            return "❌ 未找到 coco 命令，请确保已安装 coco"
        except Exception as e:
            return f"❌ Coco 执行异常: {str(e)}"

    def _clean_output(self, output: str) -> str:
        output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
        output = re.sub(r'\x1b\][^\x07]*\x07', '', output)
        output = re.sub(r'\x1b[\[\]\\^][^\x07\x1b]*', '', output)
        return output.strip()


class CocoSessionManager:
    def __init__(self):
        self._sessions: dict[str, CocoSession] = {}
        settings = get_settings()
        self._session_timeout = settings.coco_session_timeout

    def start_session(self, chat_id: str) -> CocoSession:
        session = CocoSession(chat_id=chat_id)
        self._sessions[chat_id] = session
        return session

    def get_session(self, chat_id: str) -> Optional[CocoSession]:
        session = self._sessions.get(chat_id)
        if session:
            if time.time() - session.last_active > self._session_timeout:
                self.end_session(chat_id)
                return None
        return session

    def end_session(self, chat_id: str) -> bool:
        if chat_id in self._sessions:
            del self._sessions[chat_id]
            return True
        return False

    def is_in_coco_mode(self, chat_id: str) -> bool:
        return self.get_session(chat_id) is not None

    def get_session_info(self, chat_id: str) -> Optional[str]:
        session = self.get_session(chat_id)
        if not session:
            return None

        duration = int(time.time() - session.created_at)
        minutes = duration // 60
        seconds = duration % 60

        return (
            f"📊 Coco 会话信息:\n"
            f"- 会话ID: {session.session_id}\n"
            f"- 消息数: {session.message_count}\n"
            f"- 持续时间: {minutes}分{seconds}秒"
        )
