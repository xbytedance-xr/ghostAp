"""Card event types — unified event abstraction for card state management."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.acp.models import ACPEvent


class CardEventType(str, Enum):
    """All card event types."""
    # Lifecycle
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"
    RESUMED = "resumed"
    # Content
    TEXT_STARTED = "text_started"
    TEXT_DELTA = "text_delta"
    TEXT_DONE = "text_done"
    REASONING_STARTED = "reasoning_started"
    REASONING_DELTA = "reasoning_delta"
    REASONING_DONE = "reasoning_done"
    TOOL_STARTED = "tool_started"
    TOOL_DELTA = "tool_delta"
    TOOL_DONE = "tool_done"
    TOOL_FAILED = "tool_failed"
    PLAN_UPDATED = "plan_updated"
    # Meta
    TOOL_MODEL_CHANGED = "tool_model_changed"
    PROGRESS_UPDATED = "progress_updated"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"


@dataclass(frozen=True)
class CardEvent:
    """Immutable card event dispatched to reducer."""
    type: CardEventType
    payload: dict = field(default_factory=dict)

    # --- Factory methods ---
    @classmethod
    def started(cls) -> "CardEvent":
        return cls(type=CardEventType.STARTED)

    @classmethod
    def completed(cls) -> "CardEvent":
        return cls(type=CardEventType.COMPLETED)

    @classmethod
    def failed(cls, error: str = "") -> "CardEvent":
        return cls(type=CardEventType.FAILED, payload={"error": error})

    @classmethod
    def cancelled(cls) -> "CardEvent":
        return cls(type=CardEventType.CANCELLED)

    @classmethod
    def text_started(cls, block_id: str) -> "CardEvent":
        return cls(type=CardEventType.TEXT_STARTED, payload={"block_id": block_id})

    @classmethod
    def text_delta(cls, block_id: str, text: str) -> "CardEvent":
        return cls(type=CardEventType.TEXT_DELTA, payload={"block_id": block_id, "text": text})

    @classmethod
    def text_done(cls, block_id: str) -> "CardEvent":
        return cls(type=CardEventType.TEXT_DONE, payload={"block_id": block_id})

    @classmethod
    def reasoning_started(cls, block_id: str) -> "CardEvent":
        return cls(type=CardEventType.REASONING_STARTED, payload={"block_id": block_id})

    @classmethod
    def reasoning_delta(cls, block_id: str, text: str) -> "CardEvent":
        return cls(type=CardEventType.REASONING_DELTA, payload={"block_id": block_id, "text": text})

    @classmethod
    def reasoning_done(cls, block_id: str) -> "CardEvent":
        return cls(type=CardEventType.REASONING_DONE, payload={"block_id": block_id})

    @classmethod
    def tool_started(cls, block_id: str, tool_name: str, tool_input: str = "") -> "CardEvent":
        return cls(type=CardEventType.TOOL_STARTED, payload={
            "block_id": block_id, "tool_name": tool_name, "tool_input": tool_input,
        })

    @classmethod
    def tool_delta(cls, block_id: str, content: str) -> "CardEvent":
        return cls(type=CardEventType.TOOL_DELTA, payload={"block_id": block_id, "content": content})

    @classmethod
    def tool_done(cls, block_id: str, tool_output: str = "", tool_summary: str = "") -> "CardEvent":
        return cls(type=CardEventType.TOOL_DONE, payload={
            "block_id": block_id, "tool_output": tool_output, "tool_summary": tool_summary,
        })

    @classmethod
    def tool_failed(cls, block_id: str, error: str = "") -> "CardEvent":
        return cls(type=CardEventType.TOOL_FAILED, payload={"block_id": block_id, "error": error})

    @classmethod
    def plan_updated(cls, content: str) -> "CardEvent":
        return cls(type=CardEventType.PLAN_UPDATED, payload={"content": content})

    @classmethod
    def tool_model_changed(cls, tool_name: str | None = None, model_name: str | None = None) -> "CardEvent":
        return cls(type=CardEventType.TOOL_MODEL_CHANGED, payload={
            "tool_name": tool_name, "model_name": model_name,
        })

    @classmethod
    def progress_updated(cls, current: int, total: int, label: str = "") -> "CardEvent":
        return cls(type=CardEventType.PROGRESS_UPDATED, payload={
            "current": current, "total": total, "label": label,
        })

    @classmethod
    def from_acp(cls, acp_event: "ACPEvent") -> "CardEvent":
        """Convert an ACPEvent to a CardEvent."""
        from src.acp.models import ACPEventType as AET

        match acp_event.event_type:
            case AET.TEXT_CHUNK:
                return cls(type=CardEventType.TEXT_DELTA, payload={
                    "block_id": "_active_text",
                    "text": acp_event.text or "",
                })
            case AET.THOUGHT_CHUNK:
                return cls(type=CardEventType.REASONING_DELTA, payload={
                    "block_id": "_active_reasoning",
                    "text": acp_event.text or "",
                })
            case AET.TOOL_CALL_START:
                tc = acp_event.tool_call
                return cls(type=CardEventType.TOOL_STARTED, payload={
                    "block_id": tc.id if tc else "",
                    "tool_name": tc.title if tc else "",
                    "tool_input": tc.content if tc else "",
                })
            case AET.TOOL_CALL_UPDATE:
                tc = acp_event.tool_call
                return cls(type=CardEventType.TOOL_DELTA, payload={
                    "block_id": tc.id if tc else "",
                    "content": tc.content if tc else "",
                })
            case AET.TOOL_CALL_DONE:
                tc = acp_event.tool_call
                summary = tc.title if tc else ""
                output = tc.content if tc else ""
                status = tc.status if tc else "completed"
                if status == "failed":
                    return cls(type=CardEventType.TOOL_FAILED, payload={
                        "block_id": tc.id if tc else "",
                        "error": output,
                    })
                return cls(type=CardEventType.TOOL_DONE, payload={
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
                return cls(type=CardEventType.PLAN_UPDATED, payload={"content": content})
            case _:
                return cls(type=CardEventType.TEXT_DELTA, payload={
                    "block_id": "_active_text",
                    "text": acp_event.text or "",
                })
