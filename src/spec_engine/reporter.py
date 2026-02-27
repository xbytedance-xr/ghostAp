"""Spec Engine 进度报告器 — 格式化进度信息供 Feishu 卡片展示。"""

from ..utils.text import format_duration, make_progress_bar
from .models import (
    SpecProject,
    SpecProjectStatus,
    SpecPhase,
    SpecCycle,
    ReviewResult,
    SpecWorkItemStatus,
)


class SpecReporter:
    """格式化 Spec Engine 的进度消息，用于 Feishu 卡片展示。"""

    # ------------------------------------------------------------------
    # Content formatters
    # ------------------------------------------------------------------

    def format_analyzing_start(self, requirement: str) -> str:
        return f"""📋 **Spec Agent 启动**

📝 正在分析项目需求...
> {requirement}

⏳ 请稍候，正在提取验收标准..."""

    def format_analyzing_done(self, project: SpecProject) -> str:
        if not project.acceptance_criteria:
            return "❌ 需求分析失败"

        lines = [
            "✅ **需求分析完成**\n",
            f"📂 项目: {project.name}",
            f"🎯 需求: {project.requirement[:100]}{'...' if len(project.requirement) > 100 else ''}",
            f"📊 验收标准: {project.total_criteria} 条\n",
            "**验收标准:**",
        ]

        for i, criterion in enumerate(project.acceptance_criteria):
            lines.append(f"  🔲 {i + 1}. {criterion}")

        lines.append("\n🚀 准备开始 Spec 循环...")
        return "\n".join(lines)

    def format_cycle_start(self, cycle_number: int, max_cycles: int) -> str:
        return f"🔄 **Spec 循环 [{cycle_number}/{max_cycles}]** 开始\n\n阶段: 📋 Spec → 🏗️ Plan → 📝 Task → 🔨 Build → 🔍 Review"

    def format_phase_start(self, cycle: int, phase: SpecPhase) -> str:
        return f"{phase.emoji} **{phase.display_name}** [循环 {cycle}]\n\n⏳ 执行中..."

    def format_phase_done(self, cycle: int, phase: SpecPhase, content: str) -> str:
        preview = content[:500] if content else "(无输出)"
        if len(content) > 500:
            preview += "\n..."
        return f"""{phase.emoji} **{phase.display_name}完成** [循环 {cycle}]

```
{preview}
```"""

    def format_review_result(self, review: ReviewResult, cycle: int) -> str:
        lines = [f"🔍 **多视角审查 [循环 {cycle}]**\n"]

        for pr in review.reviews:
            if pr.passed:
                lines.append(f"{pr.perspective.emoji} **{pr.perspective.display_name}**: ✅ PASS")
            else:
                lines.append(f"{pr.perspective.emoji} **{pr.perspective.display_name}**: ❌ 有建议")
                for s in pr.suggestions:
                    lines.append(f"  - {s}")
            lines.append("")

        total = review.total_suggestions
        if total > 0:
            lines.append(f"💡 **改进建议: {total} 条** → 将驱动下一轮 Spec 循环")
        else:
            lines.append("✅ **所有视角均通过，无改进建议**")

        return "\n".join(lines)

    def format_criteria_brief(self, project: SpecProject) -> str:
        tracker = project.criteria_tracker
        if not tracker.criteria:
            return ""
        lines = [f"📋 **验收标准 ({tracker.satisfied_count}/{tracker.total_count})**"]
        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            marker = "✅" if satisfied else "🔲"
            lines.append(f"  {marker} {criterion}")
        return "\n".join(lines)

    def format_criteria_update(self, project: SpecProject) -> str:
        lines = ["📋 **验收标准进度**\n"]
        tracker = project.criteria_tracker

        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            if satisfied:
                cycle_num = tracker.satisfied_at_iteration.get(i, "?")
                lines.append(f"  ✅ {i + 1}. {criterion} (循环{cycle_num})")
            else:
                lines.append(f"  🔲 {i + 1}. {criterion}")

        progress_bar = self._make_progress_bar(
            tracker.satisfied_count, tracker.total_count
        )
        lines.append(f"\n{progress_bar}")

        return "\n".join(lines)

    def format_project_done(self, project: SpecProject) -> str:
        if project.status == SpecProjectStatus.COMPLETED:
            lines = [
                "🎉 **Spec 模式完成！**\n",
                f"📂 项目: {project.name}",
                f"🎯 需求: {project.requirement[:80]}{'...' if len(project.requirement) > 80 else ''}",
                f"🔁 总循环: {project.current_cycle_number} 轮",
                f"✅ 验收标准: {project.satisfied_count}/{project.total_criteria} 全部满足",
            ]
        elif project.status == SpecProjectStatus.ABORTED:
            lines = [
                "⚠️ **Spec 模式终止**\n",
                f"📂 项目: {project.name}",
                f"📝 原因: {project.error or '未知'}",
                f"🔁 总循环: {project.current_cycle_number} 轮",
                f"📊 验收标准: {project.satisfied_count}/{project.total_criteria} 满足",
            ]

            # 固定首屏：未满足标准 + Top 建议
            tracker = project.criteria_tracker
            unsatisfied = tracker.unsatisfied_criteria
            if unsatisfied:
                lines.append("\n**未满足的验收标准（Top 8）:**")
                for c in unsatisfied[:8]:
                    lines.append(f"- [ ] {c}")

            last_cycle = project.current_cycle
            if last_cycle and last_cycle.review_result and not last_cycle.review_result.all_passed:
                pending: list[str] = []
                for pr in last_cycle.review_result.failed_perspectives:
                    for s in pr.suggestions:
                        if s:
                            pending.append(str(s))
                if pending:
                    lines.append("\n**待解决建议（Top 5）:**")
                    for s in pending[:5]:
                        lines.append(f"- {s}")

            # Actionable next steps
            tips: list[str] = []
            err = (project.error or "")
            if "收敛终止" in err:
                tips.extend([
                    "使用 `/spec_guide <补充信息>` 注入更多上下文/约束，帮助继续推进",
                    "补充缺失的需求澄清点（尤其是 Spec 里出现 NEEDS CLARIFICATION 的内容）",
                    "必要时提高 `spec_max_cycles` 或暂时关闭 `spec_review_enabled` 以定位阻塞",
                ])
            elif "最大循环" in err:
                tips.extend([
                    "使用 `/spec_guide` 提供更明确的目标与边界，减少反复试错",
                    "提高 `spec_max_cycles` 并再次运行，或拆分需求缩小范围",
                ])
            if tips:
                lines.append("\n**下一步建议：**")
                for t in tips:
                    lines.append(f"- {t}")
        elif project.status == SpecProjectStatus.CLARIFYING:
            # Legacy: CLARIFYING 状态不再由正常流程产生，保留用于向后兼容旧持久化状态
            lines = [
                "⏸️ **Spec 模式暂停**（旧版澄清状态）\n",
                f"📂 项目: {project.name}",
                f"📊 验收标准: {project.satisfied_count}/{project.total_criteria} 满足",
            ]
            lines.append("\n**继续方式：**")
            lines.append("- 发送 `/spec_resume` 继续执行")
        else:
            lines = [
                "⏸️ **Spec 模式暂停**\n",
                f"📂 项目: {project.name}",
                f"📊 验收标准: {project.satisfied_count}/{project.total_criteria} 满足",
            ]

        if project.duration():
            lines.append(f"⏱️ 总耗时: {format_duration(project.duration())}")

        # Full criteria list as appendix (for scanability)
        lines.append("\n**验收标准（完整列表）:**")
        tracker = project.criteria_tracker
        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            marker = "✅" if satisfied else "🔲"
            lines.append(f"  {marker} {i + 1}. {criterion}")

        return "\n".join(lines)

    def format_error(self, error: str) -> str:
        return f"""❌ **Spec Agent 错误**

```
{error}
```

请检查错误信息后重试。"""

    def format_status(self, project: SpecProject) -> str:
        status_text = {
            SpecProjectStatus.IDLE: "⏳ 等待开始",
            SpecProjectStatus.ANALYZING: "🧠 正在分析需求",
            SpecProjectStatus.RUNNING: "🔄 循环执行中",
            SpecProjectStatus.CLARIFYING: "❓ 等待澄清输入",
            SpecProjectStatus.PAUSED: "⏸️ 已暂停",
            SpecProjectStatus.COMPLETED: "✅ 已完成",
            SpecProjectStatus.ABORTED: "⚠️ 已终止",
        }.get(project.status, "❓ 未知状态")

        lines = [
            f"📊 **{project.name}** Spec 状态\n",
            f"状态: {status_text}",
            f"循环: {project.current_cycle_number} 轮",
            f"标准: {project.satisfied_count}/{project.total_criteria} 满足",
        ]

        if project.duration():
            lines.append(f"⏱️ 已执行: {format_duration(project.duration())}")

        # Current cycle phase
        if project.current_cycle:
            cycle = project.current_cycle
            lines.append(f"\n**当前循环 [{cycle.cycle_number}]:** {cycle.phase.emoji} {cycle.phase.display_name}")

            # Pending review suggestions (top N)
            if cycle.review_result and not cycle.review_result.all_passed:
                pending: list[str] = []
                for pr in cycle.review_result.failed_perspectives:
                    for s in pr.suggestions:
                        if s:
                            pending.append(str(s))
                if pending:
                    lines.append("\n**待解决建议（Top 5）:**")
                    for s in pending[:5]:
                        lines.append(f"  - {s}")

        # Recent cycles
        if project.cycles:
            lines.append("\n**循环历史:**")
            for cycle in project.cycles[-5:]:
                status_emoji = "✅" if cycle.status == "completed" else "❌" if cycle.status == "failed" else "🔄"
                review_info = ""
                if cycle.review_result:
                    if cycle.review_result.all_passed:
                        review_info = " (审查通过)"
                    else:
                        review_info = f" ({cycle.review_result.total_suggestions}条建议)"
                lines.append(f"  {status_emoji} 循环 {cycle.cycle_number}{review_info}")

        # Backlog (generated specs)
        if getattr(project, "work_items", None):
            pending = [w for w in project.work_items if w.status == SpecWorkItemStatus.PENDING]
            in_prog = [w for w in project.work_items if w.status == SpecWorkItemStatus.IN_PROGRESS]
            lines.append("\n**待优化单元（Spec Backlog）:**")
            lines.append(f"  待办: {len(pending)}  •  执行中: {len(in_prog)}  •  总计: {len(project.work_items)}")
            for w in (in_prog + pending)[:5]:
                q = (w.question or "").strip()
                q = q if len(q) <= 80 else q[:80] + "..."
                lines.append(f"  - {w.item_id}: {q}")

        # Criteria progress
        lines.append("\n**验收标准:**")
        tracker = project.criteria_tracker
        for i, criterion in enumerate(tracker.criteria):
            satisfied = tracker.satisfied.get(i, False)
            marker = "✅" if satisfied else "🔲"
            lines.append(f"  {marker} {criterion}")

        return "\n".join(lines)

    def format_history(self, project: SpecProject, tail: int = 20) -> str:
        tail = max(1, int(tail or 20))
        lines: list[str] = [f"🗂️ **Spec 历史** — {project.name}\n"]

        # Compaction hint
        if getattr(project, "cycle_count_total", 0) and project.cycle_count_total > len(project.cycles):
            truncated = project.cycle_count_total - len(project.cycles)
            lines.append(f"⚠️ 状态文件为压缩模式：仅保留最近 {len(project.cycles)} 轮明细（前 {truncated} 轮已截断）")
            hist = getattr(project, "history_log_path", None)
            if hist:
                lines.append(f"可查完整增量历史日志：`{hist}`\n")
            else:
                root = getattr(project, "artifacts_root", None)
                if root:
                    lines.append(f"可查完整增量历史日志：见目录 `{root}`（history log 路径未加载）\n")

        if not project.cycles:
            lines.append("(暂无循环记录)")
        else:
            lines.append("**循环（最近）:**")
            for c in project.cycles[-tail:]:
                review = ""
                if c.review_result:
                    review = "PASS" if c.review_result.all_passed else f"{c.review_result.total_suggestions}条建议"
                lines.append(
                    f"- 循环 {c.cycle_number}: {c.status}"
                    f"  • spec={bool(c.spec_path)} plan={bool(c.plan_path)} tasks={bool(c.tasks_path)} build={bool(c.build_path)} review={review or '跳过'}"
                )

        if getattr(project, "work_items", None):
            lines.append("\n**生成的 Spec 文件（最近）:**")
            for w in project.work_items[-tail:]:
                status = w.status.value
                q = (w.question or "").strip()
                q = q if len(q) <= 60 else q[:60] + "..."
                deleted = " (已清理)" if getattr(w, "spec_deleted", False) else ""
                lines.append(f"- [{status}] {w.item_id}: {q}{deleted} • {w.spec_path}")
        return "\n".join(lines)

    def format_metrics(self, project: SpecProject, tail: int = 20) -> str:
        tail = max(1, int(tail or 20))
        lines: list[str] = [f"📈 **Spec 指标** — {project.name}\n"]
        if not getattr(project, "metrics_history", None):
            lines.append("(暂无指标记录)")
            return "\n".join(lines)

        last = project.metrics_history[-1]
        lines.append(
            f"当前: 循环 {last.cycle_number}  •  目标达成度={last.goal_attainment:.2f}  •  优化空间={last.improvement_space:.2f}"
        )
        lines.append(f"验收标准: {last.satisfied_count}/{last.total_criteria}  •  backlog 待办: {last.backlog_pending}\n")

        lines.append("**最近变化:**")
        for m in project.metrics_history[-tail:]:
            lines.append(
                f"- 循环 {m.cycle_number}: +{m.new_satisfied} 达标  •  建议={m.review_suggestions}  •  达成度={m.goal_attainment:.2f}  •  空间={m.improvement_space:.2f}"
            )
        return "\n".join(lines)

    def format_guidance_injected(self, message: str) -> str:
        return f"""💬 **引导信息已注入**

> {message}

将在下一轮循环中生效。"""

    # ------------------------------------------------------------------
    # Title helpers (for card headers)
    # ------------------------------------------------------------------

    def get_analyzing_start_title(self) -> str:
        return "📋 Spec Agent 启动"

    def get_analyzing_done_title(self) -> str:
        return "✅ 需求分析完成"

    def get_cycle_start_title(self, cycle: int, max_cycles: int) -> str:
        return f"🔄 Spec 循环 [{cycle}/{max_cycles}]"

    def get_phase_title(self, cycle: int, phase: SpecPhase) -> str:
        return f"{phase.emoji} {phase.display_name} [循环 {cycle}]"

    def get_review_title(self, cycle: int, all_passed: bool) -> str:
        if all_passed:
            return f"✅ 审查通过 [循环 {cycle}]"
        return f"🔍 多视角审查 [循环 {cycle}]"

    def get_project_done_title(self, project: SpecProject) -> str:
        if project.status == SpecProjectStatus.COMPLETED:
            return "🎉 Spec 模式完成！"
        elif project.status == SpecProjectStatus.ABORTED:
            return "⚠️ Spec 模式终止"
        elif project.status == SpecProjectStatus.CLARIFYING:
            return "❓ 需要澄清"
        return "⏸️ Spec 模式暂停"

    def get_guidance_injected_title(self) -> str:
        return "💬 引导信息已注入"

    def get_error_title(self) -> str:
        return "❌ Spec Agent 错误"

    def get_status_title(self) -> str:
        return "📊 Spec 状态"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_progress_bar(completed: int, total: int) -> str:
        return make_progress_bar(completed, total)

    def get_progress_info(self, project: SpecProject) -> dict:
        return {
            "progress_bar": self._make_progress_bar(
                project.satisfied_count, project.total_criteria
            ),
            "satisfied_count": project.satisfied_count,
            "total_criteria": project.total_criteria,
            "cycle_count": project.current_cycle_number,
            "status": project.status,
            "project_name": project.name,
            "project_id": project.project_id,
            "is_running": project.status == SpecProjectStatus.RUNNING,
            "is_paused": project.status == SpecProjectStatus.PAUSED,
        }
