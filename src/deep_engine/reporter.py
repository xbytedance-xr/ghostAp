from typing import Optional
from .models import (
    DeepProject,
    DeepProjectStatus,
    DeepTask,
    DeepTaskStatus,
    ExecutionResult,
    ProgressUpdate,
)


class ProgressReporter:
    def format_planning_start(self, requirement: str) -> str:
        preview = requirement[:100] + "..." if len(requirement) > 100 else requirement
        return f"""🧠 **Deep Engine 启动**

📝 正在分析需求...
> {preview}

⏳ 请稍候，正在规划任务..."""

    def format_planning_done(self, project: DeepProject) -> str:
        lines = [
            f"✅ **任务规划完成**\n",
            f"📂 项目: {project.name}",
            f"📍 目录: `{project.root_path}`",
            f"📊 共 {project.total_count} 个任务\n",
            "**任务列表:**",
        ]

        for task in project.tasks:
            dep_info = ""
            if task.dependencies:
                dep_indices = [
                    str(t.order + 1) for t in project.tasks
                    if t.task_id in task.dependencies
                ]
                if dep_indices:
                    dep_info = f" (依赖: {', '.join(dep_indices)})"

            lines.append(f"  {task.order + 1}. {task.title}{dep_info}")

        lines.append("\n🚀 准备开始执行...")
        return "\n".join(lines)

    def format_task_start(self, task: DeepTask, current: int, total: int) -> str:
        progress_bar = self._make_progress_bar(current - 1, total)
        return f"""🔄 **执行任务 [{current}/{total}]**

{progress_bar}

📌 **{task.title}**
{task.description}

⏳ 正在执行..."""

    def format_task_progress(self, task: DeepTask, content: str) -> str:
        preview = content[-500:] if len(content) > 500 else content
        return f"""🔄 **{task.title}** 执行中...

```
{preview}
```"""

    def format_task_done(self, task: DeepTask, result: ExecutionResult, current: int, total: int) -> str:
        progress_bar = self._make_progress_bar(current, total)

        if result.success:
            output_preview = result.output[-800:] if len(result.output) > 800 else result.output
            return f"""✅ **任务完成 [{current}/{total}]**

{progress_bar}

📌 **{task.title}**
⏱️ 耗时: {result.duration:.1f}s

**执行结果:**
```
{output_preview}
```"""
        else:
            error_preview = result.error[:300] if result.error else "未知错误"
            return f"""❌ **任务失败 [{current}/{total}]**

{progress_bar}

📌 **{task.title}**
⏱️ 耗时: {result.duration:.1f}s

**错误信息:**
```
{error_preview}
```"""

    def format_project_done(self, project: DeepProject) -> str:
        progress_bar = self._make_progress_bar(project.completed_count, project.total_count)

        if project.status == DeepProjectStatus.COMPLETED:
            lines = [
                f"🎉 **全部任务完成！**\n",
                progress_bar,
                "",
                f"📂 项目: {project.name}",
                f"⏱️ 总耗时: {project.duration():.1f}s" if project.duration() else "",
                "",
                "**任务执行结果:**",
            ]
        elif project.status == DeepProjectStatus.FAILED:
            lines = [
                f"⚠️ **执行完成（有失败）**\n",
                progress_bar,
                "",
                f"📂 项目: {project.name}",
                f"⏱️ 总耗时: {project.duration():.1f}s" if project.duration() else "",
                f"❌ 失败任务: {project.failed_count} 个",
                "",
                "**任务执行结果:**",
            ]
        else:
            lines = [
                f"⏸️ **执行已暂停**\n",
                progress_bar,
                "",
                f"📂 项目: {project.name}",
                f"✅ 已完成: {project.completed_count}/{project.total_count}",
                "",
                "**任务状态:**",
            ]

        for task in project.tasks:
            status_emoji = self._get_status_emoji(task.status)
            duration_str = f" ({task.duration():.1f}s)" if task.duration() else ""
            lines.append(f"  {status_emoji} {task.order + 1}. {task.title}{duration_str}")

        return "\n".join(lines)

    def format_error(self, error: str) -> str:
        return f"""❌ **Deep Engine 错误**

```
{error[:500]}
```

请检查错误信息后重试。"""

    def format_status(self, project: DeepProject) -> str:
        progress = project.get_progress_update("")
        progress_bar = self._make_progress_bar(project.completed_count, project.total_count)

        status_text = {
            DeepProjectStatus.IDLE: "⏳ 等待开始",
            DeepProjectStatus.PLANNING: "🧠 正在规划",
            DeepProjectStatus.EXECUTING: "🔄 执行中",
            DeepProjectStatus.PAUSED: "⏸️ 已暂停",
            DeepProjectStatus.COMPLETED: "✅ 已完成",
            DeepProjectStatus.FAILED: "❌ 执行失败",
        }.get(project.status, "❓ 未知状态")

        lines = [
            f"📊 **{project.name}** 状态\n",
            progress_bar,
            "",
            f"状态: {status_text}",
            f"进度: {project.completed_count}/{project.total_count}",
        ]

        if project.duration():
            lines.append(f"耗时: {project.duration():.1f}s")

        current_task = project.get_current_task()
        if current_task:
            lines.append(f"\n当前任务: {current_task.title}")

        return "\n".join(lines)

    def _make_progress_bar(self, completed: int, total: int) -> str:
        if total == 0:
            return "[░░░░░░░░░░] 0%"

        percent = (completed / total) * 100
        filled = int(percent / 10)
        empty = 10 - filled

        return f"[{'█' * filled}{'░' * empty}] {percent:.0f}% ({completed}/{total})"

    def _get_status_emoji(self, status: DeepTaskStatus) -> str:
        return {
            DeepTaskStatus.PENDING: "⏳",
            DeepTaskStatus.READY: "🔜",
            DeepTaskStatus.IN_PROGRESS: "🔄",
            DeepTaskStatus.COMPLETED: "✅",
            DeepTaskStatus.FAILED: "❌",
            DeepTaskStatus.SKIPPED: "⏭️",
            DeepTaskStatus.BLOCKED: "🚫",
        }.get(status, "❓")


