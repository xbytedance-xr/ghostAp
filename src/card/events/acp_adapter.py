"""ACP event to CardEvent adaptation logic.

Extracted from CardEvent.from_acp() to maintain SRP — the CardEvent class
stays focused on being a pure data container with simple factory methods,
while this module handles the ACP protocol translation concern.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .factories import CardEvent
from .types import CardEventType

if TYPE_CHECKING:
    from src.acp.models import ACPEvent


def card_event_from_acp(acp_event: "ACPEvent") -> CardEvent:
    """Convert an ACPEvent to a CardEvent.

    Maps ACP event types to the card event pipeline:
    - TEXT_CHUNK → TEXT_DELTA
    - THOUGHT_CHUNK → REASONING_DELTA
    - TOOL_CALL_START → TOOL_STARTED
    - TOOL_CALL_UPDATE → TOOL_DELTA
    - TOOL_CALL_DONE → TOOL_DONE / TOOL_FAILED
    - PLAN_UPDATE → PLAN_UPDATED
    - (fallback) → TEXT_DELTA
    """
    from src.acp.models import ACPEventType as AET

    match acp_event.event_type:
        case AET.TEXT_CHUNK:
            return CardEvent(type=CardEventType.TEXT_DELTA, payload={
                "block_id": "_active_text",
                "text": acp_event.text or "",
            })
        case AET.THOUGHT_CHUNK:
            return CardEvent(type=CardEventType.REASONING_DELTA, payload={
                "block_id": "_active_reasoning",
                "text": acp_event.text or "",
            })
        case AET.TOOL_CALL_START:
            tc = acp_event.tool_call
            return CardEvent(type=CardEventType.TOOL_STARTED, payload={
                "block_id": tc.id if tc else "",
                "tool_name": tc.title if tc else "",
                "tool_input": tc.content if tc else "",
            })
        case AET.TOOL_CALL_UPDATE:
            tc = acp_event.tool_call
            return CardEvent(type=CardEventType.TOOL_DELTA, payload={
                "block_id": tc.id if tc else "",
                "content": tc.content if tc else "",
            })
        case AET.TOOL_CALL_DONE:
            tc = acp_event.tool_call
            summary = tc.title if tc else ""
            output = tc.content if tc else ""
            status = tc.status if tc else "completed"
            if status == "failed":
                return CardEvent(type=CardEventType.TOOL_FAILED, payload={
                    "block_id": tc.id if tc else "",
                    "error": output,
                })
            return CardEvent(type=CardEventType.TOOL_DONE, payload={
                "block_id": tc.id if tc else "",
                "tool_output": output,
                "tool_summary": summary,
            })
        case AET.PLAN_UPDATE:
            plan = acp_event.plan
            if plan:
                lines = []
                for entry in plan.entries:
                    icon = {"completed": "✅", "in_progress": "⏳", "pending": "○"}.get(entry.status, "○")
                    lines.append(f"{icon} {entry.content}")
                content = "\n".join(lines)
            else:
                content = ""
            return CardEvent(type=CardEventType.PLAN_UPDATED, payload={"content": content})
        case _:
            return CardEvent(type=CardEventType.TEXT_DELTA, payload={
                "block_id": "_active_text",
                "text": acp_event.text or "",
            })
