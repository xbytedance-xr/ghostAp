"""SectionLayout: SSOT for card section ordering and pagination contract."""

from __future__ import annotations

from dataclasses import dataclass

from src.card.render.atoms import RenderAtom, estimate_atom_size
from src.card.render.budget import RenderBudget
from src.card.render.pagination import BASE_OVERHEAD, FIXED_NODE_OVERHEAD, split_atom


@dataclass(frozen=True)
class SectionLayout:
    """Single source of truth for card section ordering and pagination.

    sticky_head: repeated on every page, never moved by pagination.
    status:      first page only; secondary status panels (progress, criteria).
    body:        primary content; subject to greedy pagination.
    appendix:    last page only; reserved for future use.
    """

    sticky_head: tuple[RenderAtom, ...]
    status: tuple[RenderAtom, ...]
    body: tuple[RenderAtom, ...]
    appendix: tuple[RenderAtom, ...]

    def assemble_for_page(
        self,
        page_idx: int,
        total_pages: int,
        body_slice: tuple[RenderAtom, ...],
    ) -> tuple[RenderAtom, ...]:
        """Build full atom sequence for one page."""
        result: list[RenderAtom] = list(self.sticky_head)
        if page_idx == 0:
            result.extend(self.status)
        result.extend(body_slice)
        if page_idx == total_pages - 1:
            result.extend(self.appendix)
        return tuple(result)


def paginate_layout(layout: SectionLayout, budget: RenderBudget) -> list[tuple[RenderAtom, ...]]:
    """Paginate body atoms with sticky_head reserved on every page."""
    sticky_size = sum(_atom_size(a) for a in layout.sticky_head)
    sticky_nodes = sum(a.node_count for a in layout.sticky_head)
    status_size = sum(_atom_size(a) for a in layout.status)
    status_nodes = sum(a.node_count for a in layout.status)

    base_byte = budget.byte_budget - BASE_OVERHEAD - sticky_size
    base_node = budget.node_budget - FIXED_NODE_OVERHEAD - sticky_nodes

    body_pages: list[list[RenderAtom]] = [[]]
    cur_bytes = 0
    cur_nodes = 0
    is_first_page = True

    def remaining_byte() -> int:
        extra = status_size if is_first_page else 0
        return base_byte - extra - cur_bytes

    def remaining_node() -> int:
        extra = status_nodes if is_first_page else 0
        return base_node - extra - cur_nodes

    for atom in layout.body:
        atom_size = _atom_size(atom)
        if atom_size <= remaining_byte() and atom.node_count <= remaining_node():
            body_pages[-1].append(atom)
            cur_bytes += atom_size
            cur_nodes += atom.node_count
            continue

        split_result = split_atom(atom, max(remaining_byte(), 0))
        if split_result is not None and len(split_result) > 1:
            first_part, *rest = split_result
            body_pages[-1].append(first_part)
            cur_bytes += _atom_size(first_part)
            cur_nodes += first_part.node_count
            for part in rest:
                body_pages.append([])
                is_first_page = False
                cur_bytes = 0
                cur_nodes = 0
                body_pages[-1].append(part)
                cur_bytes += _atom_size(part)
                cur_nodes += part.node_count
            continue

        if body_pages[-1]:
            body_pages.append([])
            is_first_page = False
            cur_bytes = 0
            cur_nodes = 0
        body_pages[-1].append(atom)
        cur_bytes += atom_size
        cur_nodes += atom.node_count

    if not body_pages or (len(body_pages) == 1 and not body_pages[0]):
        body_pages = [[]]

    total = len(body_pages)
    return [layout.assemble_for_page(idx, total, tuple(slice_)) for idx, slice_ in enumerate(body_pages)]


def _atom_size(atom: RenderAtom) -> int:
    return atom.byte_size if atom.byte_size > 0 else estimate_atom_size(atom)
