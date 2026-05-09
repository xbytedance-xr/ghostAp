"""SectionLayout: SSOT for card section ordering and pagination contract."""

from __future__ import annotations

from dataclasses import dataclass

from src.card.render.atoms import RenderAtom


@dataclass(frozen=True)
class SectionLayout:
    """Single source of truth for card section ordering and pagination.

    sticky_head: repeated on every page, never moved by pagination.
    status:      first page only; secondary status panels (progress, criteria).
    body:        primary content; subject to greedy pagination.
    appendix:    last page only; tool_history, references.
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
