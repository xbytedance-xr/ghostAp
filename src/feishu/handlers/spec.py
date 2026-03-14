"""Spec Engine handler — start, status, pause, resume, stop, guide."""

from __future__ import annotations

import logging
import os
import time
import asyncio
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...spec_engine import SpecEngineCallbacks
from ...spec_engine.task_persistence import list_pending_tasks, load_task_state, delete_task_state
from ...spec_engine.models import (
    SpecProject,
    SpecProjectStatus,
    SpecPhase,
    ReviewResult,
)
from ...tasking import TaskSpec, TaskPriority
from ...utils.errors import fmt_error
from ...utils.text import append_duration_to_title, generate_task_id
from ..emoji import EmojiReaction
from .base import BaseHandler
from ..renderers.spec_renderer import SpecRenderer

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class SpecHandler(BaseHandler):
    """Manages the full lifecycle of Spec Engine tasks."""

    def __init__(self, ctx: "HandlerContext") -> None:
        super().__init__(ctx)
        self.renderer = SpecRenderer(self)

    def _get_ui_state(self, spec_project_id: str) -> dict:
        """Deprecated: Delegate to renderer"""
        return self.renderer.get_ui_state(spec_project_id)

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------
    def handle_spec_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        text_lower = text.lower().strip()

        if text_lower == "/spec_recover":
            self.show_recoverable_tasks(message_id, chat_id)
        elif text_lower.startswith("/spec_recover "):
            task_id = text[len("/spec_recover "):].strip()
            self.recover_spec_task(message_id, chat_id, task_id, project)
        elif text_lower == "/spec_status" or text_lower.startswith("/spec_status "):
            self.show_spec_status(message_id, chat_id, project)
        elif text_lower == "/spec_history" or text_lower.startswith("/spec_history"):
            self.show_spec_history(message_id, chat_id, text, project)
        elif text_lower == "/spec_metrics" or text_lower.startswith("/spec_metrics"):
            self.show_spec_metrics(message_id, chat_id, text, project)
        elif text_lower == "/spec_config" or text_lower.startswith("/spec_config"):
            self.show_spec_config(message_id, chat_id, project)
        elif text_lower == "/spec_export":
            self.export_spec_report(message_id, chat_id, project)
        elif text_lower == "/spec_save":
            self.save_spec_state(message_id, chat_id, project)
        elif text_lower == "/stop_spec" or text_lower.startswith("/stop_spec "):
            self.stop_spec_engine(message_id, chat_id, project)
        elif text_lower == "/spec_pause":
            self.pause_spec_engine(message_id, chat_id, project)
        elif text_lower == "/spec_resume":
            self.resume_spec_engine(message_id, chat_id, project)
        elif text_lower.startswith("/spec_guide "):
            guide_message = text[len("/spec_guide "):].strip()
            self.update_spec_guidance(message_id, chat_id, guide_message, project)
        elif text_lower == "/spec_guide":
            self.reply_message(message_id, "📝 请提供引导信息\n\n用法: `/spec_guide <引导描述>`\n\n例如: `/spec_guide 优先考虑性能优化`")
        elif text_lower.startswith("/spec "):
            requirement = text[6:].strip()
            self.start_spec_engine(message_id, chat_id, requirement, project)
        elif text_lower == "/spec":
            self.reply_message(
                message_id,
                "📋 **Spec 模式：结构化开发闭环**\n\n"
                "用法：`/spec <你的需求描述>`\n"
                "示例：`/spec 实现用户登录注册功能，支持邮箱和手机号`\n\n"
                "**Spec vs Deep vs Loop**\n"
                "- Spec：按 `Spec→Plan→Task→Build→Review` 产出结构化产物并迭代收敛\n"
                "- Deep：一次性深度拆解并执行一个复杂任务（更偏单次冲刺）\n"
                "- Loop：以验收标准为中心做多轮迭代闭环（更偏持续推进达标）\n\n"
                "**最小示例（推荐命令组合）**\n"
                "- Web：`/spec 做一个登录页+登录接口` → `/spec_status` → `/spec_guide 优先补测试与错误提示`\n"
                "- API：`/spec 新增 /v1/users 查询接口` → `/spec_status`\n"
                "- 脚本：`/spec 写一个批量重命名脚本，支持dry-run` → `/spec_status`\n\n"
                "**可用命令**\n"
                "- `/spec <需求>`：启动\n"
                "- `/spec_guide <引导>`：补充约束/偏好（下轮生效）\n"
                "- `/spec_status`：查看进度\n"
                "- `/spec_history`：查看 spec 文件与循环历史\n"
                "- `/spec_metrics`：查看目标达成度与指标变化\n"
                "- `/spec_config`：查看 Spec 长程配置（阈值/保留策略）\n"
                "- `/spec_export`：导出当前 Spec/Plan 报告\n"
                "- `/spec_save`：立即落盘保存状态（用于断点续传）\n"
                "- `/spec_pause`：暂停\n"
                "- `/spec_resume`：恢复\n"
                "- `/spec_recover`：列出或恢复异常中断的任务（需指定 Task ID）\n"
                "- `/stop_spec`：停止\n"
            )
        else:
            self.reply_message(message_id, "❓ 未知的 Spec 命令")

    # ------------------------------------------------------------------
    # start
    # ------------------------------------------------------------------
    def start_spec_engine(self, message_id: str, chat_id: str, requirement: str, project: Optional["ProjectContext"] = None):
        if not project:
            working_dir = self.get_working_dir(chat_id)
            try:
                project, is_new = self.project_manager.get_or_create_project_for_path(working_dir, chat_id)
                if is_new:
                    logger.info("Spec Engine 自动创建项目: %s @ %s", project.project_name, project.root_path)
            except Exception as e:
                self.reply_message(message_id, fmt_error("创建项目", str(e)))
                return

        root_path = project.root_path if project else self.get_working_dir(chat_id)

        existing = self.ctx.spec_engine_manager.get(chat_id, root_path)
        if existing and existing.is_running:
            self.reply_message(
                message_id,
                "⚠️ 当前项目已有 Spec 任务在执行中\n\n发送 `/spec_status` 查看进度\n发送 `/stop_spec` 停止任务",
            )
            return

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.spec_reporter

        # Send startup card
        content = reporter.format_analyzing_start(requirement)
        title = reporter.get_analyzing_start_title()
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=title,
            content=f"{content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else content,
            engine_name=f"Spec({engine_name})", show_buttons=False,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=message_id, request_id=request_id)

        engine = self.ctx.spec_engine_manager.get_or_create(chat_id, root_path, engine_name=engine_name)

        project_name = project.project_name if project else os.path.basename(root_path) or "spec"
        task_id = generate_task_id(project_name)

        _on_rate_limit = self.create_rate_limit_callback(chat_id, message_id, project, f"Spec({engine_name})", request_id)

        def run_spec_engine():
            try:
                callbacks = self.renderer.create_spec_callbacks(message_id, chat_id, project, engine_name)
                engine.execute(requirement, callbacks, task_id=task_id, on_rate_limit=_on_rate_limit)
            except Exception as e:
                if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
                    logger.warning("Spec Engine 执行超时 (task_id=%s): %s", task_id, e)
                else:
                    logger.error("Spec Engine 执行异常: %s", e, exc_info=True)
                
                # 使用增强的 fmt_error 处理异常消息
                formatted = fmt_error("", e)
                if formatted.startswith("❌ 失败: "):
                    err_msg = formatted[len("❌ 失败: "):]
                elif formatted == "❌ 失败":
                    err_msg = "未知错误"
                else:
                    err_msg = formatted
                
                error_content = reporter.format_error(err_msg)
                error_title = reporter.get_error_title()
                err_msg_type, err_card = CardBuilder.build_deep_card(
                    project=project, title=error_title,
                    content=f"{error_content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else error_content,
                    engine_name=f"Spec({engine_name})", show_buttons=False,
                )
                self.send_message(chat_id, err_card, err_msg_type, origin_message_id=message_id, request_id=request_id)

        spec = TaskSpec(
            chat_id=chat_id,
            queue_key=f"{chat_id}:spec:{project.project_id if project else root_path}",
            name="spec_engine_run",
            task_type="spec_engine",
            project_id=project.project_id if project else None,
            message_id=message_id,
            origin_message_id=message_id,
            request_id=request_id,
            task_id=task_id,
            priority=TaskPriority.NORMAL,
        )
        handle = self.scheduler.submit(spec, lambda ctx: run_spec_engine())
        try:
            self.ctx.message_linker.link_task(message_id, handle.run_id)
        except Exception as e:
            logger.debug("link_task失败(spec_engine_run): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)

    # ------------------------------------------------------------------
    # callbacks factory
    # ------------------------------------------------------------------
    def _create_spec_callbacks(self, *args, **kwargs):
        """Deprecated: Delegate to renderer"""
        return self.renderer.create_spec_callbacks(*args, **kwargs)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def show_spec_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, origin_message_id: Optional[str] = None):
        # User command "/spec_status" resets to status view
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        spec_project_id = project.project_id if project else root_path
        
        self.renderer.update_ui_state(spec_project_id, view_mode="status", view_context={})
        
        self.renderer.render_current_view(message_id, chat_id, project, origin_message_id)

    def show_spec_history(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        try:
            engine = self.ctx.spec_engine_manager.load_or_create_from_disk(chat_id, root_path, engine_name=engine_name)
        except Exception:
            engine = self.ctx.spec_engine_manager.get(chat_id, root_path)
        if not engine or not engine.project:
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title="🗂️ Spec 历史",
                content="当前没有可查询的 Spec 历史（未运行过或未落盘）\n\n发送 `/spec <需求>` 启动后会自动生成历史。",
                engine_name=f"Spec({engine_name})", show_buttons=False,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        tail = 20
        try:
            parts = (text or "").strip().split()
            if len(parts) >= 2 and parts[1].isdigit():
                tail = max(1, min(500, int(parts[1])))
        except Exception:
            tail = 20
        content = self.ctx.spec_reporter.format_history(engine.project, tail=tail)
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title="🗂️ Spec 历史", content=content,
            engine_name=f"Spec({engine.engine_name})", show_buttons=False,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def show_spec_metrics(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        try:
            engine = self.ctx.spec_engine_manager.load_or_create_from_disk(chat_id, root_path, engine_name=engine_name)
        except Exception:
            engine = self.ctx.spec_engine_manager.get(chat_id, root_path)
        if not engine or not engine.project:
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title="📈 Spec 指标",
                content="当前没有可查询的 Spec 指标（未运行过或未落盘）\n\n发送 `/spec <需求>` 启动后会自动记录指标。",
                engine_name=f"Spec({engine_name})", show_buttons=False,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        tail = 20
        try:
            parts = (text or "").strip().split()
            if len(parts) >= 2 and parts[1].isdigit():
                tail = max(1, min(500, int(parts[1])))
        except Exception:
            tail = 20
        content = self.ctx.spec_reporter.format_metrics(engine.project, tail=tail)
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title="📈 Spec 指标", content=content,
            engine_name=f"Spec({engine.engine_name})", show_buttons=False,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def show_spec_config(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        s = self.settings
        content = (
            "🧩 **Spec 长程配置**\n\n"
            f"- max_cycles: `{getattr(s, 'spec_max_cycles', None)}` (limit=`{getattr(s, 'spec_max_cycles_limit', None)}`)\n"
            f"- infinite_mode: `{getattr(s, 'spec_infinite_mode', None)}`\n"
            f"- disable_convergence: `{getattr(s, 'spec_disable_convergence', None)}`\n"
            f"- disable_early_stop: `{getattr(s, 'spec_disable_early_stop', None)}`\n"
            f"- discovery_enabled: `{getattr(s, 'spec_discovery_enabled', None)}`\n"
            f"- generated_specs_per_cycle: `{getattr(s, 'spec_generated_specs_per_cycle', None)}`\n"
            f"- generated_specs_retention: `{getattr(s, 'spec_generated_specs_retention', None)}`\n"
            f"- state_file: `{getattr(s, 'spec_state_filename', None)}` (cycles_tail=`{getattr(s, 'spec_state_cycles_tail', None)}`)\n"
            f"- artifacts_dir: `{getattr(s, 'spec_artifacts_dirname', None)}` (cycle_retention=`{getattr(s, 'spec_cycle_artifact_retention', None)}`)\n"
            f"- history_log: `{getattr(s, 'spec_history_log_filename', None)}`\n"
        )
        engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title="🧩 Spec 配置", content=content,
            engine_name=f"Spec({engine_name})", show_buttons=False,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def export_spec_report(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.spec_engine_manager.get(chat_id, root_path)
        
        # Try to load from disk if not in memory
        if not engine:
            try:
                engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
                engine = self.ctx.spec_engine_manager.load_or_create_from_disk(chat_id, root_path, engine_name=engine_name)
            except Exception:
                pass

        if not engine or not engine.project or not engine.project.cycles:
            self.reply_message(message_id, "❌ 当前没有可导出的 Spec 记录")
            return

        spec_project = engine.project
        lines = [f"# Spec Project Export: {spec_project.name}\n"]
        lines.append(f"**Generated at**: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        lines.append(f"**Status**: {spec_project.status.value}")
        lines.append(f"**Requirement**: {spec_project.requirement}\n")

        lines.append("## Acceptance Criteria")
        tracker = spec_project.criteria_tracker
        for i, c in enumerate(tracker.criteria):
            mark = "✅" if tracker.satisfied.get(i) else "🔲"
            lines.append(f"- {mark} {c}")
        lines.append("")

        # Latest successful artifacts
        latest_cycle = spec_project.current_cycle
        if latest_cycle:
            lines.append(f"## Latest Cycle (Cycle {latest_cycle.cycle_number})")
            
            if latest_cycle.spec_content:
                lines.append("### Functional Spec")
                lines.append(latest_cycle.spec_content)
                lines.append("")
            
            if latest_cycle.plan_content:
                lines.append("### Implementation Plan")
                lines.append(latest_cycle.plan_content)
                lines.append("")

            if latest_cycle.review_result:
                lines.append("### Review Result")
                lines.append(self.ctx.spec_reporter.format_review_result(latest_cycle.review_result, latest_cycle.cycle_number))
                lines.append("")

        # Save to file
        export_filename = f"spec_export_{spec_project.project_id}_{int(time.time())}.md"
        export_path = os.path.join(root_path, export_filename)
        try:
            with open(export_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self.reply_message(message_id, f"✅ 导出成功: `{export_path}`")
        except Exception as e:
            self.reply_message(message_id, f"❌ 导出失败: {str(e)}")

    def save_spec_state(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.spec_engine_manager.get(chat_id, root_path)
        if not engine:
            engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
            engine = self.ctx.spec_engine_manager.get_or_create(chat_id, root_path, engine_name=engine_name)
        if not engine or not engine.project:
            self.reply_message(message_id, "当前没有可保存的 Spec 状态（请先运行 /spec）")
            return
        try:
            path = engine.save_state()
            self.reply_message(message_id, f"✅ 已保存 Spec 状态到: `{path}`")
        except Exception as e:
            self.reply_message(message_id, fmt_error("保存 Spec 状态", str(e)))

    # ------------------------------------------------------------------
    # pause / resume / stop
    # ------------------------------------------------------------------
    def pause_spec_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.spec_engine_manager.get(chat_id, root_path)
        if not engine:
            engine = self.ctx.spec_engine_manager.get_active_engine(chat_id)
        if engine and engine.is_running:
            engine.pause()
            try:
                if engine.project:
                    engine.save_state()
            except Exception:
                pass
            self.show_spec_status(message_id, chat_id, project=project)
            return
        self.reply_message(message_id, "当前没有正在执行的 Spec 任务")

    def resume_spec_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.spec_engine_manager.get(chat_id, root_path)

        if not engine or not engine.project:
            # 断点续传：尝试从磁盘加载
            try:
                engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
                engine = self.ctx.spec_engine_manager.load_or_create_from_disk(chat_id, root_path, engine_name=engine_name)
            except Exception:
                pass

        if not engine:
            paused = [e for e in self.ctx.spec_engine_manager.list_engines(chat_id)
                      if e.project and e.project.status in (SpecProjectStatus.PAUSED, SpecProjectStatus.CLARIFYING)]
            if len(paused) == 1:
                engine = paused[0]

        if engine and engine.project and engine.project.status in (SpecProjectStatus.PAUSED, SpecProjectStatus.CLARIFYING):
            callbacks = self.renderer.create_spec_callbacks(message_id, chat_id, project, engine_name=engine.engine_name)

            def run_resume():
                engine.resume(callbacks)

            request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
            spec = TaskSpec(
                chat_id=chat_id,
                queue_key=f"{chat_id}:spec:{project.project_id if project else root_path}",
                name="spec_engine_resume", task_type="spec_engine",
                project_id=project.project_id if project else None,
                message_id=message_id, origin_message_id=message_id,
                request_id=request_id, priority=TaskPriority.HIGH,
            )
            handle = self.scheduler.submit(spec, lambda ctx: run_resume())
            try:
                self.ctx.message_linker.link_task(message_id, handle.run_id)
            except Exception as e:
                logger.debug("link_task失败(spec_engine_resume): err=%s", e)
            self.show_spec_status(message_id, chat_id, project=project)
        else:
            self.reply_message(message_id, "当前没有可恢复的 Spec 任务")

    def stop_spec_engine(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.spec_engine_manager.get(chat_id, root_path)

        if not engine:
            running = self.ctx.spec_engine_manager.get_active_engines(chat_id)
            if len(running) == 1:
                engine = running[0]

        if not engine or not engine.is_running:
            self.reply_message(message_id, "📊 当前没有正在执行的 Spec 任务")
            return

        engine.stop()
        try:
            if engine.project:
                engine.save_state()
        except Exception:
            pass
        self.show_spec_status(message_id, chat_id, project=project)

    # ------------------------------------------------------------------
    # guidance
    # ------------------------------------------------------------------
    def update_spec_guidance(self, message_id: str, chat_id: str, guide_message: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        engine = None
        if project:
            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path)

        if not engine:
            # Fallback: if there's exactly one runnable engine in the chat
            candidates = [
                e for e in self.ctx.spec_engine_manager.list_engines(chat_id)
                if e.project and e.project.status in (
                    SpecProjectStatus.RUNNING,
                    SpecProjectStatus.PAUSED,
                    SpecProjectStatus.CLARIFYING,
                )
            ]
            if len(candidates) == 1:
                engine = candidates[0]

        if not engine or not engine.project:
            self.reply_message(
                message_id,
                "⚠️ 当前没有可注入引导的 Spec 任务（运行中/已暂停/待澄清）\n\n"
                "请先使用 `/spec <需求>` 启动，或发送 `/spec_status` 查看当前任务",
            )
            return

        if engine.project.status not in (
            SpecProjectStatus.RUNNING,
            SpecProjectStatus.PAUSED,
            SpecProjectStatus.CLARIFYING,
        ):
            self.reply_message(
                message_id,
                "⚠️ 当前 Spec 任务状态不支持注入引导（仅支持：运行中/已暂停/待澄清）\n\n"
                "发送 `/spec_status` 查看状态",
            )
            return

        engine.inject_guidance(guide_message)
        reporter = self.ctx.spec_reporter
        content = reporter.format_guidance_injected(guide_message)
        title = reporter.get_guidance_injected_title()
        engine_name = engine.engine_name

        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=title, content=content,
            engine_name=f"Spec({engine_name})", show_buttons=False,
        )
        self.send_message(chat_id, card_content, msg_type)

    # ------------------------------------------------------------------
    # recover
    # ------------------------------------------------------------------
    def show_recoverable_tasks(self, message_id: str, chat_id: str):
        tasks = list_pending_tasks()
        if not tasks:
            self.reply_message(message_id, "📋 没有可恢复的任务")
            return

        lines = ["📋 **可恢复的 Spec 任务**\n"]
        for t in tasks:
            import time as _time
            created_str = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(t.created_at))
            req_summary = t.requirement[:50] + "..." if len(t.requirement) > 50 else t.requirement
            lines.append(f"**{t.task_id}**")
            lines.append(f"- 需求: {req_summary}")
            lines.append(f"- 创建时间: {created_str}")
            if t.last_error:
                error_summary = t.last_error[:80] + "..." if len(t.last_error) > 80 else t.last_error
                lines.append(f"- 最后错误: {error_summary}")
            lines.append("")

        lines.append("使用 `/spec_recover <任务ID>` 恢复指定任务")
        self.reply_message(message_id, "\n".join(lines))

    def recover_spec_task(self, message_id: str, chat_id: str, task_id: str, project: Optional["ProjectContext"] = None):
        state = load_task_state(task_id)
        if not state:
            self.reply_message(message_id, f"❌ 未找到任务: {task_id}")
            return

        project_path = state.project_path
        if not os.path.isdir(project_path):
            self.reply_message(message_id, f"❌ 项目路径不存在: {project_path}")
            return

        if not project:
            try:
                project, _ = self.project_manager.get_or_create_project_for_path(project_path, chat_id)
            except Exception as e:
                self.reply_message(message_id, fmt_error("恢复项目上下文", str(e)))
                return

        existing = self.ctx.spec_engine_manager.get(chat_id, project_path)
        if existing and existing.is_running:
            self.reply_message(
                message_id,
                "⚠️ 当前项目已有 Spec 任务在执行中\n\n发送 `/spec_status` 查看进度\n发送 `/stop_spec` 停止任务",
            )
            return

        self.add_reaction(message_id, EmojiReaction.on_multi_task_start())

        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        engine_name = state.agent_type.capitalize() if state.agent_type else "Coco"
        reporter = self.ctx.spec_reporter

        content = reporter.format_analyzing_start(state.requirement)
        title = f"🔄 恢复任务 {task_id}"
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=title,
            content=f"{content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else content,
            engine_name=f"Spec({engine_name})", show_buttons=False,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type, origin_message_id=message_id, request_id=request_id)

        engine = self.ctx.spec_engine_manager.get_or_create(chat_id, project_path, engine_name=engine_name)

        if state.project_snapshot:
            try:
                engine._project = SpecProject.from_dict(state.project_snapshot)
                # Ensure status is PAUSED so resume() accepts it
                engine._project.status = SpecProjectStatus.PAUSED
                # Force update total cycle count from snapshot if needed
                if engine._project.cycles:
                     engine._project.cycle_count_total = max(
                         engine._project.cycle_count_total, 
                         engine._project.cycles[-1].cycle_number
                     )
            except Exception as e:
                logger.warning("恢复 project_snapshot 失败: %s", e)

        _on_rate_limit = self.create_rate_limit_callback(chat_id, message_id, project, f"Spec({engine_name})", request_id)

        def run_spec_engine():
            try:
                callbacks = self.renderer.create_spec_callbacks(message_id, chat_id, project, engine_name)
                # Use resume() instead of execute() to preserve state
                # The execute() method re-initializes the project, wiping previous progress.
                engine.resume(callbacks)
                delete_task_state(task_id)
            except Exception as e:
                if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
                    logger.warning("Spec Engine 恢复超时 (task_id=%s): %s", task_id, e)
                else:
                    logger.error("Spec Engine 恢复执行异常: %s", e, exc_info=True)
                
                formatted = fmt_error("", e)
                if formatted.startswith("❌ 失败: "):
                    err_msg = formatted[len("❌ 失败: "):]
                elif formatted == "❌ 失败":
                    err_msg = "未知错误"
                else:
                    err_msg = formatted
                
                error_content = reporter.format_error(err_msg)
                error_title = reporter.get_error_title()
                err_msg_type, err_card = CardBuilder.build_deep_card(
                    project=project, title=error_title,
                    content=f"{error_content}\n\n{self.format_ref_note(message_id, request_id)}" if request_id else error_content,
                    engine_name=f"Spec({engine_name})", show_buttons=False,
                )
                self.send_message(chat_id, err_card, err_msg_type, origin_message_id=message_id, request_id=request_id)

        spec = TaskSpec(
            chat_id=chat_id,
            queue_key=f"{chat_id}:spec:{project.project_id if project else project_path}",
            name="spec_engine_recover",
            task_type="spec_engine",
            project_id=project.project_id if project else None,
            message_id=message_id,
            origin_message_id=message_id,
            request_id=request_id,
            task_id=task_id,
            priority=TaskPriority.NORMAL,
        )
        handle = self.scheduler.submit(spec, lambda ctx: run_spec_engine())
        try:
            self.ctx.message_linker.link_task(message_id, handle.run_id)
        except Exception as e:
            logger.debug("link_task失败(spec_engine_recover): message_id=%s, run_id=%s, err=%s", message_id, handle.run_id, e)

    # ------------------------------------------------------------------
    # UI Interaction Handlers
    # ------------------------------------------------------------------
    def handle_card_action(self, open_message_id: str, open_chat_id: str, action_type: str, value: dict):
        """Handle spec_* card actions."""
        project_id = value.get("project_id", "")
        # Note: Spec engine uses 'deep_project_id' key for compatibility/convention with base templates,
        # but in Spec context it might be root_path or project_id.
        spec_project_id = value.get("deep_project_id", "")
        
        # Resolve target project
        target_project = self.project_manager.get_project(project_id) if project_id else None
        if not target_project and spec_project_id:
            try:
                if os.path.isabs(spec_project_id):
                     target_project = self.project_manager.find_project_by_path(spec_project_id)
                else:
                     target_project = self.project_manager.get_project(spec_project_id)
            except Exception:
                pass

        spec_actions = {
            "spec_pause":  self.pause_spec_engine,
            "spec_resume": self.resume_spec_engine,
            "spec_stop":   self.stop_spec_engine,
        }

        # Try dispatching standard actions first
        if self._dispatch_standard_card_action(
            open_message_id,
            open_chat_id,
            action_type,
            value,
            prefix="spec",
            action_map=spec_actions,
            toggle_log_method=self.toggle_spec_log,
            switch_mode_method=self.switch_spec_card_mode,
            toggle_ac_method=self.toggle_spec_ac,
            project=target_project,
        ):
            return

    def toggle_spec_log(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, spec_project_id: Optional[str] = None, expanded: bool = False):
        if spec_project_id:
            self.renderer.update_ui_state(spec_project_id, expanded=expanded)
            # Refresh card with new state
            self.show_spec_status(message_id, chat_id, project, origin_message_id=message_id)

    def toggle_spec_ac(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, spec_project_id: Optional[str] = None, expand_ac: bool = False):
        if spec_project_id:
            self.renderer.update_ui_state(spec_project_id, expand_ac=expand_ac)
            # Refresh card with new state
            self.show_spec_status(message_id, chat_id, project, origin_message_id=message_id)

    def switch_spec_card_mode(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, spec_project_id: Optional[str] = None, compact: bool = False):
        if spec_project_id:
            self.renderer.update_ui_state(spec_project_id, compact=compact)
            # Refresh card with new state
            self.show_spec_status(message_id, chat_id, project, origin_message_id=message_id)
