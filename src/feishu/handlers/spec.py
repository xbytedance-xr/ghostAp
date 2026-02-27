"""Spec Engine handler — start, status, pause, resume, stop, guide."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...spec_engine import SpecEngineCallbacks
from ...spec_engine.models import (
    SpecProject,
    SpecProjectStatus,
    SpecPhase,
    SpecCycle,
    ReviewResult,
)
from ...tasking import TaskSpec, TaskPriority
from ...utils.errors import fmt_error
from ...utils.text import append_duration_to_title, generate_task_id
from ..emoji import EmojiReaction
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext
    from ..handler_context import HandlerContext

logger = logging.getLogger(__name__)


class SpecHandler(BaseHandler):
    """Manages the full lifecycle of Spec Engine tasks."""

    # ------------------------------------------------------------------
    # Command router
    # ------------------------------------------------------------------
    def handle_spec_command(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        text_lower = text.lower().strip()

        if text_lower == "/spec_status" or text_lower.startswith("/spec_status "):
            self.show_spec_status(message_id, chat_id, project)
        elif text_lower == "/spec_history" or text_lower.startswith("/spec_history"):
            self.show_spec_history(message_id, chat_id, text, project)
        elif text_lower == "/spec_metrics" or text_lower.startswith("/spec_metrics"):
            self.show_spec_metrics(message_id, chat_id, text, project)
        elif text_lower == "/spec_config" or text_lower.startswith("/spec_config"):
            self.show_spec_config(message_id, chat_id, project)
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
                "- `/spec_save`：立即落盘保存状态（用于断点续传）\n"
                "- `/spec_pause`：暂停\n"
                "- `/spec_resume`：恢复\n"
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
                callbacks = self._create_spec_callbacks(message_id, chat_id, project, engine_name)
                engine.execute(requirement, callbacks, task_id=task_id, on_rate_limit=_on_rate_limit)
            except Exception as e:
                logger.error("Spec Engine 执行异常: %s", e, exc_info=True)
                error_content = reporter.format_error(str(e))
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
    def _create_spec_callbacks(self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str = "Coco") -> SpecEngineCallbacks:
        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=(project.project_id if project else None))
        reporter = self.ctx.spec_reporter
        thread_root_message_id: list[str | None] = [None]

        def _send_spec_message(card_content: str, msg_type: str = "interactive"):
            use_thread = self.settings.default_reply_mode == "thread"
            if use_thread:
                reply_to = thread_root_message_id[0] or message_id
                result_id = self.reply_message(
                    reply_to, card_content, msg_type=msg_type,
                    origin_message_id=message_id, request_id=request_id,
                    reply_in_thread=True,
                )
                if thread_root_message_id[0] is None and result_id:
                    thread_root_message_id[0] = result_id
            else:
                self.send_message(chat_id, card_content, msg_type, origin_message_id=message_id, request_id=request_id)

        def on_analyzing_done(spec_project: SpecProject):
            content = reporter.format_analyzing_done(spec_project)
            title = reporter.get_analyzing_done_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                engine_name=f"Spec({engine_name})", show_buttons=False,
            )
            _send_spec_message(card_content, msg_type)

        def on_cycle_start(current: int, max_cycles: int):
            content = reporter.format_cycle_start(current, max_cycles)
            title = reporter.get_cycle_start_title(current, max_cycles)
            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")
            progress_bar = None
            if engine and engine.project:
                progress_bar = reporter._make_progress_bar(engine.project.satisfied_count, engine.project.total_criteria)
                title = append_duration_to_title(title, engine.project.duration())
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                progress_bar=progress_bar,
                is_executing=True, engine_name=f"Spec({engine_name})",
            )
            _send_spec_message(card_content, msg_type)

        def on_phase_done(cycle: int, phase: SpecPhase, output: str):
            content = reporter.format_phase_done(cycle, phase, output)
            title = reporter.get_phase_title(cycle, phase)
            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")

            # Surface structured-artifact validation / clarification in UI (avoid silent fallback)
            if engine and engine.project and engine.project.current_cycle and engine.project.current_cycle.cycle_number == cycle:
                cc = engine.project.current_cycle
                notice: list[str] = []
                if phase == SpecPhase.SPEC:
                    if getattr(cc, "spec_artifact_errors", None):
                        notice.append("⚠️ **规格产物不合规**（已降级为纯文本）：")
                        for e in cc.spec_artifact_errors[:3]:
                            notice.append(f"- {e}")
                    if cc.spec_artifact and cc.spec_artifact.clarification_questions:
                        notice.append("\nℹ️ **已自主决策的模糊点：**")
                        for q in cc.spec_artifact.clarification_questions[:8]:
                            notice.append(f"- {q}")
                elif phase == SpecPhase.PLAN:
                    if getattr(cc, "plan_artifact_errors", None):
                        notice.append("⚠️ **规划产物不合规**（已降级为纯文本）：")
                        for e in cc.plan_artifact_errors[:3]:
                            notice.append(f"- {e}")

                if notice:
                    content = "\n".join(notice) + "\n\n" + content
            progress_bar = None
            if engine and engine.project:
                progress_bar = reporter._make_progress_bar(engine.project.satisfied_count, engine.project.total_criteria)
                title = append_duration_to_title(title, engine.project.duration())
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                progress_bar=progress_bar,
                is_executing=bool(engine and engine.project and engine.project.status == SpecProjectStatus.RUNNING),
                engine_name=f"Spec({engine_name})",
            )
            _send_spec_message(card_content, msg_type)

        def on_review_done(cycle: int, review: ReviewResult):
            content = reporter.format_review_result(review, cycle)
            title = reporter.get_review_title(cycle, review.all_passed)
            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")
            progress_bar = None
            if engine and engine.project:
                progress_bar = reporter._make_progress_bar(engine.project.satisfied_count, engine.project.total_criteria)
                title = append_duration_to_title(title, engine.project.duration())
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                progress_bar=progress_bar,
                is_executing=True, engine_name=f"Spec({engine_name})",
            )
            _send_spec_message(card_content, msg_type)

        def on_project_done(spec_project: SpecProject):
            content = reporter.format_project_done(spec_project)
            title = reporter.get_project_done_title(spec_project)
            progress_bar = reporter._make_progress_bar(spec_project.satisfied_count, spec_project.total_criteria)
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                progress_bar=progress_bar, engine_name=f"Spec({engine_name})",
            )
            _send_spec_message(card_content, msg_type)
            self.add_reaction(message_id, EmojiReaction.on_multi_task_done())

        def on_error(error: str):
            content = reporter.format_error(error)
            title = reporter.get_error_title()
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                engine_name=f"Spec({engine_name})", show_buttons=False,
            )
            _send_spec_message(card_content, msg_type)
            self.add_reaction(message_id, EmojiReaction.on_error())

        def on_phase_start(cycle: int, phase: SpecPhase):
            content = reporter.format_phase_start(cycle, phase)
            title = reporter.get_phase_title(cycle, phase)
            engine = self.ctx.spec_engine_manager.get(chat_id, project.root_path if project else "")
            title = append_duration_to_title(title, engine.project.duration() if engine and engine.project else None)
            msg_type, card_content = CardBuilder.build_deep_card(
                project=project, title=title, content=content,
                is_executing=True, engine_name=f"Spec({engine_name})",
                show_buttons=False,
            )
            _send_spec_message(card_content, msg_type)

        return SpecEngineCallbacks(
            on_analyzing_done=on_analyzing_done,
            on_cycle_start=on_cycle_start,
            on_phase_start=on_phase_start,
            on_phase_done=on_phase_done,
            on_review_done=on_review_done,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def show_spec_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        engine = self.ctx.spec_engine_manager.get(chat_id, root_path)
        reporter = self.ctx.spec_reporter

        if not engine or not engine.project:
            # 断点续传：尝试从磁盘加载状态（进程重启后也可恢复）
            try:
                engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
                engine = self.ctx.spec_engine_manager.load_or_create_from_disk(chat_id, root_path, engine_name=engine_name)
            except Exception:
                pass

        if not engine or not engine.project:
            running = self.ctx.spec_engine_manager.get_active_engines(chat_id)
            if len(running) == 1 and running[0].project:
                engine = running[0]
            else:
                engine_name = self.get_engine_name(chat_id, project_id=(project.project_id if project else None))
                msg_type, card_content = CardBuilder.build_deep_card(
                    project=project, title="📊 Spec 状态",
                    content="当前没有 Spec 任务\n\n发送 `/spec 你的需求` 开始 Spec 模式开发",
                    engine_name=f"Spec({engine_name})", show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        status_content = reporter.format_status(engine.project)

        # Surface disk-resume information for better UX
        meta = getattr(engine, "_resume_meta", None)
        if meta and isinstance(meta, dict):
            try:
                state_path = meta.get("state_path")
                saved_at = meta.get("saved_at")
                compact = meta.get("compact")
                hint_lines = ["💾 **断点恢复信息**"]
                if state_path:
                    hint_lines.append(f"- state: `{state_path}`")
                if saved_at:
                    hint_lines.append(f"- saved_at: `{saved_at}`")
                if compact:
                    hint_lines.append(f"- compact: `{compact}`")
                status_content = "\n".join(hint_lines) + "\n\n" + status_content
            except Exception:
                pass
        status_title = reporter.get_status_title()
        progress_info = reporter.get_progress_info(engine.project)
        engine_name = engine.engine_name
        msg_type, card_content = CardBuilder.build_deep_card(
            project=project, title=status_title, content=status_content,
            progress_bar=progress_info["progress_bar"],
            is_executing=progress_info["is_running"],
            engine_name=f"Spec({engine_name})",
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

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
            callbacks = self._create_spec_callbacks(message_id, chat_id, project, engine_name=engine.engine_name)

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
