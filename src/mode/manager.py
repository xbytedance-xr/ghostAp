import threading
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class InteractionMode(Enum):
    """交互模式枚举（SMART/COCO/CLAUDE/SHELL/TTADK/AIDEN/CODEX/GEMINI）。"""

    SMART = "smart"
    COCO = "coco"
    CLAUDE = "claude"
    AIDEN = "aiden"
    CODEX = "codex"
    GEMINI = "gemini"
    SHELL = "shell"
    TTADK = "ttadk"


@dataclass
class ModeState:
    """某个 chat 或 project 的模式状态。"""

    mode: InteractionMode = InteractionMode.SMART
    auto_entered: bool = False


class ModeManager:
    """Manages interaction modes at both chat and project levels.

    Mode resolution order:
    1. If project_id is provided and has a mode set, use project mode
    2. Otherwise, fall back to chat-level mode
    3. Default is SMART mode

    This allows different projects to have independent coding modes
    while maintaining backward compatibility with chat-level mode.
    """

    def __init__(self):
        self._chat_modes: dict[str, ModeState] = {}
        self._project_modes: dict[str, ModeState] = {}
        self._lock = threading.Lock()
        self._programming_modes = (
            InteractionMode.COCO,
            InteractionMode.CLAUDE,
            InteractionMode.AIDEN,
            InteractionMode.CODEX,
            InteractionMode.GEMINI,
            InteractionMode.TTADK,
        )

    def get_mode(self, chat_id: str, project_id: Optional[str] = None) -> InteractionMode:
        """Get the current interaction mode.

        Args:
            chat_id: The chat identifier
            project_id: Optional project identifier for project-level mode lookup

        Returns:
            The current interaction mode (project mode > chat mode > SMART)
        """
        with self._lock:
            if project_id:
                project_state = self._project_modes.get(project_id)
                if project_state:
                    return project_state.mode
            chat_state = self._chat_modes.get(chat_id)
            return chat_state.mode if chat_state else InteractionMode.SMART

    def set_mode(
        self,
        chat_id: str,
        mode: InteractionMode,
        auto_entered: bool = False,
        project_id: Optional[str] = None,
    ) -> InteractionMode:
        """设置交互模式（chat 级或 project 级）。

        设计说明：
        - `project_id` 非空：只影响该项目；同一 chat 的其他项目不受影响。
        - `project_id` 为空：设置 chat 级默认模式（兼容旧行为）。
        - `auto_entered=True`：标记为“自动进入”（例如回复链路自动切换），便于 UI/审计区分。

        Args:
            chat_id: The chat identifier
            mode: The new mode to set
            auto_entered: Whether the mode was auto-entered (e.g., by replying to a message)
            project_id: If provided, set mode at project level; otherwise at chat level

        Returns:
            The previous mode
        """
        with self._lock:
            if project_id:
                old_state = self._project_modes.get(project_id, ModeState())
                old_mode = old_state.mode
                self._project_modes[project_id] = ModeState(mode=mode, auto_entered=auto_entered)
            else:
                old_state = self._chat_modes.get(chat_id, ModeState())
                old_mode = old_state.mode
                self._chat_modes[chat_id] = ModeState(mode=mode, auto_entered=auto_entered)
            return old_mode

    def clear_project_mode(self, project_id: str) -> Optional[InteractionMode]:
        """Clear the mode for a specific project.

        Returns:
            The previous mode if it existed, None otherwise
        """
        with self._lock:
            old_state = self._project_modes.pop(project_id, None)
            return old_state.mode if old_state else None

    def get_project_mode(self, project_id: str) -> Optional[InteractionMode]:
        """Get the mode for a specific project (without fallback).

        Returns:
            The project's mode if set, None otherwise
        """
        with self._lock:
            state = self._project_modes.get(project_id)
            return state.mode if state else None

    def enter_programming_mode(
        self,
        chat_id: str,
        mode: InteractionMode,
        auto: bool = False,
        project_id: Optional[str] = None,
    ) -> InteractionMode:
        """统一进入编程模式入口。"""
        if mode not in self._programming_modes:
            raise ValueError(f"mode must be a programming mode, got: {mode}")
        return self.set_mode(chat_id, mode, auto_entered=auto, project_id=project_id)

    # Alias for callers preferring the shorter name
    enter_mode = enter_programming_mode

    def is_mode(self, chat_id: str, mode: InteractionMode, project_id: Optional[str] = None) -> bool:
        """Generic mode check — replaces per-mode is_*_mode methods."""
        return self.get_mode(chat_id, project_id) == mode

    def enter_coco_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Coco 编程模式。"""
        return self.enter_programming_mode(chat_id, InteractionMode.COCO, auto=auto, project_id=project_id)

    def enter_claude_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Claude 编程模式。"""
        return self.enter_programming_mode(chat_id, InteractionMode.CLAUDE, auto=auto, project_id=project_id)

    def enter_aiden_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Aiden 编程模式。"""
        return self.enter_programming_mode(chat_id, InteractionMode.AIDEN, auto=auto, project_id=project_id)

    def enter_codex_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Codex 编程模式。"""
        return self.enter_programming_mode(chat_id, InteractionMode.CODEX, auto=auto, project_id=project_id)

    def enter_ttadk_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 TTADK 编程模式。"""
        return self.enter_programming_mode(chat_id, InteractionMode.TTADK, auto=auto, project_id=project_id)

    def enter_gemini_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Gemini 编程模式。"""
        return self.enter_programming_mode(chat_id, InteractionMode.GEMINI, auto=auto, project_id=project_id)

    def enter_shell_mode(self, chat_id: str, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Shell 模式。"""
        return self.set_mode(chat_id, InteractionMode.SHELL, auto_entered=False, project_id=project_id)

    def exit_to_smart(self, chat_id: str, project_id: Optional[str] = None) -> InteractionMode:
        """退出到 SMART 模式。"""
        return self.set_mode(chat_id, InteractionMode.SMART, auto_entered=False, project_id=project_id)

    def is_coco_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Coco 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.COCO

    def is_claude_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Claude 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.CLAUDE

    def is_aiden_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Aiden 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.AIDEN

    def is_codex_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Codex 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.CODEX

    def is_ttadk_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 TTADK 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.TTADK

    def is_gemini_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Gemini 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.GEMINI

    def is_smart_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 SMART 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.SMART

    def is_shell_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Shell 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.SHELL

    def is_programming_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断是否处于编程模式（COCO/CLAUDE/AIDEN/CODEX/GEMINI/TTADK）。"""
        mode = self.get_mode(chat_id, project_id)
        return mode in self._programming_modes

    def get_mode_display_name(self, chat_id: str, project_id: Optional[str] = None) -> str:
        """返回当前模式的 UI 展示名。"""
        mode = self.get_mode(chat_id, project_id)
        return {
            InteractionMode.SMART: "🧠 智能模式",
            InteractionMode.COCO: "🤖 Coco 编程模式",
            InteractionMode.CLAUDE: "🔮 Claude 编程模式",
            InteractionMode.AIDEN: "🎯 Aiden 编程模式",
            InteractionMode.CODEX: "⚡ Codex 编程模式",
            InteractionMode.GEMINI: "✨ Gemini 编程模式",
            InteractionMode.SHELL: "💻 Shell 模式",
            InteractionMode.TTADK: "🎮 TTADK 编程模式",
        }.get(mode, "未知模式")
