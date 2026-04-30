"""Main card state reducer — dispatches events to sub-reducers."""

from __future__ import annotations

from dataclasses import replace

from .models import CardState, CardMetadata, HeaderState, FooterState
from ..events import CardEvent, CardEventType
from .reducers.text import reduce_text
from .reducers.tool import reduce_tool
from .reducers.reasoning import reduce_reasoning
from .reducers.plan import reduce_plan
from .reducers.lifecycle import reduce_lifecycle, _build_header
from .reducers.approval import reduce_approval


# Event type → sub-reducer routing
_TEXT_EVENTS = {CardEventType.TEXT_STARTED, CardEventType.TEXT_DELTA, CardEventType.TEXT_DONE}
_TOOL_EVENTS = {CardEventType.TOOL_STARTED, CardEventType.TOOL_DELTA, CardEventType.TOOL_DONE, CardEventType.TOOL_FAILED}
_REASONING_EVENTS = {CardEventType.REASONING_STARTED, CardEventType.REASONING_DELTA, CardEventType.REASONING_DONE}
_LIFECYCLE_EVENTS = {CardEventType.STARTED, CardEventType.COMPLETED, CardEventType.FAILED,
                     CardEventType.CANCELLED, CardEventType.PAUSED, CardEventType.RESUMED}
_APPROVAL_EVENTS = {CardEventType.APPROVAL_REQUESTED, CardEventType.APPROVAL_RESOLVED}


def reduce_card_state(state: CardState | None, event: CardEvent, metadata: CardMetadata | None = None) -> CardState:
    """
    Pure function: old state + event → new state. No side effects.
    
    If state is None, creates initial state (expects STARTED event or provides defaults).
    metadata is only used for initial state creation.
    """
    if state is None:
        # Initialize with metadata
        meta = metadata or CardMetadata()
        state = CardState(metadata=meta)

    # Route to sub-reducer
    if event.type in _TEXT_EVENTS:
        new_state = reduce_text(state, event)
    elif event.type in _TOOL_EVENTS:
        new_state = reduce_tool(state, event)
    elif event.type in _REASONING_EVENTS:
        new_state = reduce_reasoning(state, event)
    elif event.type == CardEventType.PLAN_UPDATED:
        new_state = reduce_plan(state, event)
    elif event.type in _LIFECYCLE_EVENTS:
        new_state = reduce_lifecycle(state, event)
    elif event.type in _APPROVAL_EVENTS:
        new_state = reduce_approval(state, event)
    elif event.type == CardEventType.TOOL_MODEL_CHANGED:
        new_meta = replace(state.metadata,
                           tool_name=event.payload.get("tool_name") or state.metadata.tool_name,
                           model_name=event.payload.get("model_name") or state.metadata.model_name)
        header = _build_header(new_meta, state.terminal)
        new_state = replace(state, metadata=new_meta, header=header)
    elif event.type == CardEventType.PROGRESS_UPDATED:
        current = event.payload.get("current", 0)
        total = event.payload.get("total", 0)
        label = event.payload.get("label", "")
        if total > 0:
            pct = int(current / total * 100)
            filled = pct // 10
            bar = "▰" * filled + "▱" * (10 - filled)
            progress_text = f"{bar} {pct}% · 步骤 {current}/{total}"
            if label:
                progress_text += f" · {label}"
        else:
            progress_text = None
        new_state = replace(state, footer=replace(state.footer, progress=progress_text))
    else:
        # Unknown event — return state unchanged
        new_state = state

    # Bump version if state changed
    if new_state is not state:
        new_state = replace(new_state, version=state.version + 1)

    return new_state
