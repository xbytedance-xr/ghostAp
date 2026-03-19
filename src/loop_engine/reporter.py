"""Loop Engine 进度报告器 — 格式化进度信息供 Feishu 卡片展示。"""

from ..utils.text import format_duration, make_progress_bar
from .models import (
    IterationRecord,
    IterationStatus,
    LoopProject,
    LoopProjectStatus,
    ReviewResult,
)


class LoopReporter:
    """格式化 Loop Engine 的进度消息，用于 Feishu 卡片展示。"""

    # ------------------------------------------------------------------
    # Content formatters
    # ------------------------------------------------------------------

    def format_analyzing_start(self, requirement: str) -> str:
        return f"""🔄 **Loop Agent 启动**

📝 正在分析产品诉求...
> {requirement}

⏳ 请稍候，正在提取验收标准..."""

    def format_analyzing_done(self, project: LoopProject) -> str:
        if not project.requirement:
            return "❌ 需求分析失败"

        req = project.requirement
        lines = [
            "✅ **需求分析完成**\n",
            f"📂 项目: {project.name}",
            f"🎯 目标: {req.goal}",
            f"📊 验收标准: {project.total_criteria} 条\n",
            "**验收标准:**",
        ]

        for i, criterion in enumerate(req.acceptance_criteria):
            lines.append(f"  🔲 {i + 1}. {criterion}")

        if req.constraints:
            lines.append("\n**约束条件:**")
            for c in req.constraints:
                lines.append(f"  - {c}")

        lines.append("\n🚀 准备开始迭代执行...")
        return "\n".join(lines)

    def format_iteration_start(self, iteration: int, max_iterations: int, criteria_status: str = "") -> str:
        parts = [f"🔄 **迭代 [{iteration}/{max_iterations}]**\n", "🤖 **Agent 执行中...**"]
        if criteria_status:
            parts.append(f"\n{criteria_status}")
        parts.append("\n⏳ 正在执行...")
        return "\n".join(parts)

    def format_iteration_done(self, iteration: int, record: IterationRecord) -> str:
        if record.status == IterationStatus.SUCCESS:
            summary = record.focus or "执行完成"
            output_section = ""
            if record.output:
                output_section = f"\n\n**输出:**\n```\n{record.output}\n```"
            return f"""✅ **迭代完成 [{iteration}]**

🤖 **{summary}**
⏱️ 耗时: {format_duration(record.duration)}{output_section}"""
        else:
            error_text = record.error or "未知错误"
            return f"""❌ **迭代失败 [{iteration}]**

⏱️ 耗时: {format_duration(record.duration)}

**错误信息:**
```
{error_text}
```"""

    def format_criteria_brief(self, project: LoopProject) -> str:
        """Brief criteria status for iteration cards."""
        tracker = project.criteria_tracker
        if not tracker.criteria:
            return ""
        lines = [f"📋 **验收标准 ({tracker.satisfied_count}/{tracker.total_count})**"]
        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            marker = "✅" if satisfied else "🔲"
            lines.append(f"  {marker} {criterion}")
        return "\n".join(lines)

    def format_criteria_update(self, project: LoopProject) -> str:
        lines = ["📋 **验收标准进度**\n"]
        tracker = project.criteria_tracker

        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            if satisfied:
                iter_num = tracker.satisfied_at_iteration.get(i, "?")
                lines.append(f"  ✅ {i + 1}. {criterion} (第{iter_num}轮)")
            else:
                lines.append(f"  🔲 {i + 1}. {criterion}")

        progress_bar = self._make_progress_bar(tracker.satisfied_count, tracker.total_count)
        lines.append(f"\n{progress_bar}")

        return "\n".join(lines)

    def format_review_result(self, review: ReviewResult) -> str:
        lines = [f"🔍 **多视角审查 [第{review.iteration}轮]**\n"]

        count = 0
        total_reviews = len(review.reviews)

        for pr in review.reviews:
            count += 1
            if pr.passed:
                lines.append(f"{pr.perspective.emoji} **{pr.perspective.display_name}**: ✅ PASS")
            else:
                status_text = pr.perspective.failure_label
                lines.append(f"{pr.perspective.emoji} **{pr.perspective.display_name}**: {status_text}")
                for s in pr.suggestions:
                    lines.append(f"  - {s}")

            if count < total_reviews:
                lines.append("\n---")
            else:
                lines.append("")

        total = review.total_suggestions
        if total > 0:
            lines.append(f"💡 **改进建议: {total} 条** → 将在下轮迭代中处理")
        else:
            lines.append("✅ **所有视角均通过，无改进建议**")

        return "\n".join(lines)

    def format_project_done(self, project: LoopProject) -> str:
        if project.status == LoopProjectStatus.COMPLETED:
            lines = [
                "🎉 **Loop 模式完成！**\n",
                f"📂 项目: {project.name}",
                f"🎯 目标: {project.requirement.goal if project.requirement else '未知'}",
                f"🔁 总迭代: {project.current_iteration} 轮",
                f"✅ 验收标准: {project.satisfied_count}/{project.total_criteria} 全部满足",
            ]
        elif project.status == LoopProjectStatus.ABORTED:
            lines = [
                "⚠️ **Loop 模式终止**\n",
                f"📂 项目: {project.name}",
                f"📝 原因: {project.error or '未知'}",
                f"🔁 总迭代: {project.current_iteration} 轮",
                f"📊 验收标准: {project.satisfied_count}/{project.total_criteria} 满足",
            ]
        else:
            lines = [
                "⏸️ **Loop 模式暂停**\n",
                f"📂 项目: {project.name}",
                f"📊 验收标准: {project.satisfied_count}/{project.total_criteria} 满足",
            ]

        if project.duration():
            lines.append(f"⏱️ 总耗时: {format_duration(project.duration())}")

        # 标准详情
        lines.append("\n**验收标准:**")
        tracker = project.criteria_tracker
        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            marker = "✅" if satisfied else "🔲"
            lines.append(f"  {marker} {i + 1}. {criterion}")

        return "\n".join(lines)

    def format_error(self, error: str) -> str:
        err = (error or "").strip() or "未知错误"
        is_timeout = "TimeoutError" in err or "操作耗时过长" in err or "timeout" in err.lower()
        internal = "Internal error" if "internal error" in err.lower() else ""

        summary_parts: list[str] = []
        if is_timeout:
            summary_parts.append("⏱️ 操作超时，请检查网络或稍后重试")
        if internal:
            summary_parts.append(internal)
            
        summary_line = ""
        if summary_parts:
            summary_line = "\n\n" + "\n".join([f"- {p}" for p in summary_parts]) + "\n"
            
        advice = "建议您稍后点击重试。" if is_timeout else "请检查错误信息后重试。"
        
        return f"""❌ **Loop Agent 错误**{summary_line}
```
{err}
```

{advice}"""

    def format_status(self, project: LoopProject) -> str:
        status_text = {
            LoopProjectStatus.IDLE: "⏳ 等待开始",
            LoopProjectStatus.ANALYZING: "🧠 正在分析需求",
            LoopProjectStatus.RUNNING: "🔄 迭代执行中",
            LoopProjectStatus.PAUSED: "⏸️ 已暂停",
            LoopProjectStatus.COMPLETED: "✅ 已完成",
            LoopProjectStatus.ABORTED: "⚠️ 已终止",
        }.get(project.status, "❓ 未知状态")

        lines = [
            f"📊 **{project.name}** Loop 状态\n",
            f"状态: {status_text}",
            f"迭代: {project.current_iteration} 轮",
            f"标准: {project.satisfied_count}/{project.total_criteria} 满足",
        ]

        if project.duration():
            lines.append(f"⏱️ 已执行: {format_duration(project.duration())}")

        # 最近的迭代
        if project.iterations:
            lines.append("\n**最近迭代:**")
            for record in project.iterations[-5:]:
                status_emoji = "✅" if record.status == IterationStatus.SUCCESS else "❌"
                focus_text = record.focus[:60] if record.focus else ""
                lines.append(f"  {status_emoji} #{record.iteration} {focus_text}")

        # 标准进度
        lines.append("\n**验收标准:**")
        tracker = project.criteria_tracker
        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            marker = "✅" if satisfied else "🔲"
            lines.append(f"  {marker} {criterion}")

        return "\n".join(lines)

    def format_guidance_injected(self, message: str) -> str:
        return f"""💬 **引导信息已注入**

> {message}

将在下一轮迭代中生效。"""

    def format_history_list(self, iterations: list[IterationRecord], page: int, page_size: int = 5) -> str:
        """Format a paginated list of iteration history."""
        # Sort by iteration descending (newest first)
        sorted_iterations = sorted(iterations, key=lambda x: x.iteration, reverse=True)

        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size

        page_items = sorted_iterations[start_idx:end_idx]
        total_items = len(sorted_iterations)
        total_pages = (total_items + page_size - 1) // page_size if total_items > 0 else 1

        if not page_items:
            return "📭 暂无历史记录"

        lines = []
        for record in page_items:
            status_emoji = "✅" if record.status == IterationStatus.SUCCESS else "❌"
            if record.status == IterationStatus.RUNNING:
                status_emoji = "🔄"

            focus = record.focus or "(无摘要)"
            if len(focus) > 25:
                focus = focus[:25] + "..."

            duration = format_duration(record.duration) if record.duration else "--"

            lines.append(f"#{record.iteration} {status_emoji} **{focus}** ({duration})")

        summary = f"第 {page}/{total_pages} 页 · 共 {total_items} 条记录"
        return summary + "\n\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # Title helpers (for card headers)
    # ------------------------------------------------------------------

    def get_analyzing_start_title(self) -> str:
        return "🔄 Loop Agent 启动"

    def get_analyzing_done_title(self) -> str:
        return "✅ 需求分析完成"

    def get_iteration_start_title(self, current: int, max_iterations: int) -> str:
        return f"🔄 迭代 [{current}/{max_iterations}]"

    def get_iteration_done_title(self, success: bool, current: int) -> str:
        if success:
            return f"✅ 迭代完成 [{current}]"
        return f"❌ 迭代失败 [{current}]"

    def get_project_done_title(self, project: LoopProject) -> str:
        if project.status == LoopProjectStatus.COMPLETED:
            return "🎉 Loop 模式完成！"
        elif project.status == LoopProjectStatus.ABORTED:
            return "⚠️ Loop 模式终止"
        return "⏸️ Loop 模式暂停"

    def get_review_title(self, iteration: int, all_passed: bool) -> str:
        if all_passed:
            return f"✅ 审查通过 [第{iteration}轮]"
        return f"🔍 多视角审查 [第{iteration}轮]"

    def get_criteria_update_title(self) -> str:
        return "📋 验收标准更新"

    def get_guidance_injected_title(self) -> str:
        return "💬 引导信息已注入"

    def get_error_title(self) -> str:
        return "❌ Loop Agent 错误"

    def get_status_title(self) -> str:
        return "📊 Loop 状态"

    # ------------------------------------------------------------------
    # Structured card sections (for build_deep_card new params)
    # ------------------------------------------------------------------

    def format_status_line(self, project: LoopProject) -> str:
        """One-line status for card metadata area."""
        status_map = {
            LoopProjectStatus.IDLE: "⏳ 等待开始",
            LoopProjectStatus.ANALYZING: "🧠 分析中",
            LoopProjectStatus.RUNNING: "🔄 迭代执行中",
            LoopProjectStatus.PAUSED: "⏸️ 已暂停",
            LoopProjectStatus.COMPLETED: "✅ 已完成",
            LoopProjectStatus.ABORTED: "⚠️ 已终止",
        }
        status_text = status_map.get(project.status, "❓ 未知")
        iter_info = f"迭代 {project.current_iteration}"
        criteria_info = f"标准 {project.satisfied_count}/{project.total_criteria}"
        return f"{status_text} · {iter_info} · {criteria_info}"

    def format_duration_line(self, project: LoopProject) -> str:
        """Duration line for card metadata area."""
        if not project.duration():
            return ""
        return f"⏱️ {format_duration(project.duration())}"

    def format_criteria_section(self, project: LoopProject) -> str:
        """Standalone criteria section for card layout."""
        return self.format_criteria_brief(project)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_progress_bar(completed: int, total: int) -> str:
        return make_progress_bar(completed, total)

    def get_progress_info(self, project: LoopProject) -> dict:
        return {
            "progress_bar": self._make_progress_bar(project.satisfied_count, project.total_criteria),
            "satisfied_count": project.satisfied_count,
            "total_criteria": project.total_criteria,
            "iteration_count": project.current_iteration,
            "status": project.status,
            "project_name": project.name,
            "project_id": project.project_id,
            "is_running": project.status == LoopProjectStatus.RUNNING,
            "is_paused": project.status == LoopProjectStatus.PAUSED,
        }
