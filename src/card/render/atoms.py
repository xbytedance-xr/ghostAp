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
    "text", "tool_panel", "tool_history", "reasoning", "plan",
    "criteria_panel", "phase_panel", "warning_banner", "progress_bar",
    "worktree_panel", "task_list", "activity_summary", "phase_banner",
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

    Implements tool history folding: ≥threshold completed tools → folded into one atom.
    Uses a registry dispatch pattern for simple block→atom mappings.
    """
    atoms: list[RenderAtom] = []
    i = 0
    n = len(blocks)
    handlers = _get_block_kind_handlers()

    while i < n:
        block = blocks[i]

        if block.kind == "tool_call":
            # Tool calls require lookahead grouping — handled explicitly
            if block.status == "completed":
                group_start = i
                # Scan forward: group completed tools even when interleaved with
                # reasoning/text blocks (which get their own atoms separately).
                # This prevents reasoning from breaking tool grouping and ensures
                # the fold threshold is evaluated on the aggregate count.
                interleaved: list[ContentBlock] = []
                while i < n:
                    if blocks[i].kind == "tool_call" and blocks[i].status == "completed":
                        i += 1
                    elif blocks[i].kind in ("reasoning", "text"):
                        interleaved.append(blocks[i])
                        i += 1
                    else:
                        break
                group = [b for b in blocks[group_start:i] if b.kind == "tool_call" and b.status == "completed"]

                if len(group) >= budget.tool_history_fold_threshold:
                    # Fold into a single tool_history atom
                    summary_lines = []
                    for b in group:
                        name = b.tool_name or "tool"
                        summary = b.tool_summary or "done"
                        summary_lines.append(f"✅ {name}: {summary}")
                    content = "\n".join(summary_lines)
                    atom = RenderAtom(
                        kind="tool_history",
                        block_id=group[0].block_id,
                        content=content,
                        splittable=False,
                        node_count=1,
                    )
                    atom.byte_size = estimate_atom_size(atom)
                    atoms.append(atom)
                    # Emit interleaved text/reasoning blocks that were absorbed
                    # during lookahead — they must not be silently discarded.
                    for ib in interleaved:
                        handler = handlers.get(ib.kind)
                        if handler is not None:
                            atoms.append(handler(ib))
                else:
                    # Below threshold: render each tool individually, with
                    # interleaved blocks in their original positions
                    for b in blocks[group_start:i]:
                        if b.kind == "tool_call":
                            atom = _tool_block_to_atom(b)
                            atoms.append(atom)
                        else:
                            handler = handlers.get(b.kind)
                            if handler is not None:
                                atoms.append(handler(b))
            else:
                # Active or failed tool_call → never folded
                atom = _tool_block_to_atom(block)
                atoms.append(atom)
                i += 1
        else:
            # Registry dispatch for all other block kinds
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


def _tool_block_to_atom(block: ContentBlock) -> RenderAtom:
    """Convert a single tool_call ContentBlock to a tool_panel RenderAtom."""
    content_parts = []
    if block.tool_name:
        content_parts.append(f"tool: {block.tool_name}")
    if block.tool_summary:
        content_parts.append(f"summary: {block.tool_summary}")
    content = "\n".join(content_parts) if content_parts else block.content

    atom = RenderAtom(
        kind="tool_panel",
        block_id=block.block_id,
        content=content,
        splittable=False,
        node_count=2,  # tool panel typically has header + body
    )
    atom.byte_size = estimate_atom_size(atom)
    return atom
