from __future__ import annotations

import logging

from .models import WorktreeInfo, WorktreeRuntimeState, WorktreeUnit, WorktreeUnitStatus

from ..card.styles import STATUS_DISPLAY_MAP

logger = logging.getLogger(__name__)


class WorktreeReporter:
    def refresh_state(self, state: WorktreeRuntimeState) -> WorktreeRuntimeState:
        state.summary_lines = self.build_unit_summary_lines(state.units)
        state.merge_notes = self.build_merge_notes(state.units, state.base_branch)
        # Allow merge when at least one unit completed (partial success is mergeable)
        state.merge_entry_ready = bool(state.units) and any(
            unit.status == WorktreeUnitStatus.COMPLETED for unit in state.units
        )
        if state.last_error:
            state.summary_lines.insert(0, f"- 总体错误：{state.last_error}")
        return state

    @staticmethod
    def _get_unit_display_name(unit: WorktreeUnit) -> str:
        if unit.display_name:
            return unit.display_name

        if unit.unit_id:
            # 处理 wt-01 这种标准格式
            if unit.unit_id.startswith("wt-"):
                suffix = unit.unit_id[3:]
                try:
                    idx = int(suffix)
                    if 1 <= idx <= 26:
                        letter = chr(ord("A") + idx - 1)
                        return f"工作空间 {letter}"
                except ValueError:
                    logger.debug("failed to convert index to letter", exc_info=True)

            # 兼容其他带后缀的格式
            suffix = unit.unit_id.split("-")[-1] if "-" in unit.unit_id else unit.unit_id
            if suffix.isdigit():
                return f"单元 {suffix}"

        return "自动分配中"

    @staticmethod
    def build_unit_summary_lines(units: list[WorktreeUnit]) -> list[str]:
        lines: list[str] = []
        status_icon = {"completed": "✅", "failed": "❌", "running": "🔄", "planned": "📋", "ready": "⏳"}
        for unit in units:
            status = unit.status or WorktreeUnitStatus.PENDING
            icon = status_icon.get(status, "⏳")
            change_text = "有代码变更" if unit.has_changes else "无代码变更"
            task_text = unit.task_title or "未分配任务"

            display_name = WorktreeReporter._get_unit_display_name(unit)

            if status == WorktreeUnitStatus.FAILED:
                summary = f"🔍 失败原因：{unit.error or '未知执行异常'}"
            else:
                summary = unit.summary or "暂无摘要"
            lines.append(f"- {icon} `{display_name}` · `{STATUS_DISPLAY_MAP.get(status, status)}` · {task_text} · {change_text} · {summary}")
        return lines

    @staticmethod
    def build_merge_notes(units: list[WorktreeUnit], base_branch: str) -> list[str]:
        notes: list[str] = []
        target = base_branch or "main"
        for unit in units:
            display_name = WorktreeReporter._get_unit_display_name(unit)

            notes.append(
                f"- `{display_name}` → 分支 `{unit.branch_name or '(未创建)'}` → worktree `{unit.worktree_path or '(未创建)'}` → 建议合并回 `{target}`"
            )
        return notes

    @staticmethod
    def format_worktree_table(entries: list[WorktreeInfo]) -> str:
        """Format worktree list into an aligned table string."""
        if not entries:
            return "(无 worktree)"

        headers = ("路径", "分支", "状态", "最后更新")
        rows: list[tuple[str, str, str, str]] = []
        for entry in entries:
            active_mark = " *" if entry.is_active else ""
            rows.append((
                entry.path,
                entry.branch or "(detached)",
                f"活跃{active_mark}" if entry.is_active else "非活跃",
                entry.last_updated or "-",
            ))

        # Compute column widths
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                col_widths[i] = max(col_widths[i], len(cell))

        def _fmt_row(cells: tuple[str, ...]) -> str:
            return "  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(cells))

        lines = [_fmt_row(headers), "-" * (sum(col_widths) + 2 * (len(headers) - 1))]
        for row in rows:
            lines.append(_fmt_row(row))
        return "\n".join(lines)
