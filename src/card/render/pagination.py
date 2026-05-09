"""Pagination: split RenderAtoms into pages that fit within RenderBudget."""

from __future__ import annotations

import warnings

from src.card.render.atoms import RenderAtom, estimate_atom_size
from src.card.render.budget import RenderBudget

# Approximate overhead for card config/header/footer skeleton
BASE_OVERHEAD = 500

# Fixed node overhead for elements injected after pagination:
# header/config(3) + banner(3) + footer(8) + buttons(6) = 20
FIXED_NODE_OVERHEAD = 20


def paginate_atoms(
    atoms: list[RenderAtom], budget: RenderBudget
) -> list[list[RenderAtom]]:
    """[Deprecated] Use paginate_layout(SectionLayout(...), budget) instead.

    Retained as a thin shim that wraps body atoms into a SectionLayout with
    no sticky/status/appendix. Behavior stays identical for callers that do
    not care about sticky_head.
    """
    warnings.warn(
        "paginate_atoms is deprecated; use paginate_layout instead",
        DeprecationWarning,
        stacklevel=2,
    )
    from src.card.render.layout import SectionLayout, paginate_layout

    layout = SectionLayout(
        sticky_head=(),
        status=(),
        body=tuple(atoms),
        appendix=(),
    )
    pages = paginate_layout(layout, budget)
    return [list(page) for page in pages]


def split_atom(atom: RenderAtom, remaining_bytes: int) -> list[RenderAtom] | None:
    """Try to split a splittable atom.

    Split strategies (in order):
    1. By paragraph (double newline)
    2. By line (single newline)
    3. By 1600 character chunks

    Returns None if atom is not splittable.
    """
    if not atom.splittable:
        return None

    content = atom.content
    if not content:
        return None

    # Strategy 1: Split by paragraph (double newline)
    parts = _try_split_by_separator(atom, content, "\n\n", remaining_bytes)
    if parts is not None:
        return parts

    # Strategy 2: Split by line (single newline)
    parts = _try_split_by_separator(atom, content, "\n", remaining_bytes)
    if parts is not None:
        return parts

    # Strategy 3: Split by 1600 character chunks
    parts = _try_split_by_chars(atom, content, 1600, remaining_bytes)
    if parts is not None:
        return parts

    return None


def _try_split_by_separator(
    atom: RenderAtom, content: str, separator: str, remaining_bytes: int
) -> list[RenderAtom] | None:
    """Try to split content by separator, fitting first part within remaining_bytes."""
    segments = content.split(separator)
    if len(segments) < 2:
        return None

    # Find how many segments fit in remaining_bytes
    first_part_segments: list[str] = []
    for seg in segments:
        candidate = separator.join(first_part_segments + [seg])
        candidate_size = _estimate_content_bytes(candidate)
        if candidate_size > remaining_bytes and first_part_segments:
            break
        first_part_segments.append(seg)

    if not first_part_segments or len(first_part_segments) == len(segments):
        # Either nothing fits or everything fits — split not useful
        if not first_part_segments:
            return None
        return None

    first_content = separator.join(first_part_segments)
    rest_content = separator + separator.join(segments[len(first_part_segments):])

    return _make_split_atoms(atom, first_content, rest_content)


def _try_split_by_chars(
    atom: RenderAtom, content: str, chunk_size: int, remaining_bytes: int
) -> list[RenderAtom] | None:
    """Split content into character chunks."""
    if len(content) <= chunk_size:
        return None

    # Determine how many chars fit in remaining_bytes
    # Use a conservative estimate: each char ~3 bytes in JSON
    chars_for_remaining = max(remaining_bytes // 3, chunk_size)
    split_point = min(chars_for_remaining, len(content) - 1)

    if split_point <= 0:
        split_point = chunk_size

    # Ensure we don't exceed content length
    split_point = min(split_point, len(content) - 1)

    first_content = content[:split_point]
    rest_content = content[split_point:]

    if not first_content or not rest_content:
        return None

    return _make_split_atoms(atom, first_content, rest_content)


def _make_split_atoms(
    atom: RenderAtom, first_content: str, rest_content: str
) -> list[RenderAtom]:
    """Create split atom parts from content pieces."""
    first_atom = RenderAtom(
        kind=atom.kind,
        block_id=atom.block_id,
        content=first_content,
        splittable=True,
        node_count=atom.node_count,
    )
    first_atom.byte_size = estimate_atom_size(first_atom)

    rest_atom = RenderAtom(
        kind=atom.kind,
        block_id=atom.block_id,
        content=rest_content,
        splittable=True,
        node_count=atom.node_count,
    )
    rest_atom.byte_size = estimate_atom_size(rest_atom)

    return [first_atom, rest_atom]


def _estimate_content_bytes(content: str) -> int:
    """Estimate JSON byte size for content."""
    overhead = 100
    return len(content.encode("utf-8")) * 3 + overhead
