"""Spec review role result reducer."""

from __future__ import annotations

from dataclasses import replace

from ...events import CardEvent
from ..models import CardState, ReviewRoleBlock


def reduce_review_result(state: CardState, event: CardEvent) -> CardState:
    """Replace one cycle's review role panels with the latest structured result."""
    cycle_num = int(event.payload.get("cycle_num") or 0)
    roles = event.payload.get("roles") or []
    if not isinstance(roles, list):
        roles = []

    blocks = tuple(
        block for block in state.blocks
        if not (
            block.kind == "review_role"
            and isinstance(getattr(block, "data", None), dict)
            and int((block.data or {}).get("cycle_num") or 0) == cycle_num
        )
    )

    review_blocks: list[ReviewRoleBlock] = []
    for index, role in enumerate(roles, start=1):
        if not isinstance(role, dict):
            continue
        role_id = str(role.get("role_id") or role.get("title") or index)
        safe_role_id = "".join(ch if ch.isalnum() else "_" for ch in role_id).strip("_") or str(index)
        data = dict(role)
        data["cycle_num"] = cycle_num
        data.setdefault("role_index", index)
        review_blocks.append(
            ReviewRoleBlock(
                block_id=f"review_{cycle_num}_{index}_{safe_role_id}",
                data=data,
            )
        )

    return replace(state, blocks=blocks + tuple(review_blocks))
