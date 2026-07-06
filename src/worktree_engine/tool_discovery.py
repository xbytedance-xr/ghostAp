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
    _KnownTool("traex", "Traex", "TRAE CLI", 4),
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
        ("acp", "traex"): 4,
        ("cli", "traex"): 4,
        ("ttadk", "ttadk"): 90,
    }

    def get_available_tools(self) -> list[dict]:
        """Return available tools as dicts suitable for card builders.

        Discovery logic:
        1. Known tools (coco, aiden, codex, claude, traex) — appear if binary
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
            provider_obj = tool_registry.get_provider(known.name)
            has_cli = shutil.which(known.name) is not None
            has_acp = bool(provider_obj) and (
                has_cli or self._is_acp_provider_available(known.name, provider_obj)
            )
            if not has_cli and not has_acp:
                continue
            if known.name in seen:
                continue

            # Determine provider: prefer ACP if provider is available, including
            # package-backed fallbacks such as Codex ACP via npx.
            if provider_obj and has_acp:
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

    def _is_acp_provider_available(self, tool_name: str, provider_obj: object) -> bool:
        if not provider_obj:
            return False
        try:
            if tool_registry.get_availability(
                tool_name,
                allow_sync_probe=True,
                trigger_async_probe=False,
            ):
                return True
        except Exception:
            logger.debug("ACP availability check failed for %s", tool_name, exc_info=True)
        try:
            get_fallback = getattr(provider_obj, "get_fallback_command", None)
            return bool(callable(get_fallback) and get_fallback())
        except Exception:
            logger.debug("ACP fallback check failed for %s", tool_name, exc_info=True)
            return False

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
                        display_name=name,
                        agent_name="ttadk",
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
        force_refresh: bool = True,
    ) -> list[dict]:
        """Return available models for a tool (ACP or TTADK) as dicts for card builder."""
        if provider == "acp":
            try:
                acp_models = fetch_acp_models(
                    tool_name, cwd=cwd, current_model=current_model
                )
                # IMPORTANT: keep display_name == m.name (model_id) so the
                # worktree model select card shows e.g. "GPT-5.2" / "Doubao Pro"
                # in the title and button. ACP's `description` carries
                # quota/load metadata ("Model load: 14%, Quota: 48% used,
                # resets weekly, …") which is useful as secondary info but
                # must NOT be promoted into the visible name slot.
                return [
                    {
                        "name": m.name,
                        "display_name": m.name,
                        "description": m.description or "",
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
                force_refresh=force_refresh,
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
