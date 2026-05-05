"""Block index helper for O(1) lookup by block_id."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import AnyContentBlock


def rebuild_block_index(blocks: tuple[AnyContentBlock, ...]) -> dict[str, int]:
    """Build a mapping from block_id → index position in the blocks tuple.

    Used by delta reducers (text, tool, reasoning) for O(1) lookup instead of
    O(n) linear scan. Returns the *last* index for a given block_id if duplicates
    exist (shouldn't happen in practice).
    """
    return {b.block_id: i for i, b in enumerate(blocks) if b.block_id}
