"""Centralized backend resolution for agent types.

Eliminates scattered `startswith("ttadk_")` checks across engine and ACP layers.
"""

from __future__ import annotations

from typing import Literal


def resolve_backend_kind(agent_type: str) -> Literal["acp", "cli"]:
    """Determine transport backend for given agent type."""
    normalized = agent_type.lower().strip()
    if normalized == "claude" or normalized.startswith("ttadk_"):
        return "cli"
    return "acp"


def is_cli_backend(agent_type: str) -> bool:
    """Shorthand: does this agent type use CLI bridge?"""
    return resolve_backend_kind(agent_type) == "cli"


def is_ttadk_type(agent_type: str) -> bool:
    """Check if agent type is a TTADK variant (any ttadk_* prefix)."""
    return agent_type.lower().strip().startswith("ttadk_")


def resolve_cwd(agent_type: str, root_path: str) -> str:
    """Resolve working directory for agent, handling ttadk normalization."""
    if is_ttadk_type(agent_type):
        from src.utils.path import normalize_ttadk_cwd

        return normalize_ttadk_cwd(root_path) or root_path
    return root_path
