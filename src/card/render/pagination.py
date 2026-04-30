"""Pagination: split RenderAtoms into pages that fit within RenderBudget."""

from __future__ import annotations

from src.card.render.atoms import RenderAtom, estimate_atom_size
from src.card.render.budget import RenderBudget

# Approximate overhead for card config/header/footer skeleton
BASE_OVERHEAD = 500


def paginate_atoms(
    atoms: list[RenderAtom], budget: RenderBudget
) -> list[list[RenderAtom]]:
    """Greedy pagination.

    1. Try to fit atom into current page
    2. If over budget, try to split (paragraph → line → 1600 chars)
    3. If split fails, start new page
    4. Never discard content
    """
    if not atoms:
        return [[]]

    page_budget = budget.byte_budget - BASE_OVERHEAD
    pages: list[list[RenderAtom]] = [[]]
    current_bytes = 0
    current_nodes = 0

    for atom in atoms:
        atom_size = atom.byte_size if atom.byte_size > 0 else estimate_atom_size(atom)

        # Check if atom fits in current page
        if (
            current_bytes + atom_size <= page_budget
            and current_nodes + atom.node_count <= budget.node_budget
        ):
            pages[-1].append(atom)
            current_bytes += atom_size
            current_nodes += atom.node_count
        else:
            # Try to split the atom
            remaining_bytes = page_budget - current_bytes
            split_result = split_atom(atom, remaining_bytes)

            if split_result is not None and len(split_result) > 1:
                # First part goes to current page
                first_part = split_result[0]
                pages[-1].append(first_part)

                # Remaining parts go to new pages
                for part in split_result[1:]:
                    part_size = (
                        part.byte_size
                        if part.byte_size > 0
                        else estimate_atom_size(part)
                    )
                    if (
                        current_bytes + part_size > page_budget
                        or current_nodes + part.node_count > budget.node_budget
                    ):
                        pages.append([])
                        current_bytes = 0
                        current_nodes = 0
                    pages[-1].append(part)
                    current_bytes += part_size
                    current_nodes += part.node_count
            else:
                # Cannot split or not splittable → start new page
                # But if current page is empty, force the atom in to avoid infinite loop
                if pages[-1]:
                    pages.append([])
                    current_bytes = 0
                    current_nodes = 0
                pages[-1].append(atom)
                current_bytes += atom_size
                current_nodes += atom.node_count

    return pages


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
