"""Unified tool registry for Workflow Engine.

Discovers available tools from ACP providers and TTADK at runtime,
replacing the hardcoded TOOL_DESCRIPTIONS constant.

Usage:
    from .tool_registry import get_available_tools

    tools = get_available_tools()
    # -> {"coco": "全栈编程·支持 subagent", "claude": "Anthropic 深度推理", ...}
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache configuration
# ---------------------------------------------------------------------------

_CACHE_TTL_S: float = 300.0  # 5-minute cache
_cache_lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
_cached_tools: dict[str, str] | None = None
_cached_at: float = 0.0

# ---------------------------------------------------------------------------
# Fallback descriptions (used when provider probing fails)
# ---------------------------------------------------------------------------

_FALLBACK_DESCRIPTIONS: dict[str, str] = {
    "coco": "全栈编程·支持 subagent",
    "aiden": "代码审查·架构设计",
    "codex": "OpenAI 自主编程",
    "claude": "Anthropic 深度推理",
    "traex": "高并发推理·轻量任务",
    "gemini": "Google 多模态推理",
    "ttadk": "TTADK CLI 桥接",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_available_tools(*, force_refresh: bool = False) -> dict[str, str]:
    """Return a dict of tool_name -> description for all available tools.

    Merges ACP providers + TTADK tools. Results are cached for
    _CACHE_TTL_S seconds to avoid repeated probing.

    Thread-safe. On failure, returns the fallback dict.
    """
    global _cached_tools, _cached_at

    now = time.monotonic()
    if not force_refresh and _cached_tools is not None and (now - _cached_at) < _CACHE_TTL_S:
        return dict(_cached_tools)

    with _cache_lock:
        # Double-check after acquiring lock
        if not force_refresh and _cached_tools is not None and (time.monotonic() - _cached_at) < _CACHE_TTL_S:
            return dict(_cached_tools)

        tools = _discover_tools()
        _cached_tools = tools
        _cached_at = time.monotonic()
        return dict(tools)


def invalidate_cache() -> None:
    """Force the next get_available_tools() call to re-discover."""
    global _cached_tools, _cached_at
    with _cache_lock:
        _cached_tools = None
        _cached_at = 0.0


# ---------------------------------------------------------------------------
# Discovery implementation
# ---------------------------------------------------------------------------


def _discover_tools() -> dict[str, str]:
    """Merge ACP + TTADK tool lists into a unified dict."""
    result: dict[str, str] = {}

    # Phase 1: ACP providers (lightweight — no subprocess)
    try:
        result.update(_discover_acp_tools())
    except Exception as e:
        logger.debug("ACP tool discovery failed, using fallback: %s", repr(e))
        # Add ACP fallbacks
        for name in ("coco", "claude", "aiden", "codex", "gemini", "traex"):
            if name not in result:
                result[name] = _FALLBACK_DESCRIPTIONS.get(name, name)

    # Phase 2: TTADK tools (static defaults, no I/O)
    try:
        result.update(_discover_ttadk_tools())
    except Exception as e:
        logger.debug("TTADK tool discovery failed: %s", repr(e))
        if "ttadk" not in result:
            result["ttadk"] = _FALLBACK_DESCRIPTIONS.get("ttadk", "TTADK")

    # Ensure at least the fallback set is covered
    for name, desc in _FALLBACK_DESCRIPTIONS.items():
        if name not in result:
            result[name] = desc

    return result


def _discover_acp_tools() -> dict[str, str]:
    """Discover tools from ACP providers (lazy-init, no subprocess probing)."""
    tools: dict[str, str] = {}

    try:
        from ..acp.providers import get_providers
    except ImportError:
        logger.debug("Cannot import ACP providers module")
        return tools

    try:
        providers = get_providers()
    except Exception as e:
        logger.debug("get_providers() failed: %s", e)
        return tools

    # Try to get localized descriptions from text module
    desc_map: dict[str, str] = {}
    try:
        from ..utils.text import get_acp_result_header_text
        headers = get_acp_result_header_text()
        for name in providers:
            key = f"tool_desc_{name}"
            if key in headers:
                desc_map[name] = headers[key]
    except Exception:
        pass

    for name in providers:
        description = (
            desc_map.get(name)
            or _FALLBACK_DESCRIPTIONS.get(name)
            or name
        )
        tools[name] = description

    return tools


def _discover_ttadk_tools() -> dict[str, str]:
    """Discover TTADK tools from static defaults (no subprocess)."""
    tools: dict[str, str] = {}

    try:
        from ..ttadk.manager import DEFAULT_TOOLS, TOOL_DESCRIPTIONS
    except ImportError:
        logger.debug("Cannot import TTADK manager")
        return tools

    # Only include TTADK-exclusive tools (not already in ACP)
    acp_names = {"coco", "claude", "aiden", "codex", "gemini", "traex"}

    for tool in DEFAULT_TOOLS:
        name = tool.name if hasattr(tool, "name") else str(tool)
        if name in acp_names:
            continue
        desc = TOOL_DESCRIPTIONS.get(name, tool.description if hasattr(tool, "description") else name)
        tools[name] = desc

    # Always include ttadk as a composite entry
    if "ttadk" not in tools:
        tools["ttadk"] = _FALLBACK_DESCRIPTIONS.get("ttadk", "TTADK CLI 桥接")

    return tools
