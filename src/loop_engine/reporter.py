"""Loop Engine 进度报告器 — 格式化进度信息供 Feishu 卡片展示。"""

from .models import (
    LoopProject,
    LoopProjectStatus,
    IterationRecord,
    IterationStatus,
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

    def format_iteration_start(self, iteration: int, max_iterations: int) -> str:
        return f"""🔄 **迭代 [{iteration}/{max_iterations}]**

🤖 **Agent 执行中...**

⏳ 正在执行..."""

    def format_iteration_done(self, iteration: int, record: IterationRecord) -> str:
        if record.status == IterationStatus.SUCCESS:
            summary = record.focus or "执行完成"
            output_preview = ""
            if record.output:
                preview = record.output[:200]
                output_preview = f"\n\n**输出预览:**\n```\n{preview}\n```"
            return f"""✅ **迭代完成 [{iteration}]**

🤖 **{summary}**
⏱️ 耗时: {record.duration:.1f}s{output_preview}"""
        else:
            error_text = record.error or "未知错误"
            return f"""❌ **迭代失败 [{iteration}]**

⏱️ 耗时: {record.duration:.1f}s

**错误信息:**
```
{error_text}
```"""

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

        progress_bar = self._make_progress_bar(
            tracker.satisfied_count, tracker.total_count
        )
        lines.append(f"\n{progress_bar}")

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
            lines.append(f"⏱️ 总耗时: {project.duration():.1f}s")

        # 标准详情
        lines.append("\n**验收标准:**")
        tracker = project.criteria_tracker
        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            marker = "✅" if satisfied else "🔲"
            lines.append(f"  {marker} {i + 1}. {criterion}")

        return "\n".join(lines)

    def format_error(self, error: str) -> str:
        return f"""❌ **Loop Agent 错误**

```
{error}
```

请检查错误信息后重试。"""

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
            lines.append(f"耗时: {project.duration():.1f}s")

        # 最近的迭代
        if project.iterations:
            lines.append("\n**最近迭代:**")
            for record in project.iterations[-5:]:
                status_emoji = (
                    "✅" if record.status == IterationStatus.SUCCESS else "❌"
                )
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

    def get_criteria_update_title(self) -> str:
        return "📋 验收标准更新"

    def get_guidance_injected_title(self) -> str:
        return "💬 引导信息已注入"

    def get_error_title(self) -> str:
        return "❌ Loop Agent 错误"

    def get_status_title(self) -> str:
        return "📊 Loop 状态"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_progress_bar(self, completed: int, total: int) -> str:
        if total == 0:
            return "[░░░░░░░░░░] 0%"

        percent = (completed / total) * 100
        filled = int(percent / 10)
        empty = 10 - filled

        return f"[{'█' * filled}{'░' * empty}] {percent:.0f}% ({completed}/{total})"

    def get_progress_info(self, project: LoopProject) -> dict:
        return {
            "progress_bar": self._make_progress_bar(
                project.satisfied_count, project.total_criteria
            ),
            "satisfied_count": project.satisfied_count,
            "total_criteria": project.total_criteria,
            "iteration_count": project.current_iteration,
            "status": project.status,
            "project_name": project.name,
            "project_id": project.project_id,
            "is_running": project.status == LoopProjectStatus.RUNNING,
            "is_paused": project.status == LoopProjectStatus.PAUSED,
        }
