from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from typing import Optional

from ..acp.helper import fetch_acp_models
from ..acp.providers import get_providers, tool_registry
from ..ttadk import get_ttadk_manager
from .selection import WorktreeToolOption

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _KnownTool:
    """Static definition of a top-level tool candidate."""

    name: str
    display_name: str
    description: str
    priority: int


# All known top-level tools, ordered by priority.
# Each tool appears if its binary is found via shutil.which().
_KNOWN_TOOLS: tuple[_KnownTool, ...] = (
    _KnownTool("coco", "Coco", "字节跳动 AI", 0),
    _KnownTool("aiden", "Aiden", "Aiden CLI", 1),
    _KnownTool("codex", "Codex", "OpenAI Codex", 2),
    _KnownTool("claude", "Claude", "Anthropic Claude CLI", 3),
    _KnownTool("gemini", "Gemini", "Google Gemini CLI", 4),
)


class WorktreeToolDiscovery:
    """Discovers available tools and their models from ACP, CLI, and TTADK providers."""

    _TOP_LEVEL_PRIORITY = {
        ("acp", "coco"): 0,
        ("cli", "coco"): 0,
        ("acp", "aiden"): 1,
        ("cli", "aiden"): 1,
        ("acp", "codex"): 2,
        ("cli", "codex"): 2,
        ("acp", "claude"): 3,
        ("cli", "claude"): 3,
        ("acp", "gemini"): 4,
        ("cli", "gemini"): 4,
        ("ttadk", "ttadk"): 90,
    }

    def get_available_tools(self) -> list[dict]:
        """Return available tools as dicts suitable for card builders.

        Discovery logic:
        1. Known tools (coco, aiden, codex, claude, gemini) — appear if binary
           is found via shutil.which(). Uses ACP provider when available for
           model selection, otherwise falls back to CLI provider.
        2. TTADK-managed tools — unified entry when TTADK returns tools.
        """
        # 触发 ACP provider 的 lazy 注册——否则 tool_registry 默认为空，所有 ACP
        # 工具会被错判为 CLI 模式（supports_model=False），导致 selection_key 撞车。
        get_providers()
        tools: list[dict] = []
        seen: set[str] = set()

        # --- Known top-level tools (same level for all) ---
        for known in _KNOWN_TOOLS:
            if not shutil.which(known.name):
                continue
            if known.name in seen:
                continue

            # Determine provider: prefer ACP if provider is registered
            provider_obj = tool_registry.get_provider(known.name)
            if provider_obj:
                provider_type = "acp"
                supports_model = True
                model_optional = True
                # Worktree 场景的核心语义是"工具 × 模型"组合作为最小单位，
                # 因此即便 ACP provider 配置了 skip_model_selection（如 Coco/Aiden 在普通
                # ACP 启动时为简化交互而跳过），worktree 仍必须弹模型选择卡，否则同工具
                # 多次添加会因 selection_key 撞车被 dedup 拦截。
                skip = False
            else:
                provider_type = "cli"
                supports_model = False
                model_optional = False
                skip = False

            tools.append(
                WorktreeToolOption(
                    provider=provider_type,
                    tool_name=known.name,
                    display_name=known.display_name,
                    description=known.description,
                    supports_model=supports_model,
                    model_optional=model_optional,
                    skip_model_selection=skip,
                ).__dict__
            )
            seen.add(known.name)

        # --- TTADK entry ---
        ttadk_tools = self.get_ttadk_tools()
        if ttadk_tools:
            tools.append(
                WorktreeToolOption(
                    provider="ttadk",
                    tool_name="ttadk",
                    display_name="TTADK",
                    description="TTADK 多工具入口",
                    supports_model=False,
                ).__dict__
            )

        return self._sort_top_level_tools(tools)

    def _sort_top_level_tools(self, tools: list[dict]) -> list[dict]:
        def key(item: dict) -> tuple[int, str]:
            priority = self._TOP_LEVEL_PRIORITY.get(
                (item.get("provider"), item.get("tool_name")),
                50,
            )
            return priority, str(item.get("display_name") or item.get("tool_name") or "")

        return sorted(tools, key=key)

    def get_ttadk_tools(self) -> list[dict]:
        tools: list[dict] = []
        try:
            manager = get_ttadk_manager()
            result = manager.get_tools()
            for t in result.tools:
                name = str(t.name or "").strip()
                if not name:
                    continue
                tools.append(
                    WorktreeToolOption(
                        provider="ttadk",
                        tool_name=name,
                        display_name=f"TTADK · {name}",
                        description=f"TTADK · {name}",
                        supports_model=True,
                        model_optional=True,
                        skip_model_selection=getattr(t, "skip_model_selection", False),
                    ).__dict__
                )
        except Exception:
            logger.debug("TTADK tool discovery failed", exc_info=True)
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
            models_result = manager.get_models(
                tool_name=tool_name,
                cwd=cwd,
                force_refresh=True,
            )
            warnings = list(getattr(models_result, "warnings", []) or [])
            source = str(getattr(models_result, "source", "") or "").strip().lower()
            if source == "defaults" or "models_untrusted" in warnings:
                return []
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
