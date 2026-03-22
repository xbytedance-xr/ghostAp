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

    # ------------------------------------------------------------------
    # lowerCamelCase aliases (compat / gradual refactor)
    # ------------------------------------------------------------------
    def getMode(self, chatId: str, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`get_mode()`。"""
        return self.get_mode(chatId, project_id=projectId)

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

    def setMode(
        self,
        chatId: str,
        mode: InteractionMode,
        autoEntered: bool = False,
        projectId: Optional[str] = None,
    ) -> InteractionMode:
        """lowerCamelCase 兼容别名：`set_mode()`。"""
        return self.set_mode(chatId, mode, auto_entered=autoEntered, project_id=projectId)

    def clear_project_mode(self, project_id: str) -> Optional[InteractionMode]:
        """Clear the mode for a specific project.

        Returns:
            The previous mode if it existed, None otherwise
        """
        with self._lock:
            old_state = self._project_modes.pop(project_id, None)
            return old_state.mode if old_state else None

    def clearProjectMode(self, projectId: str) -> Optional[InteractionMode]:
        """lowerCamelCase 兼容别名：`clear_project_mode()`。"""
        return self.clear_project_mode(projectId)

    def get_project_mode(self, project_id: str) -> Optional[InteractionMode]:
        """Get the mode for a specific project (without fallback).

        Returns:
            The project's mode if set, None otherwise
        """
        with self._lock:
            state = self._project_modes.get(project_id)
            return state.mode if state else None

    def getProjectMode(self, projectId: str) -> Optional[InteractionMode]:
        """lowerCamelCase 兼容别名：`get_project_mode()`。"""
        return self.get_project_mode(projectId)

    def enter_coco_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Coco 编程模式。"""
        return self.set_mode(chat_id, InteractionMode.COCO, auto_entered=auto, project_id=project_id)

    def enterCocoMode(self, chatId: str, auto: bool = False, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`enter_coco_mode()`。"""
        return self.enter_coco_mode(chatId, auto=auto, project_id=projectId)

    def enter_claude_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Claude 编程模式。"""
        return self.set_mode(chat_id, InteractionMode.CLAUDE, auto_entered=auto, project_id=project_id)

    def enterClaudeMode(self, chatId: str, auto: bool = False, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`enter_claude_mode()`。"""
        return self.enter_claude_mode(chatId, auto=auto, project_id=projectId)

    def enter_aiden_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Aiden 编程模式。"""
        return self.set_mode(chat_id, InteractionMode.AIDEN, auto_entered=auto, project_id=project_id)

    def enterAidenMode(self, chatId: str, auto: bool = False, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`enter_aiden_mode()`。"""
        return self.enter_aiden_mode(chatId, auto=auto, project_id=projectId)

    def enter_codex_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Codex 编程模式。"""
        return self.set_mode(chat_id, InteractionMode.CODEX, auto_entered=auto, project_id=project_id)

    def enterCodexMode(self, chatId: str, auto: bool = False, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`enter_codex_mode()`。"""
        return self.enter_codex_mode(chatId, auto=auto, project_id=projectId)

    def enter_ttadk_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 TTADK 编程模式。"""
        return self.set_mode(chat_id, InteractionMode.TTADK, auto_entered=auto, project_id=project_id)

    def enter_gemini_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Gemini 编程模式。"""
        return self.set_mode(chat_id, InteractionMode.GEMINI, auto_entered=auto, project_id=project_id)

    def enterGeminiMode(self, chatId: str, auto: bool = False, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`enter_gemini_mode()`。"""
        return self.enter_gemini_mode(chatId, auto=auto, project_id=projectId)

    def enterTtadkMode(self, chatId: str, auto: bool = False, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`enter_ttadk_mode()`。"""
        return self.enter_ttadk_mode(chatId, auto=auto, project_id=projectId)

    def enter_shell_mode(self, chat_id: str, project_id: Optional[str] = None) -> InteractionMode:
        """进入 Shell 模式。"""
        return self.set_mode(chat_id, InteractionMode.SHELL, auto_entered=False, project_id=project_id)

    def enterShellMode(self, chatId: str, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`enter_shell_mode()`。"""
        return self.enter_shell_mode(chatId, project_id=projectId)

    def exit_to_smart(self, chat_id: str, project_id: Optional[str] = None) -> InteractionMode:
        """退出到 SMART 模式。"""
        return self.set_mode(chat_id, InteractionMode.SMART, auto_entered=False, project_id=project_id)

    def exitToSmart(self, chatId: str, projectId: Optional[str] = None) -> InteractionMode:
        """lowerCamelCase 兼容别名：`exit_to_smart()`。"""
        return self.exit_to_smart(chatId, project_id=projectId)

    def is_coco_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Coco 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.COCO

    def isCocoMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_coco_mode()`。"""
        return self.is_coco_mode(chatId, project_id=projectId)

    def is_claude_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Claude 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.CLAUDE

    def isClaudeMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_claude_mode()`。"""
        return self.is_claude_mode(chatId, project_id=projectId)

    def is_aiden_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Aiden 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.AIDEN

    def isAidenMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_aiden_mode()`。"""
        return self.is_aiden_mode(chatId, project_id=projectId)

    def is_codex_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Codex 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.CODEX

    def isCodexMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_codex_mode()`。"""
        return self.is_codex_mode(chatId, project_id=projectId)

    def is_ttadk_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 TTADK 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.TTADK

    def isTtadkMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_ttadk_mode()`。"""
        return self.is_ttadk_mode(chatId, project_id=projectId)

    def is_gemini_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Gemini 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.GEMINI

    def isGeminiMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_gemini_mode()`。"""
        return self.is_gemini_mode(chatId, project_id=projectId)

    def is_smart_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 SMART 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.SMART

    def isSmartMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_smart_mode()`。"""
        return self.is_smart_mode(chatId, project_id=projectId)

    def is_shell_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断当前是否为 Shell 模式。"""
        return self.get_mode(chat_id, project_id) == InteractionMode.SHELL

    def isShellMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_shell_mode()`。"""
        return self.is_shell_mode(chatId, project_id=projectId)

    def is_programming_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        """判断是否处于编程模式（COCO/CLAUDE/AIDEN/CODEX/GEMINI/TTADK）。"""
        mode = self.get_mode(chat_id, project_id)
        return mode in (
            InteractionMode.COCO,
            InteractionMode.CLAUDE,
            InteractionMode.AIDEN,
            InteractionMode.CODEX,
            InteractionMode.GEMINI,
            InteractionMode.TTADK,
        )

    def isProgrammingMode(self, chatId: str, projectId: Optional[str] = None) -> bool:
        """lowerCamelCase 兼容别名：`is_programming_mode()`。"""
        return self.is_programming_mode(chatId, project_id=projectId)

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

    def getModeDisplayName(self, chatId: str, projectId: Optional[str] = None) -> str:
        """lowerCamelCase 兼容别名：`get_mode_display_name()`。"""
        return self.get_mode_display_name(chatId, project_id=projectId)
