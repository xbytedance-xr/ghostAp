from enum import Enum
from typing import Optional
from dataclasses import dataclass
import threading


class InteractionMode(Enum):
    SMART = "smart"
    COCO = "coco"


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

    def get_state(self, chat_id: str) -> ModeState:
        with self._lock:
            return self._modes.get(chat_id, ModeState())

    def set_mode(self, chat_id: str, mode: InteractionMode, auto_entered: bool = False) -> InteractionMode:
        with self._lock:
            old_state = self._modes.get(chat_id, ModeState())
            old_mode = old_state.mode
            self._modes[chat_id] = ModeState(mode=mode, auto_entered=auto_entered)
            return old_mode

    def enter_coco_mode(self, chat_id: str, auto: bool = False) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.COCO, auto_entered=auto)

    def exit_to_smart(self, chat_id: str) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.SMART, auto_entered=False)

    def is_coco_mode(self, chat_id: str) -> bool:
        return self.get_mode(chat_id) == InteractionMode.COCO

    def is_smart_mode(self, chat_id: str) -> bool:
        return self.get_mode(chat_id) == InteractionMode.SMART

    def was_auto_entered(self, chat_id: str) -> bool:
        with self._lock:
            state = self._modes.get(chat_id)
            return state.auto_entered if state else False

    def get_mode_display_name(self, chat_id: str) -> str:
        mode = self.get_mode(chat_id)
        return {
            InteractionMode.SMART: "🧠 智能模式",
            InteractionMode.COCO: "🤖 编程模式",
        }.get(mode, "未知模式")
