"""RenderAtom: smallest renderable unit for card pagination."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from src.card.render.budget import RenderBudget
from src.card.state.models import ContentBlock
from src.card.ui_text import UI_TEXT

logger = logging.getLogger(__name__)

# Single source of truth for all atom kinds.
# renderer.py validates that _ATOM_RENDERERS keys match this type at load time.
AtomKind = Literal[
    "text", "tool_panel", "reasoning", "plan",
    "criteria_panel", "phase_panel", "warning_banner", "progress_bar",
    "worktree_panel", "task_list", "phase_banner",
    "subagent_dispatch", "activity_digest", "review_role",
]

@dataclass
class RenderAtom:
    """Smallest renderable unit. Maps to one or more Feishu schema elements."""

    kind: AtomKind
    elements: list[dict] = field(default_factory=list)
    byte_size: int = 0  # Estimated JSON byte size
    node_count: int = 0  # Element node count
    splittable: bool = False  # Whether this atom can be split across pages
    block_id: str = ""
    content: str = ""  # Raw content (for split operations)


def estimate_atom_size(atom: RenderAtom) -> int:
    """Estimate the JSON byte size of a RenderAtom.

    If elements exist, use actual JSON serialization size.
    Otherwise, estimate from content length * 3 + overhead.
    """
    if atom.elements:
        return len(json.dumps(atom.elements).encode("utf-8"))
    # Estimate: content bytes + structural overhead
    overhead = 100  # JSON object structure overhead
    return len(atom.content.encode("utf-8")) * 3 + overhead


def flatten_to_atoms(
    blocks: tuple[ContentBlock, ...], budget: RenderBudget
) -> list[RenderAtom]:
    """Convert ContentBlocks into a flat list of RenderAtoms.

    Tool calls are grouped into compact activity_digest atoms (one-line summary)
    that interleave with reasoning/text atoms in the body section.
    Active (running) tools emit a compact tool_panel atom inline.

    Uses a registry dispatch pattern for simple block→atom mappings.
    """
    from src.card.render.tools import render_activity_digest_panel, render_active_tool_line

    atoms: list[RenderAtom] = []
    pending_tools: list[ContentBlock] = []
    handlers = _get_block_kind_handlers()

    def _flush_pending() -> None:
        """Flush accumulated completed/failed tools as a single activity_digest atom."""
        if not pending_tools:
            return
        digest_panel = render_activity_digest_panel(pending_tools)
        if digest_panel:
            atom = RenderAtom(
                kind="activity_digest",
                block_id=pending_tools[0].block_id,
                content=str(digest_panel["header"]["title"].get("content", "")),
                elements=[digest_panel],
                splittable=False,
                node_count=1,
            )
            atom.byte_size = estimate_atom_size(atom)
            atoms.append(atom)
        pending_tools.clear()

    i = 0
    n = len(blocks)

    while i < n:
        block = blocks[i]

        if block.kind == "tool_call":
            if block.status == "active":
                # Flush any pending completed tools first
                _flush_pending()
                # Active tool: emit compact one-line indicator in body
                active_text = render_active_tool_line(block)
                atom = RenderAtom(
                    kind="tool_panel",
                    block_id=block.block_id,
                    content=active_text,
                    splittable=False,
                    node_count=1,
                )
                atom.byte_size = estimate_atom_size(atom)
                atoms.append(atom)
            else:
                # Completed or failed: accumulate for digest
                pending_tools.append(block)
            i += 1
        else:
            # Non-tool block: flush pending tools, then dispatch normally
            _flush_pending()
            handler = handlers.get(block.kind)
            if handler is not None:
                atom = handler(block)
                atoms.append(atom)
            else:
                logger.warning("flatten_to_atoms: unknown block kind %r (block_id=%s), skipping", block.kind, block.block_id)
                # Produce a visible placeholder so users know content failed to render
                error_text = UI_TEXT["card_content_load_error"]
                placeholder = RenderAtom(
                    kind="warning_banner",
                    block_id=block.block_id,
                    content=error_text,
                    node_count=1,
                )
                placeholder.byte_size = estimate_atom_size(placeholder)
                atoms.append(placeholder)
            i += 1

    # Flush any remaining pending tools at end of blocks
    _flush_pending()

    return atoms


# --- Block-to-atom handler functions (registered via _get_block_kind_handlers) ---

def _block_to_text_atom(block: ContentBlock) -> RenderAtom:
    atom = RenderAtom(
        kind="text", block_id=block.block_id, content=block.content,
        splittable=True, node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


def _block_to_reasoning_atom(block: ContentBlock) -> RenderAtom:
    atom = RenderAtom(
        kind="reasoning", block_id=block.block_id, content=block.content,
        splittable=False, node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


def _block_to_plan_atom(block: ContentBlock) -> RenderAtom:
    atom = RenderAtom(
        kind="plan", block_id=block.block_id, content=block.content,
        splittable=False, node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


def _block_to_criteria_atom(block: ContentBlock) -> RenderAtom:
    atom = RenderAtom(
        kind="criteria_panel", block_id=block.block_id, content=block.content,
        splittable=False, node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


def _block_to_phase_atom(block: ContentBlock) -> RenderAtom:
    atom = RenderAtom(
        kind="phase_panel", block_id=block.block_id, content=block.content,
        splittable=False, node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


def _block_to_worktree_atom(block: ContentBlock) -> RenderAtom:
    atom = RenderAtom(
        kind="worktree_panel", block_id=block.block_id, content=block.content,
        splittable=False, node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


def _block_to_task_list_atom(block: ContentBlock) -> RenderAtom:
    # Build content from tasks for size estimation
    tasks = getattr(block, "tasks", ())
    content_lines = [t.get("name", "") for t in tasks] if tasks else []
    content = "\n".join(content_lines)
    atom = RenderAtom(
        kind="task_list", block_id=block.block_id, content=content,
        splittable=False, node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


def _block_to_review_role_atom(block: ContentBlock) -> RenderAtom:
    data = getattr(block, "data", None) or {}
    content_parts = [
        str(data.get("title") or ""),
        str(data.get("summary") or ""),
        "\n".join(str(item) for item in data.get("suggestions") or []),
    ]
    atom = RenderAtom(
        kind="review_role",
        block_id=block.block_id,
        content="\n".join(part for part in content_parts if part),
        splittable=False,
        node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


def _block_to_separator_atom(block: ContentBlock) -> RenderAtom:
    task_name = getattr(block, "task_name", "")
    is_first = getattr(block, "is_first_overflow", False)
    status_emoji = getattr(block, "status_emoji", "⏳")
    key = "orch_overflow_separator_first" if is_first else "orch_overflow_separator"
    content = UI_TEXT[key].format(task_name=task_name, status_emoji=status_emoji)
    atom = RenderAtom(
        kind="text", block_id=block.block_id, content=content,
        splittable=False, node_count=1,
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom


# Registry: maps block.kind → handler function.
# tool_call is handled separately due to lookahead grouping logic.
# Lazy-initialized on first use to avoid import-time coupling with block_registry.

_ATOM_HANDLER_DISPATCH: dict[str, Callable[[ContentBlock], RenderAtom]] = {
    "text": _block_to_text_atom,
    "reasoning": _block_to_reasoning_atom,
    "plan": _block_to_plan_atom,
    "criteria": _block_to_criteria_atom,
    "phase": _block_to_phase_atom,
    "task_list": _block_to_task_list_atom,
    "review_role": _block_to_review_role_atom,
    "separator": _block_to_separator_atom,
}

# Module-level lazy cache for block kind handlers (avoids @functools.cache semantics)
_block_kind_handlers: dict[str, Callable[[ContentBlock], RenderAtom]] | None = None


def _get_block_kind_handlers() -> dict[str, Callable[[ContentBlock], RenderAtom]]:
    """Build and cache the block-kind-to-handler mapping (lazy init).

    Deferred to first call to avoid models.py → block_registry.py → atoms.py
    import-time coupling that could lead to circular imports.
    """
    global _block_kind_handlers
    if _block_kind_handlers is None:
        from src.card.state.block_registry import BLOCK_KIND_TO_ATOM

        handlers = {
            **_ATOM_HANDLER_DISPATCH,
            **{kind: _block_to_worktree_atom
               for kind, atom in BLOCK_KIND_TO_ATOM.items()
               if atom == "worktree_panel"},
        }

        # Startup assertion: all registered block kinds must have a handler
        # (tool_call is excluded — it has dedicated lookahead grouping logic)
        missing = set(BLOCK_KIND_TO_ATOM.keys()) - set(handlers.keys()) - {"tool_call"}
        if missing:
            raise RuntimeError(
                f"BLOCK_KIND_TO_ATOM contains kinds with no handler registered: {missing}. "
                f"Add handlers in _ATOM_HANDLER_DISPATCH or worktree_panel merge."
            )

        _block_kind_handlers = handlers
    return _block_kind_handlers


def invalidate_atom_handlers() -> None:
    """Reset the cached handler mapping. Intended for testing/hot-reload scenarios."""
    global _block_kind_handlers
    _block_kind_handlers = None

