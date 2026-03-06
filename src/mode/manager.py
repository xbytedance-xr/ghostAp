from enum import Enum
from typing import Optional
from dataclasses import dataclass
import threading


class InteractionMode(Enum):
    SMART = "smart"
    COCO = "coco"
    CLAUDE = "claude"
    SHELL = "shell"
    TTADK = "ttadk"


@dataclass
class ModeState:
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

    def set_mode(
        self,
        chat_id: str,
        mode: InteractionMode,
        auto_entered: bool = False,
        project_id: Optional[str] = None,
    ) -> InteractionMode:
        """Set the interaction mode.
        
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

    def enter_coco_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.COCO, auto_entered=auto, project_id=project_id)

    def enter_claude_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.CLAUDE, auto_entered=auto, project_id=project_id)

    def enter_ttadk_mode(self, chat_id: str, auto: bool = False, project_id: Optional[str] = None) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.TTADK, auto_entered=auto, project_id=project_id)

    def enter_shell_mode(self, chat_id: str, project_id: Optional[str] = None) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.SHELL, auto_entered=False, project_id=project_id)

    def exit_to_smart(self, chat_id: str, project_id: Optional[str] = None) -> InteractionMode:
        return self.set_mode(chat_id, InteractionMode.SMART, auto_entered=False, project_id=project_id)

    def is_coco_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        return self.get_mode(chat_id, project_id) == InteractionMode.COCO

    def is_claude_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        return self.get_mode(chat_id, project_id) == InteractionMode.CLAUDE

    def is_ttadk_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        return self.get_mode(chat_id, project_id) == InteractionMode.TTADK

    def is_smart_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        return self.get_mode(chat_id, project_id) == InteractionMode.SMART

    def is_shell_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        return self.get_mode(chat_id, project_id) == InteractionMode.SHELL

    def is_programming_mode(self, chat_id: str, project_id: Optional[str] = None) -> bool:
        mode = self.get_mode(chat_id, project_id)
        return mode in (InteractionMode.COCO, InteractionMode.CLAUDE, InteractionMode.TTADK)

    def get_mode_display_name(self, chat_id: str, project_id: Optional[str] = None) -> str:
        mode = self.get_mode(chat_id, project_id)
        return {
            InteractionMode.SMART: "🧠 智能模式",
            InteractionMode.COCO: "🤖 Coco 编程模式",
            InteractionMode.CLAUDE: "🔮 Claude 编程模式",
            InteractionMode.SHELL: "💻 Shell 模式",
            InteractionMode.TTADK: "🎮 TTADK 编程模式",
        }.get(mode, "未知模式")
