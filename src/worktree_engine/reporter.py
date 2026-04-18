from __future__ import annotations

from .models import WorktreeRuntimeState, WorktreeUnit


class WorktreeReporter:
    def refresh_state(self, state: WorktreeRuntimeState) -> WorktreeRuntimeState:
        state.summary_lines = self.build_unit_summary_lines(state.units)
        state.merge_notes = self.build_merge_notes(state.units, state.base_branch)
        state.merge_entry_ready = bool(state.units) and all(unit.status == "completed" for unit in state.units)
        if state.last_error:
            state.summary_lines.insert(0, f"- 总体错误：{state.last_error}")
        return state

    @staticmethod
    def build_unit_summary_lines(units: list[WorktreeUnit]) -> list[str]:
        lines: list[str] = []
        for unit in units:
            status = unit.status or "pending"
            change_text = "有代码变更" if unit.has_changes else "无代码变更"
            task_text = unit.task_title or "未分配任务"
            summary = unit.summary or unit.error or "暂无摘要"
            lines.append(f"- `{unit.display_name}` · `{status}` · {task_text} · {change_text} · {summary}")
        return lines

    @staticmethod
    def build_merge_notes(units: list[WorktreeUnit], base_branch: str) -> list[str]:
        notes: list[str] = []
        target = base_branch or "main"
        for unit in units:
            notes.append(
                f"- `{unit.display_name}` → 分支 `{unit.branch_name or '(未创建)'}` → worktree `{unit.worktree_path or '(未创建)'}` → 建议合并回 `{target}`"
            )
        return notes
