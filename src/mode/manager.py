from enum import Enum
from typing import Optional
from dataclasses import dataclass
import threading


class InteractionMode(Enum):
    SMART = "smart"
    COCO = "coco"
    CLAUDE = "claude"
    SHELL = "shell"


@dataclass
class ModeState:
    mode: InteractionMode = InteractionMode.SMART
    auto_entered: bool = False


class ModeManager:
    def __init__(self):
        self._modes: dict[str, ModeState] = {}
        self._lock = threading.Lock()

    def get_mode(self, chat_id: str) -> InteractionMode:
        with self._lock:
            state = self._modes.get(chat_id)
            return state.mode if state else InteractionMode.SMART

    def set_mode(self, chat_id: str, mode: InteractionMode, auto_entered: bool = False) -> InteractionMode:
        with self._lock:
            old_state = self._modes.get(chat_id, ModeState())
            old_mode = old_state.mode
            self._modes[chat_id] = ModeState(mode=mode, auto_entered=auto_entered)
            return old_mode

    def enter_coco_mode(self, chat_id: str, auto: bool = False) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.COCO, auto_entered=auto)

    def enter_claude_mode(self, chat_id: str, auto: bool = False) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.CLAUDE, auto_entered=auto)

    def enter_shell_mode(self, chat_id: str) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.SHELL, auto_entered=False)

    def exit_to_smart(self, chat_id: str) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.SMART, auto_entered=False)

    def is_coco_mode(self, chat_id: str) -> bool:
        return self.get_mode(chat_id) == InteractionMode.COCO

    def is_claude_mode(self, chat_id: str) -> bool:
        return self.get_mode(chat_id) == InteractionMode.CLAUDE

    def is_smart_mode(self, chat_id: str) -> bool:
        return self.get_mode(chat_id) == InteractionMode.SMART

    def is_shell_mode(self, chat_id: str) -> bool:
        return self.get_mode(chat_id) == InteractionMode.SHELL

    def is_programming_mode(self, chat_id: str) -> bool:
        mode = self.get_mode(chat_id)
        return mode in (InteractionMode.COCO, InteractionMode.CLAUDE)

    def get_mode_display_name(self, chat_id: str) -> str:
        mode = self.get_mode(chat_id)
        return {
            InteractionMode.SMART: "🧠 智能模式",
            InteractionMode.COCO: "🤖 Coco 编程模式",
            InteractionMode.CLAUDE: "🔮 Claude 编程模式",
            InteractionMode.SHELL: "💻 Shell 模式",
        }.get(mode, "未知模式")
