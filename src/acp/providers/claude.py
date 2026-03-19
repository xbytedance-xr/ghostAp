"""Claude provider implementation for ACP mode."""

from typing import Optional

from ..provider import ACPProvider
from ..sync_adapter import _resolve_with_auto_update


class ClaudeProvider(ACPProvider):
    @property
    def name(self) -> str:
        return "claude"

    def check_availability(self) -> bool:
        """Check if claude is available and supports ACP mode."""
        return _resolve_with_auto_update("claude")

    def get_serve_command(self, model_name: Optional[str] = None) -> tuple[str, list[str]]:
        """Get the command to start the claude ACP server."""
        # Claude uses pure 'acp serve' via sync_adapter _resolve_with_auto_update checks
        return "claude", ["acp", "serve"]
