"""ACP tool providers package.

This package contains implementations of the ACPProvider interface for various
AI development tools (e.g., coco, claude, aiden, codex).
"""

from .aiden import AidenProvider
from .claude import ClaudeProvider
from .coco import CocoProvider
from .codex import CodexProvider
from .gemini import GeminiProvider
from ..provider import ToolRegistry, tool_registry

# Register standard providers
tool_registry.register(CocoProvider(), is_default=True)
tool_registry.register(ClaudeProvider())
tool_registry.register(AidenProvider())
tool_registry.register(CodexProvider())
tool_registry.register(GeminiProvider())

__all__ = [
    "ToolRegistry",
    "tool_registry",
    "CocoProvider",
    "ClaudeProvider",
    "AidenProvider",
    "CodexProvider",
    "GeminiProvider",
]