def create_deep_card_content(project: DeepProject, show_buttons: bool = True) -> dict:
    reporter = ProgressReporter()
    progress_bar = reporter._make_progress_bar(project.completed_count, project.total_count)

    status_text = {
        DeepProjectStatus.IDLE: "等待开始",
        DeepProjectStatus.PLANNING: "正在规划",
        DeepProjectStatus.EXECUTING: "执行中",
        DeepProjectStatus.PAUSED: "已暂停",
        DeepProjectStatus.COMPLETED: "已完成",
        DeepProjectStatus.FAILED: "执行失败",
    }.get(project.status, "未知")

    task_lines = []
    for task in project.tasks:
        emoji = reporter._get_status_emoji(task.status)
        duration = f" ({task.duration():.1f}s)" if task.duration() else ""
        task_lines.append(f"{emoji} {task.order + 1}. {task.title}{duration}")

    content = {
        "header": {
            "title": f"🧠 Deep Engine - {project.name}",
            "subtitle": f"{status_text} | {progress_bar}",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "\n".join(task_lines),
            }
        ],
    }

    if show_buttons and project.status in (DeepProjectStatus.EXECUTING, DeepProjectStatus.PAUSED):
        buttons = []
        if project.status == DeepProjectStatus.EXECUTING:
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "⏸️ 暂停"},
                "type": "default",
                "value": {"action": "deep_pause", "project_id": project.project_id},
            })
        elif project.status == DeepProjectStatus.PAUSED:
            buttons.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": "▶️ 继续"},
                "type": "primary",
                "value": {"action": "deep_resume", "project_id": project.project_id},
            })

        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🛑 停止"},
            "type": "danger",
            "value": {"action": "deep_stop", "project_id": project.project_id},
        })

        content["elements"].append({
            "tag": "action",
            "actions": buttons,
        })

    return content
