from __future__ import annotations

import logging
import shutil
from typing import Optional

from ..acp.helper import fetch_acp_models, list_acp_tools
from ..acp.providers import tool_registry
from ..ttadk import get_ttadk_manager
from .selection import WorktreeToolOption

logger = logging.getLogger(__name__)


class WorktreeToolDiscovery:
    """Discovers available tools and their models from ACP, CLI, and TTADK providers."""

    def get_available_tools(self) -> list[dict]:
        """Return available tools as dicts suitable for card builders.

        Probes three provider categories:
        1. ACP direct tools (coco, aiden, codex, gemini)
        2. CLI tools (claude)
        3. TTADK-managed tools (filtered by shutil.which)
        """
        tools: list[dict] = []
        seen: set[str] = set()

        # --- ACP tools ---
        acp_tools = list_acp_tools()
        for t in acp_tools:
            name = t.name
            if name in seen:
                continue
            if shutil.which(name):
                provider = tool_registry.get_provider(name)
                skip = (
                    getattr(provider, "skip_model_selection", False)
                    if provider
                    else False
                )
                tools.append(
                    WorktreeToolOption(
                        provider="acp",
                        tool_name=name,
                        display_name=name.capitalize(),
                        description=t.description,
                        supports_model=True,
                        model_optional=True,
                        skip_model_selection=skip,
                    ).__dict__
                )
                seen.add(name)

        # --- CLI tools ---
        if "claude" not in seen and shutil.which("claude"):
            tools.append(
                WorktreeToolOption(
                    provider="cli",
                    tool_name="claude",
                    display_name="Claude",
                    description="Anthropic Claude CLI",
                    supports_model=False,
                ).__dict__
            )
            seen.add("claude")

        # --- TTADK tools ---
        try:
            manager = get_ttadk_manager()
            result = manager.get_tools()
            for t in result.tools:
                name = t.name
                if name in seen:
                    continue
                tools.append(
                    WorktreeToolOption(
                        provider="ttadk",
                        tool_name=name,
                        display_name=t.description or name,
                        description=f"TTADK · {name}",
                        supports_model=True,
                        model_optional=True,
                        skip_model_selection=getattr(t, "skip_model_selection", False),
                    ).__dict__
                )
                seen.add(name)
        except Exception:
            pass

        return tools

    def get_models_for_tool(
        self,
        tool_name: str,
        provider: str = "ttadk",
        cwd: Optional[str] = None,
        current_model: Optional[str] = None,
    ) -> list[dict]:
        """Return available models for a tool (ACP or TTADK) as dicts for card builder."""
        if provider == "acp":
            try:
                acp_models = fetch_acp_models(
                    tool_name, cwd=cwd, current_model=current_model
                )
                return [
                    {
                        "name": m.name,
                        "display_name": m.description or m.name,
                        "is_default": m.is_default,
                    }
                    for m in acp_models
                ]
            except Exception:
                return []

        try:
            manager = get_ttadk_manager()
            models_result = manager.get_models(tool_name=tool_name, cwd=cwd)
            models = []
            for m in (models_result.models if models_result else []):
                models.append(
                    {
                        "name": m.name,
                        "display_name": getattr(m, "friendly_name", None)
                        or getattr(m, "display_name", None)
                        or m.name,
                        "is_default": getattr(m, "is_default", False),
                    }
                )
            return models
        except Exception:
            return []
