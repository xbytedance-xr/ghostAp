"""ACP event models — unified event abstraction over ACP session updates.

Converts raw ACP schema types (ToolCallStart, AgentMessageChunk, AgentPlanUpdate, etc.)
into simpler GhostAP-internal event objects for rendering and tracking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ACPEventType(Enum):
    """Event types produced from ACP session_update notifications."""
    TEXT_CHUNK = "text_chunk"
    THOUGHT_CHUNK = "thought_chunk"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_UPDATE = "tool_call_update"
    TOOL_CALL_DONE = "tool_call_done"
    PLAN_UPDATE = "plan_update"


@dataclass
class ToolCallInfo:
    """Simplified tool call representation."""
    id: str
    title: str
    kind: str  # read/edit/delete/execute/think/search/fetch/other
    status: str  # pending/in_progress/completed/failed
    content: str = ""
    locations: list[str] = field(default_factory=list)
    # Optional structured result (best-effort, may be populated from local history)
    result: Optional[dict] = None


@dataclass
class PlanEntryInfo:
    """Simplified plan entry."""
    content: str
    priority: str = "medium"  # high/medium/low
    status: str = "pending"  # pending/in_progress/completed


@dataclass
class PlanInfo:
    """Simplified plan."""
    entries: list[PlanEntryInfo] = field(default_factory=list)


@dataclass
class ACPEvent:
    """Unified ACP event, parsed from session_update notifications."""
    event_type: ACPEventType
    text: Optional[str] = None
    tool_call: Optional[ToolCallInfo] = None
    plan: Optional[PlanInfo] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ACPSessionState:
    """ACP session state (serializable for persistence)."""
    session_id: str
    agent_type: str  # "coco" / "claude"
    cwd: str
    created_at: float = field(default_factory=time.time)
    message_count: int = 0
    is_active: bool = True
    last_active: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent_type": self.agent_type,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "message_count": self.message_count,
            "is_active": self.is_active,
            "last_active": self.last_active,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ACPSessionState:
        return cls(
            session_id=data["session_id"],
            agent_type=data["agent_type"],
            cwd=data["cwd"],
            created_at=data.get("created_at", time.time()),
            message_count=data.get("message_count", 0),
            is_active=data.get("is_active", True),
            last_active=data.get("last_active", time.time()),
        )


@dataclass
class PromptResult:
    """Result of a prompt sent via ACP."""
    stop_reason: str  # end_turn/max_tokens/max_turn_requests/refusal/cancelled
    text: str = ""
    tool_calls: list[ToolCallInfo] = field(default_factory=list)
    # Structured tool results (best-effort). Each item is a dict with at least: kind/data/ts.
    tool_results: list[dict] = field(default_factory=list)
    plan: Optional[PlanInfo] = None
    modified_files: set[str] = field(default_factory=set)

    # ---- aggregation helpers ----
    def add_text(self, chunk: str) -> None:
        if chunk:
            self.text += chunk

    def add_tool_call(self, tool_call: ToolCallInfo) -> None:
        if not tool_call:
            return
        self.tool_calls.append(tool_call)
        for p in (tool_call.locations or []):
            if p:
                self.modified_files.add(p)

    def add_modified_file(self, path: str) -> None:
        if path:
            self.modified_files.add(path)

    def set_plan(self, plan: Optional[PlanInfo]) -> None:
        self.plan = plan

    def ingest_history(self, entries: list[dict]) -> None:
        """Ingest local ACP history entries.

        Expected entry format (from ACPHistoryStore):
            {"kind": "execute"|"read_file"|"write_file"|..., "data": {...}, "ts": ...}

        This method is tolerant to missing keys and malformed items.
        """
        if not entries:
            return
        for e in entries:
            if not isinstance(e, dict):
                continue
            kind = e.get("kind")
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            if kind:
                self.tool_results.append(e)
            # Track modified files from tool results
            if kind in ("write_file", "read_file"):
                p = data.get("path")
                if p:
                    self.modified_files.add(p)

    # ---- presentation ----
    def to_markdown(self, max_text_chars: int = 8000, max_items: int = 50) -> str:
        """Render a compact Markdown summary for Feishu/console display."""

        parts: list[str] = []

        # Header
        parts.append(f"**✅ PromptResult** · stop_reason=`{self.stop_reason}`")

        # Text
        if self.text:
            text = self.text
            if max_text_chars > 0 and len(text) > max_text_chars:
                text = text[:max_text_chars] + "\n... (truncated)"
            parts.append("\n**📝 输出文本**")
            parts.append(text)

        # Plan
        if self.plan and self.plan.entries:
            parts.append("\n**📋 计划**")
            for ent in self.plan.entries[:max_items]:
                icon = {"completed": "✅", "in_progress": "🔄", "pending": "⏳"}.get(ent.status, "⬜")
                parts.append(f"- {icon} {ent.content}")

        # Tool calls
        if self.tool_calls:
            parts.append("\n**🛠️ 工具调用**")
            for tc in self.tool_calls[:max_items]:
                loc = f" `{tc.locations[0]}`" if tc.locations else ""
                parts.append(f"- `{tc.kind}` {tc.title}{loc} · `{tc.status}`")

        # Tool results
        if self.tool_results:
            parts.append("\n**📦 工具结果(本地记录)**")
            for e in self.tool_results[:max_items]:
                kind = e.get("kind", "unknown")
                data = e.get("data") if isinstance(e.get("data"), dict) else {}
                if kind == "execute":
                    cmd = data.get("command", "")
                    code = data.get("exit_code")
                    parts.append(f"- `execute` `{cmd}` · exit_code={code}")
                elif kind in ("read_file", "write_file"):
                    p = data.get("path", "")
                    parts.append(f"- `{kind}` `{p}`")
                elif kind == "permission":
                    parts.append(f"- `permission` {data.get('outcome', '')} · {data.get('reason', '')}")
                else:
                    parts.append(f"- `{kind}`")

        # Modified files
        if self.modified_files:
            parts.append("\n**🗂️ 改动文件**")
            for p in list(sorted(self.modified_files))[:max_items]:
                parts.append(f"- `{p}`")

        return "\n".join(parts).strip() + "\n"
