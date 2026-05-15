"""Phase lifecycle sub-reducer for Spec engine."""
from __future__ import annotations
import logging
from dataclasses import replace
from ..models import CardState, PhaseBlock, FooterState
from ...events import CardEvent, CardEventType
from ...ui_text import UI_TEXT

logger = logging.getLogger(__name__)

# Phase key → UI_TEXT key mapping
_PHASE_KEYS: tuple[str, ...] = (
    "planning", "coding", "review", "spec_review",
    "testing", "building", "deploying", "analyzing",
    "refactoring", "debugging",
)

def _get_phase_display(phase: str) -> str:
    key = f"phase_{phase}"
    return UI_TEXT.get(key, UI_TEXT["phase_default"])


def _is_same_cycle_phase_block(block, cycle_num: int) -> bool:
    return block.kind == "phase" and block.cycle_num == cycle_num


def reduce_phase(state: CardState, event: CardEvent) -> CardState:
    """Handle PHASE_STARTED / PHASE_DONE events."""
    if state.engine_ext is None:
        logger.warning("reduce_phase called with engine_ext=None, event=%s", event.type)
        return state
    match event.type:
        case CardEventType.PHASE_STARTED:
            cycle_num = event.payload.get("cycle_num", state.engine_ext.cycle_num)
            phase = event.payload.get("phase", "")
            subtitle = event.payload.get("subtitle")
            content = event.payload.get("content") or phase
            block_id = f"phase_{cycle_num}_{phase}"
            # Keep a single visible phase-progress panel per cycle. Completed
            # phase details stay in normal text blocks / cycle summaries; the
            # status panel should represent the current phase instead of
            # leaving stale snapshots at the top of the card.
            new_blocks = tuple(
                b for b in state.blocks
                if not _is_same_cycle_phase_block(b, cycle_num)
            )
            block = PhaseBlock(
                block_id=block_id,
                content=content,
                status="active",
                phase_name=phase,
                cycle_num=cycle_num,
            )
            display_name = _get_phase_display(phase)
            footer = replace(state.footer, status="tool_running",
                             status_text=f"⏳ {display_name}")
            ext = replace(state.engine_ext, phase_info=phase)
            changes: dict = {"blocks": new_blocks + (block,), "engine_ext": ext, "footer": footer}
            # Update header subtitle if provided (e.g. phase progress text)
            if subtitle is not None and state.header:
                changes["header"] = replace(state.header, subtitle=subtitle)
            return replace(state, **changes)

        case CardEventType.PHASE_DONE:
            cycle_num = event.payload.get("cycle_num", state.engine_ext.cycle_num)
            phase = event.payload.get("phase", "")
            output = event.payload.get("output", "")
            subtitle = event.payload.get("subtitle")
            block_id = f"phase_{cycle_num}_{phase}"
            # Guard: if no matching active block exists, warn and return unchanged
            has_block = any(b.block_id == block_id for b in state.blocks)
            if not has_block:
                logger.warning(
                    "PHASE_DONE received without prior PHASE_STARTED: "
                    "cycle=%s phase=%s", cycle_num, phase
                )
                return state
            # Mark the phase block as completed
            new_blocks = tuple(
                replace(b, status="completed", content=output or b.content)
                if b.block_id == block_id else b
                for b in state.blocks
            )
            footer = replace(state.footer, status="idle", status_text=None)
            ext = replace(state.engine_ext, phase_info=None)
            changes: dict = {"blocks": new_blocks, "engine_ext": ext, "footer": footer}
            if subtitle is not None and state.header:
                changes["header"] = replace(state.header, subtitle=subtitle)
            return replace(state, **changes)

    return state
