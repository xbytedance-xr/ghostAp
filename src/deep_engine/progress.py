"""Deep Engine progress tracking from ACP events.

Tracks plan entries, tool calls, and modified files from ACP events
to provide structured progress information for the Deep Engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..acp.models import PlanInfo, ToolCallInfo


@dataclass
class DeepProgress:
    """Tracks deep execution progress extracted from ACP events."""

    plan_entries: list[dict] = field(default_factory=list)  # [{content, status, priority}]
    tool_calls: list[ToolCallInfo] = field(default_factory=list)
    modified_files: set[str] = field(default_factory=set)
    _text_chunks: list[str] = field(default_factory=list)

    def update_plan(self, plan: PlanInfo) -> None:
        self.plan_entries = [
            {"content": e.content, "status": e.status, "priority": e.priority}
            for e in plan.entries
        ]

    def record_tool(self, tool: ToolCallInfo) -> None:
        self.tool_calls.append(tool)
        for loc in tool.locations:
            self.modified_files.add(loc)

    @property
    def text_buffer(self) -> str:
        return "".join(self._text_chunks)

    def append_text(self, text: str) -> None:
        self._text_chunks.append(text)

    @property
    def completed_steps(self) -> int:
        return sum(1 for e in self.plan_entries if e["status"] == "completed")

    @property
    def total_steps(self) -> int:
        return len(self.plan_entries)

    @property
    def progress_percent(self) -> float:
        if self.total_steps == 0:
            return 0.0
        return (self.completed_steps / self.total_steps) * 100

    @property
    def progress_bar(self) -> str:
        percent = self.progress_percent
        filled = int(percent / 10)
        empty = 10 - filled
        return f"[{'█' * filled}{'░' * empty}] {percent:.0f}%"

    def format_summary(self) -> str:
        """Return human-readable progress summary."""
        lines = []
        if self.plan_entries:
            lines.append(f"📋 计划进度: {self.completed_steps}/{self.total_steps}")
            lines.append(self.progress_bar)
        if self.modified_files:
            lines.append(f"📝 修改文件: {len(self.modified_files)} 个")
            for f in sorted(self.modified_files)[:10]:
                lines.append(f"  • `{f}`")
            if len(self.modified_files) > 10:
                lines.append(f"  • ... 等 {len(self.modified_files) - 10} 个文件")
        if self.tool_calls:
            lines.append(f"🔧 工具调用: {len(self.tool_calls)} 次")
        return "\n".join(lines)
