"""Coco provider implementation for ACP mode."""

from typing import Optional

from ..provider import ACPProvider
from ..sync_adapter import _resolve_with_auto_update


class CocoProvider(ACPProvider):
    @property
    def name(self) -> str:
        return "coco"

    def check_availability(self) -> bool:
        """Check if coco is available and supports ACP mode."""
        return _resolve_with_auto_update("coco")

    def get_serve_command(self, model_name: Optional[str] = None) -> tuple[str, list[str]]:
        """Get the command to start the coco ACP server."""
        args = ["acp", "serve"]
        if model_name:
            args.extend(["-c", f"model.name={model_name}"])
        return "coco", args
