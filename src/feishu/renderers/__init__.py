"""Renderer factory: lazy-loads concrete renderers by engine type."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.card.protocols import RendererProtocol
    from src.feishu.handlers.base import BaseHandler


def get_renderer(engine_type: str, handler: "BaseHandler") -> "RendererProtocol":
    """Create a renderer instance for the given engine type.

    Uses lazy imports to avoid circular dependencies between handler and renderer modules.
    """
    if engine_type == "deep":
        from .deep_renderer import DeepRenderer
        return DeepRenderer(handler)
    elif engine_type == "loop":
        from .loop_renderer import LoopRenderer
        return LoopRenderer(handler)
    elif engine_type == "spec":
        from .spec_renderer import SpecRenderer
        return SpecRenderer(handler)
    elif engine_type == "worktree":
        from .worktree_renderer import WorktreeRenderer
        return WorktreeRenderer(handler)
    else:
        raise ValueError(f"Unknown engine type: {engine_type!r}")
