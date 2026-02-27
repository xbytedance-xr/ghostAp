"""Phase tracker — tracks ACP events within a single spec phase execution.

Collects tool calls, modified files, plan progress, and text output
for one phase of the spec engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..acp.models import ACPEvent, ACPEventType, PlanInfo, ToolCallInfo


@dataclass
class PhaseTracker:
    """Tracks ACP events within a single phase."""

    tool_calls: list[ToolCallInfo] = field(default_factory=list)
    modified_files: set[str] = field(default_factory=set)
    plan_progress: Optional[PlanInfo] = None
    _text_chunks: list[str] = field(default_factory=list)

    def process(self, event: ACPEvent) -> None:
        """Process an ACP event."""
        match event.event_type:
            case ACPEventType.TEXT_CHUNK:
                if event.text:
                    self._text_chunks.append(event.text)
            case ACPEventType.TOOL_CALL_START:
                if event.tool_call:
                    for loc in event.tool_call.locations:
                        self.modified_files.add(loc)
            case ACPEventType.TOOL_CALL_DONE:
                if event.tool_call:
                    self.tool_calls.append(event.tool_call)
                    for loc in event.tool_call.locations:
                        self.modified_files.add(loc)
            case ACPEventType.PLAN_UPDATE:
                if event.plan:
                    self.plan_progress = event.plan

    @property
    def text_buffer(self) -> str:
        return "".join(self._text_chunks)

    def reset(self) -> None:
        """Reset for a new phase."""
        self.tool_calls.clear()
        self.modified_files.clear()
        self.plan_progress = None
        self._text_chunks.clear()
