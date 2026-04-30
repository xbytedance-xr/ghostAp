"""Approval sub-reducer."""
from __future__ import annotations
from dataclasses import replace
from ..models import CardState, FooterState, ButtonSpec
from ...events import CardEvent, CardEventType
from .lifecycle import _build_header


def reduce_approval(state: CardState, event: CardEvent) -> CardState:
    """Handle APPROVAL_REQUESTED / APPROVAL_RESOLVED events."""
    if event.type == CardEventType.APPROVAL_REQUESTED:
        header = _build_header(state.metadata, "awaiting_approval")
        tool_name = event.payload.get("tool_name", "")
        description = event.payload.get("description", "")
        status_text = "⏳ 等待审批"
        if tool_name:
            status_text += f": {tool_name}"
        buttons = (
            ButtonSpec(text="✅ 批准", action_id="approve_action", type="primary"),
            ButtonSpec(text="❌ 拒绝", action_id="reject_action", type="danger"),
        )
        return replace(
            state,
            terminal="awaiting_approval",
            header=header,
            footer=replace(state.footer, status="waiting_approval", status_text=status_text),
            buttons=buttons,
        )

    elif event.type == CardEventType.APPROVAL_RESOLVED:
        approved = event.payload.get("approved", False)
        if approved:
            # Resume running state
            header = _build_header(state.metadata, "running")
            return replace(
                state,
                terminal="running",
                header=header,
                footer=FooterState(status="thinking", status_text="💭 正在思考..."),
                buttons=(),
            )
        else:
            # Approval rejected → cancelled
            header = _build_header(state.metadata, "cancelled")
            return replace(
                state,
                terminal="cancelled",
                header=header,
                footer=FooterState(),
                buttons=(),
            )

    return state
