"""Reducer for task image artifacts."""

from __future__ import annotations

from dataclasses import replace

from ...events import CardEvent, CardEventType
from ..models import CardState, ImageBlock


def reduce_image(state: CardState, event: CardEvent) -> CardState:
    if event.type not in {CardEventType.IMAGE_ADDED, CardEventType.IMAGE_FAILED}:
        return state

    image_id = str(event.payload.get("image_id") or "")
    if not image_id:
        return state
    block_id = f"image:{image_id}"
    image_key = str(event.payload.get("image_key") or "") or None
    alt = str(event.payload.get("alt") or "任务图片")[:120]
    block = ImageBlock(
        block_id=block_id,
        image_key=image_key,
        alt=alt,
        status="completed" if event.type is CardEventType.IMAGE_ADDED else "failed",
    )

    index = state.block_index.get(block_id)
    if index is None:
        return replace(state, blocks=(*state.blocks, block))
    blocks = list(state.blocks)
    blocks[index] = block
    return replace(state, blocks=tuple(blocks))
