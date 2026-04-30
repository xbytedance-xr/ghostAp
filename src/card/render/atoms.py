"""RenderAtom: smallest renderable unit for card pagination."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from src.card.render.budget import RenderBudget
from src.card.state.models import ContentBlock


@dataclass
class RenderAtom:
    """Smallest renderable unit. Maps to one or more Feishu schema elements."""

    kind: Literal["text", "tool_panel", "tool_history", "reasoning", "plan"]
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
    """
    atoms: list[RenderAtom] = []
    i = 0
    n = len(blocks)

    while i < n:
        block = blocks[i]

        if block.kind == "tool_call":
            # Collect consecutive completed tool_call blocks for potential folding
            if block.status == "completed":
                group_start = i
                while (
                    i < n
                    and blocks[i].kind == "tool_call"
                    and blocks[i].status == "completed"
                ):
                    i += 1
                group = blocks[group_start:i]

                if len(group) >= budget.tool_history_fold_threshold:
                    # Fold into a single tool_history atom
                    summary_lines = []
                    for b in group:
                        name = b.tool_name or "tool"
                        summary = b.tool_summary or "done"
                        summary_lines.append(f"✓ {name}: {summary}")
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
                else:
                    # Individual tool_panel atoms
                    for b in group:
                        atom = _tool_block_to_atom(b)
                        atoms.append(atom)
            else:
                # Active or failed tool_call → never folded
                atom = _tool_block_to_atom(block)
                atoms.append(atom)
                i += 1
        elif block.kind == "text":
            atom = RenderAtom(
                kind="text",
                block_id=block.block_id,
                content=block.content,
                splittable=True,
                node_count=1,
            )
            atom.byte_size = estimate_atom_size(atom)
            atoms.append(atom)
            i += 1
        elif block.kind == "reasoning":
            atom = RenderAtom(
                kind="reasoning",
                block_id=block.block_id,
                content=block.content,
                splittable=False,
                node_count=1,
            )
            atom.byte_size = estimate_atom_size(atom)
            atoms.append(atom)
            i += 1
        elif block.kind == "plan":
            atom = RenderAtom(
                kind="plan",
                block_id=block.block_id,
                content=block.content,
                splittable=False,
                node_count=1,
            )
            atom.byte_size = estimate_atom_size(atom)
            atoms.append(atom)
            i += 1
        else:
            i += 1

    return atoms


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
