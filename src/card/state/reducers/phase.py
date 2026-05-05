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


def reduce_phase(state: CardState, event: CardEvent) -> CardState:
    """Handle PHASE_STARTED / PHASE_DONE events."""
    if state.engine_ext is None:
        logger.warning("reduce_phase called with engine_ext=None, event=%s", event.type)
        return state
    match event.type:
        case CardEventType.PHASE_STARTED:
            cycle_num = event.payload.get("cycle_num", state.engine_ext.cycle_num)
            phase = event.payload.get("phase", "")
            block_id = f"phase_{cycle_num}_{phase}"
            # Idempotency: if an active phase block with same id exists, replace it
            new_blocks = tuple(
                b for b in state.blocks
                if not (b.block_id == block_id and b.status == "active")
            )
            block = PhaseBlock(
                block_id=block_id,
                content=phase,
                status="active",
                phase_name=phase,
                cycle_num=cycle_num,
            )
            display_name = _get_phase_display(phase)
            footer = replace(state.footer, status="tool_running",
                             status_text=f"⏳ {display_name}")
            ext = replace(state.engine_ext, phase_info=phase)
            return replace(state, blocks=new_blocks + (block,),
                           engine_ext=ext, footer=footer)

        case CardEventType.PHASE_DONE:
            cycle_num = event.payload.get("cycle_num", state.engine_ext.cycle_num)
            phase = event.payload.get("phase", "")
            output = event.payload.get("output", "")
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
            return replace(state, blocks=new_blocks, engine_ext=ext, footer=footer)

    return state
