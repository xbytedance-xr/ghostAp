"""Diagnostics handler — task board, context diff report, message trace, unified status."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ...card import CardBuilder
from ...project import ContextEntryType
from ...tasking import TaskPriority, TaskSpec
from ...utils.text import format_duration
from ...utils.errors import get_error_detail
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class DiagnosticsHandler(BaseHandler):
    """Task board, context diff reports, and message tracing."""

    # ------------------------------------------------------------------
    # Task board
    # ------------------------------------------------------------------
    def show_task_board(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        arg = ""
        try:
            parts = (text or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip().lower()
        except Exception:
            arg = ""

        try:
            mode_display = self.mode_manager.get_mode_display_name(chat_id)
        except Exception:
            mode_display = ""

        def _status_emoji(st) -> str:
            val = getattr(st, "value", st)
            return {"queued": "⏳", "running": "🔄", "succeeded": "✅", "failed": "❌", "canceled": "⛔"}.get(
                str(val), "❓"
            )

        if arg in ("all", "-a", "--all"):
            tasks = self.scheduler.list_tasks(chat_id=chat_id, include_done=False, limit=50)
            groups: dict[str, list] = {}
            for st in tasks:
                pid = st.spec.project_id or ""
                groups.setdefault(pid, []).append(st)

            lines = []
            if mode_display:
                lines.append(f"**当前模式**: {mode_display}")
                lines.append("")

            if not tasks:
                lines.append("暂无正在进行的任务")
            else:
                for pid, items in groups.items():
                    proj_name = "无项目"
                    if pid:
                        try:
                            p = self.project_manager.get_project(pid)
                            if p:
                                proj_name = p.project_name
                        except Exception:
                            pass
                    lines.append(f"### {proj_name} {f'(`{pid}`)' if pid else ''}")
                    for st in items[:10]:
                        emoji = _status_emoji(st.status)
                        pct = f" {st.progress_percent:.0f}%" if st.progress_percent is not None else ""
                        msg = f" — {st.progress_message}" if st.progress_message else ""
                        lines.append(f"- {emoji} `{st.run_id}` {st.spec.name} ({st.spec.task_type}){pct}{msg}")

            msg_type, card_content = CardBuilder.build_smart_response_card(
                project=None,
                title="📋 任务看板",
                content="\n".join(lines) if lines else "暂无任务",
                working_dir=self.get_working_dir(chat_id),
                show_buttons=True,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        # Default: current project
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        if not project:
            self.reply_message(message_id, "当前没有活跃项目，无法按项目查看任务。\n\n发送 `/projects` 查看项目看板")
            return

        tasks = self.scheduler.list_tasks(chat_id=chat_id, project_id=project.project_id, include_done=False, limit=30)
        lines = []
        if mode_display:
            lines.append(f"**当前模式**: {mode_display}")
            lines.append("")
        if not tasks:
            lines.append("暂无正在进行的任务")
        else:
            for st in tasks:
                emoji = _status_emoji(st.status)
                pct = f" {st.progress_percent:.0f}%" if st.progress_percent is not None else ""
                msg = f" — {st.progress_message}" if st.progress_message else ""
                lines.append(f"- {emoji} `{st.run_id}` {st.spec.name} ({st.spec.task_type}){pct}{msg}")

        msg_type, card_content = CardBuilder.build_project_response_card(
            project,
            "📋 任务看板",
            "\n".join(lines),
            show_buttons=True,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    # ------------------------------------------------------------------
    # Unified status — /status [task_id|all]
    # ------------------------------------------------------------------
    def show_unified_status(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        """Show unified status across all engine types (Deep/Loop/Spec).

        - /status          → list running/paused engine tasks for current chat
        - /status all      → include completed tasks
        - /status <task_id> → detailed status for a specific task
        """
        arg = ""
        try:
            parts = (text or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip()
        except Exception:
            arg = ""

        # /status <task_id> — specific task lookup
        if arg and arg.lower() not in ("all", "-a", "--all"):
            self._show_task_detail(message_id, chat_id, arg, project)
            return

        include_done = arg.lower() in ("all", "-a", "--all") if arg else False

        lines: list[str] = []

        # Collect engines across all three types
        entries: list[
            tuple[str, str, str, str, str, Optional[float]]
        ] = []  # (mode, task_id, name, status, info, started_at)

        # Deep engines
        for engine in self.ctx.deep_engine_manager.list_engines(chat_id):
            if not engine.project:
                continue
            p = engine.project
            status_val = p.status.value
            if not include_done and status_val in ("completed", "failed"):
                continue
            tid = p.task_id or ""
            dur = format_duration(p.duration()) if p.duration() else ""
            info = f"{dur}" if dur else status_val
            entries.append(("Deep", tid, p.name, status_val, info, p.started_at))

        # Loop engines
        for engine in self.ctx.loop_engine_manager.list_engines(chat_id):
            if not engine.project:
                continue
            p = engine.project
            status_val = p.status.value
            if not include_done and status_val in ("completed", "aborted"):
                continue
            tid = p.task_id or ""
            dur = format_duration(p.duration()) if p.duration() else ""
            criteria = f"{p.satisfied_count}/{p.total_criteria}" if p.total_criteria else ""
            parts_list = [f"迭代{p.current_iteration}"]
            if criteria:
                parts_list.append(f"标准{criteria}")
            if dur:
                parts_list.append(dur)
            info = " · ".join(parts_list) if parts_list else status_val
            entries.append(("Loop", tid, p.name, status_val, info, p.started_at))

        # Spec engines
        for engine in self.ctx.spec_engine_manager.list_engines(chat_id):
            if not engine.project:
                continue
            p = engine.project
            status_val = p.status.value
            if not include_done and status_val in ("completed", "aborted"):
                continue
            tid = p.task_id or ""
            dur = format_duration(p.duration()) if p.duration() else ""
            criteria = f"{p.satisfied_count}/{p.total_criteria}" if p.total_criteria else ""
            phase = ""
            if p.current_cycle:
                phase = p.current_cycle.phase.display_name
            parts_list = [f"循环{p.current_cycle_number}"]
            if phase:
                parts_list.append(phase)
            if criteria:
                parts_list.append(f"标准{criteria}")
            if dur:
                parts_list.append(dur)
            info = " · ".join(parts_list) if parts_list else status_val
            entries.append(("Spec", tid, p.name, status_val, info, p.started_at))

        if not entries:
            proj_name = project.project_name if project else ""
            content = "当前没有 Deep/Loop/Spec 引擎任务\n\n"
            if proj_name:
                content += f"📂 当前项目: **{proj_name}**\n\n"
            content += (
                "启动任务:\n• `/deep <需求>` — 单次深度执行\n• `/loop <需求>` — 迭代闭环\n• `/spec <需求>` — 结构化开发"
            )
            msg_type, card_content = CardBuilder.build_smart_response_card(
                project=project,
                title="📊 统一状态",
                content=content,
                working_dir=self.get_working_dir(chat_id),
                show_buttons=True,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        # Sort: running first, then by start time descending
        def _sort_key(e):
            _, _, _, status_val, _, started_at = e
            running = 0 if status_val in ("executing", "running", "planning", "analyzing") else 1
            return (running, -(started_at or 0))

        entries.sort(key=_sort_key)

        status_emoji_map = {
            "idle": "⏳",
            "planning": "🧠",
            "executing": "🔄",
            "running": "🔄",
            "analyzing": "🧠",
            "paused": "⏸️",
            "completed": "✅",
            "failed": "❌",
            "aborted": "⚠️",
            "clarifying": "❓",
        }

        lines.append(f"**引擎任务 ({len(entries)})**\n")
        for mode, tid, name, status_val, info, _ in entries:
            emoji = status_emoji_map.get(status_val, "❓")
            tid_short = f" `{tid[-12:]}`" if tid else ""
            lines.append(f"- {emoji} **{mode}** · {name}{tid_short} · {info}")

        if not include_done:
            lines.append("\n_发送 `/status all` 查看包括已完成的任务_")

        msg_type, card_content = CardBuilder.build_smart_response_card(
            project=project,
            title="📊 统一状态",
            content="\n".join(lines),
            working_dir=self.get_working_dir(chat_id),
            show_buttons=True,
        )
        self.reply_message(message_id, card_content, msg_type=msg_type)

    def _show_task_detail(
        self, message_id: str, chat_id: str, task_id: str, project: Optional["ProjectContext"] = None
    ):
        """Show detailed status for a specific task by task_id."""
        # Search across all engine managers
        for engine in self.ctx.deep_engine_manager.list_engines(chat_id):
            if (
                engine.project
                and engine.project.task_id
                and (engine.project.task_id == task_id or task_id in engine.project.task_id)
            ):
                content = self.ctx.progress_reporter.format_status(engine.project)
                title = "📊 Deep 任务详情"
                engine_name = engine.engine_name
                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    title=title,
                    content=content,
                    engine_name=engine_name,
                    show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        for engine in self.ctx.loop_engine_manager.list_engines(chat_id):
            if (
                engine.project
                and engine.project.task_id
                and (engine.project.task_id == task_id or task_id in engine.project.task_id)
            ):
                content = self.ctx.loop_reporter.format_status(engine.project)
                title = "📊 Loop 任务详情"
                engine_name = engine.engine_name
                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    title=title,
                    content=content,
                    engine_name=f"Loop({engine_name})",
                    show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        for engine in self.ctx.spec_engine_manager.list_engines(chat_id):
            if (
                engine.project
                and engine.project.task_id
                and (engine.project.task_id == task_id or task_id in engine.project.task_id)
            ):
                content = self.ctx.spec_reporter.format_status(engine.project)
                title = "📊 Spec 任务详情"
                engine_name = engine.engine_name
                msg_type, card_content = CardBuilder.build_engine_card(
                    project=project,
                    title=title,
                    content=content,
                    engine_name=f"Spec({engine_name})",
                    show_buttons=False,
                )
                self.reply_message(message_id, card_content, msg_type=msg_type)
                return

        # Also check scheduler by task_id
        state = self.scheduler.get_state_by_task_id(task_id)
        if state:
            status_emoji = {"queued": "⏳", "running": "🔄", "succeeded": "✅", "failed": "❌", "canceled": "⛔"}.get(
                str(getattr(state.status, "value", state.status)), "❓"
            )
            lines = [
                f"**任务**: {state.spec.name}",
                f"**类型**: {state.spec.task_type}",
                f"**状态**: {status_emoji} {state.status.value if hasattr(state.status, 'value') else state.status}",
                f"**run_id**: `{state.run_id}`",
            ]
            if state.spec.task_id:
                lines.append(f"**task_id**: `{state.spec.task_id}`")
            if state.progress_message:
                lines.append(f"**进度**: {state.progress_message}")
            msg_type, card_content = CardBuilder.build_smart_response_card(
                project=project,
                title="📊 任务详情",
                content="\n".join(lines),
                working_dir=self.get_working_dir(chat_id),
                show_buttons=False,
            )
            self.reply_message(message_id, card_content, msg_type=msg_type)
            return

        self.reply_message(message_id, f"未找到 task_id: `{task_id}`\n\n发送 `/status` 查看所有任务")

    # ------------------------------------------------------------------
    # Context diff
    # ------------------------------------------------------------------
    def show_context_diff(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        self._submit_diff_report(message_id, chat_id, text, project)

    def _build_context_diff_report(
        self, chat_id: str, text: str, project: "ProjectContext"
    ) -> tuple[bool, str, Optional[str]]:
        ctx = self.context_manager.store.get(project.project_id)
        if not ctx:
            return False, "", "该项目暂无上下文记录，无法生成 Diff 报告。"

        versions = list(ctx.versions)
        arg = ""
        try:
            parts = (text or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip()
        except Exception:
            arg = ""

        arg_lower = arg.lower().strip()

        def _parse_vnum(s: str) -> Optional[int]:
            s = (s or "").strip().lower()
            if s.startswith("v"):
                s = s[1:]
            if not s.isdigit():
                return None
            try:
                return int(s)
            except Exception:
                return None

        from_vnum: Optional[int] = None
        to_vnum: Optional[int] = None
        show_current = False

        if arg_lower in ("", "last"):
            if len(versions) >= 2:
                from_vnum = versions[-2].version_number
                to_vnum = versions[-1].version_number
            elif len(versions) == 1:
                from_vnum = versions[-1].version_number
                show_current = True
            else:
                return (
                    False,
                    "",
                    "该项目尚无版本书签。\n\n提示：版本书签会在模式切换、项目切换、Deep 完成等关键节点自动创建。",
                )
        elif arg_lower in ("current", "now"):
            if not versions:
                return False, "", "该项目尚无版本书签，无法计算 `current` diff。"
            from_vnum = versions[-1].version_number
            show_current = True
        elif ".." in arg_lower:
            a, b = arg_lower.split("..", 1)
            from_vnum = _parse_vnum(a)
            to_vnum = _parse_vnum(b)
            if from_vnum is None or to_vnum is None:
                return False, "", "用法错误：`/diff <A>..<B>`，例如 `/diff 3..5`。"
        else:
            v = _parse_vnum(arg_lower)
            if v is None:
                return False, "", "用法：`/diff [last|current|N|A..B]`，例如 `/diff current` 或 `/diff 2..3`。"
            from_vnum = v
            show_current = True

        from_v = ctx.get_version(from_vnum) if from_vnum is not None else None
        to_v = ctx.get_version(to_vnum) if (to_vnum is not None) else None
        if not from_v:
            return False, "", f"找不到版本 v{from_vnum}，当前共有 {len(versions)} 个版本。"
        if to_vnum is not None and not to_v:
            return False, "", f"找不到版本 v{to_vnum}，当前共有 {len(versions)} 个版本。"

        def _entry_seq(e) -> int:
            try:
                return int(getattr(e, "seq", 0) or 0)
            except Exception:
                return 0

        start_seq = int(getattr(from_v, "last_seq", 0) or 0)
        end_seq = int(getattr(to_v, "last_seq", 0) or 0) if to_v else None

        entries = []
        if start_seq > 0:
            for e in ctx.entries:
                s = _entry_seq(e)
                if s <= start_seq:
                    continue
                if end_seq is not None and s > end_seq:
                    continue
                entries.append(e)
        else:
            start_idx = from_v.entry_count
            all_entries = ctx.entries
            if to_v is not None:
                end_idx = min(len(all_entries), to_v.entry_count)
                start_idx = min(max(start_idx, 0), end_idx)
                entries = list(all_entries[start_idx:end_idx])
            else:
                start_idx = min(max(start_idx, 0), len(all_entries))
                entries = list(all_entries[start_idx:])

        def _short(s: str, n: int = 180) -> str:
            s = (s or "").replace("\r", " ").replace("\n", " ")
            return s if len(s) <= n else (s[: n - 1] + "…")

        header_lines = ["## 🧾 Diff 报告"]
        header_lines.append(f"**项目**: {project.project_name} (`{project.project_id}`)")
        if show_current:
            header_lines.append(f"**范围**: v{from_v.version_number} → 当前")
        elif to_v:
            header_lines.append(f"**范围**: v{from_v.version_number} → v{to_v.version_number}")
        header_lines.append(f"**起点原因**: {_short(from_v.reason, 120)}")
        if to_v:
            header_lines.append(f"**终点原因**: {_short(to_v.reason, 120)}")
        header_lines.append(f"**新增条目**: {len(entries)}")

        file_changes, mode_changes, deep_results, summaries, conversations, others = [], [], [], [], [], []
        for e in entries:
            et = getattr(e, "entry_type", None)
            sm = getattr(e, "source_mode", None)
            et_val = getattr(et, "value", str(et))
            sm_val = getattr(sm, "value", str(sm))
            if et == ContextEntryType.FILE_CHANGE:
                file_changes.append(e)
            elif et == ContextEntryType.MODE_TRANSITION:
                mode_changes.append(e)
            elif et == ContextEntryType.DEEP_ENGINE_RESULT:
                deep_results.append(e)
            elif et == ContextEntryType.AI_SUMMARY:
                summaries.append(e)
            elif et == ContextEntryType.CONVERSATION:
                conversations.append(e)
            else:
                others.append((et_val, sm_val, e))

        lines = list(header_lines)
        if not entries:
            lines.append("")
            lines.append("✅ 本范围内没有新增上下文条目")
        else:
            if file_changes:
                lines.append("")
                uniq, seen = [], set()
                for e in file_changes:
                    p = (e.content or "").strip()
                    if not p or p in seen:
                        continue
                    seen.add(p)
                    uniq.append(p)
                lines.append(f"### 📝 文件变更 ({len(uniq)})")
                for p in uniq[:20]:
                    lines.append(f"- `{p}`")
            if deep_results:
                lines.append("")
                lines.append(f"### 🧠 Deep 结果 ({len(deep_results)})")
                for e in deep_results[:5]:
                    name = (e.metadata or {}).get("name") or "unknown"
                    tasks = (e.metadata or {}).get("tasks") or []
                    done = sum(1 for t in tasks if isinstance(t, dict) and t.get("status") == "completed")
                    lines.append(f"- `{name}`：已完成 {done}/{len(tasks)} 个任务")
            if mode_changes:
                lines.append("")
                lines.append(f"### 🔄 模式切换 ({len(mode_changes)})")
                for e in mode_changes[-10:]:
                    reason = (e.metadata or {}).get("reason", "")
                    lines.append(f"- {_short(e.content, 120)}{f'（{_short(reason, 80)}）' if reason else ''}")
            if summaries:
                lines.append("")
                lines.append(f"### 📌 AI 摘要 ({len(summaries)})")
                for e in summaries[-5:]:
                    lines.append(f"- {_short(e.content, 200)}")
            if conversations:
                lines.append("")
                lines.append(f"### 💬 对话片段 ({len(conversations)})")
                for e in conversations[-8:]:
                    role = (e.metadata or {}).get("role", "?")
                    lines.append(f"- `{role}`: {_short(e.content, 160)}")
            if others:
                lines.append("")
                lines.append(f"### 📎 其他 ({len(others)})")
                for et_val, sm_val, e in others[:10]:
                    lines.append(f"- `{et_val}`/`{sm_val}`: {_short(e.content, 160)}")

        content = "\n".join(lines)
        if len(content) > 7000:
            content = content[:7000] + "\n…（内容过长已截断）"
        return True, content, None

    def _submit_diff_report(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        if project is None:
            project = self.project_manager.get_active_project(chat_id)
        if not project:
            self.reply_message(message_id, "当前没有活跃项目，无法生成 Diff 报告。\n\n发送 `/projects` 选择项目")
            return

        request_id = self.ensure_request_id(message_id, chat_id=chat_id, project_id=project.project_id)
        streaming_manager = self.get_streaming_manager()
        ref_note = self.format_ref_note(message_id, request_id)
        initial = f"🧾 正在生成 Diff 报告...\n\n{ref_note}" if ref_note else "🧾 正在生成 Diff 报告..."

        card = streaming_manager.create_streaming_card(
            chat_id=chat_id,
            project_name=project.project_name,
            project_path=project.root_path,
            project_id=project.project_id,
            initial_content=initial,
            is_coco_mode=False,
            is_claude_mode=False,
            reply_to_message_id=message_id,
        )
        card_message_id = streaming_manager.send_streaming_card(card) if card else None
        if card_message_id:
            try:
                self.register_message_project(card_message_id, project)
            except Exception:
                pass
            try:
                self.ctx.message_linker.register_origin(
                    message_id, request_id=request_id, chat_id=chat_id, project_id=project.project_id
                )
                self.ctx.message_linker.link_reply(message_id, card_message_id)
            except Exception:
                pass

        spec = TaskSpec(
            chat_id=chat_id,
            queue_key=f"{chat_id}:diff:{project.project_id}",
            name="diff_report",
            task_type="diff_report",
            project_id=project.project_id,
            message_id=message_id,
            origin_message_id=message_id,
            request_id=request_id,
            priority=TaskPriority.NORMAL,
        )

        def _run(task_ctx):
            try:
                try:
                    full_ref = self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)
                    if card and card_message_id and full_ref:
                        streaming_manager.update_content(card, f"🧾 正在生成 Diff 报告...\n\n{full_ref}")
                except Exception:
                    pass

                task_ctx.progress("解析参数", 5)
                if card and card_message_id:
                    try:
                        streaming_manager.update_content(
                            card,
                            f"🧾 解析参数中（5%）...\n\n{self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)}",
                        )
                    except Exception:
                        pass

                ok, content, err = self._build_context_diff_report(chat_id, text, project)
                if not ok:
                    msg = err or "生成 Diff 报告失败"
                    final_ref = self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)
                    final = f"❌ {msg}\n\n{final_ref}" if final_ref else f"❌ {msg}"
                    if card and card_message_id:
                        streaming_manager.close_streaming(card, final_content=final)
                    else:
                        self.reply_message(message_id, msg, origin_message_id=message_id, request_id=request_id)
                    return

                task_ctx.progress("生成报告", 80)
                if card and card_message_id:
                    try:
                        streaming_manager.update_content(
                            card,
                            f"🧾 生成报告中（80%）...\n\n{self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)}",
                        )
                    except Exception:
                        pass

                final_ref = self.format_ref_note(message_id, request_id, run_id=task_ctx.run_id)
                final = f"{content}\n\n{final_ref}" if final_ref and final_ref not in content else content
                if card and card_message_id:
                    streaming_manager.close_streaming(card, final_content=final)
                else:
                    msg_type, card_content = CardBuilder.build_project_response_card(
                        project,
                        "🧾 Diff 报告",
                        final,
                        show_buttons=False,
                        footer="用法：`/diff`（最近两版） • `/diff current`（到当前） • `/diff N` • `/diff A..B`",
                    )
                    rid = self.reply_message_with_id(
                        message_id, card_content, msg_type, origin_message_id=message_id, request_id=request_id
                    )
                    if rid:
                        self.register_message_project(rid, project)
                task_ctx.progress("完成", 100)
            except Exception as e:
                msg = f"Diff 报告生成异常: {get_error_detail(e)}"
                final_ref = self.format_ref_note(message_id, request_id, run_id=getattr(task_ctx, "run_id", None))
                final = f"❌ {msg}\n\n{final_ref}" if final_ref else f"❌ {msg}"
                try:
                    if card and card_message_id:
                        streaming_manager.close_streaming(card, final_content=final)
                except Exception:
                    pass
                self.reply_message(message_id, msg, origin_message_id=message_id, request_id=request_id)

        handle = self.scheduler.submit(spec, _run)
        try:
            self.ctx.message_linker.link_task(message_id, handle.run_id)
        except Exception:
            pass

        if card and card_message_id:
            try:
                full_ref = self.format_ref_note(message_id, request_id, run_id=handle.run_id)
                streaming_manager.update_content(card, f"🧾 已开始生成 Diff 报告...\n\n{full_ref}")
            except Exception:
                pass
        return handle

    # ------------------------------------------------------------------
    # Message trace
    # ------------------------------------------------------------------
    def show_message_trace(self, message_id: str, chat_id: str, text: str, project: Optional["ProjectContext"] = None):
        arg = ""
        try:
            parts = (text or "").strip().split(None, 1)
            if len(parts) > 1:
                arg = parts[1].strip()
        except Exception:
            arg = ""

        key = arg or message_id
        data = None
        try:
            data = self.ctx.message_linker.query(key)
        except Exception:
            data = None

        if not data:
            self.reply_message(message_id, f"未找到关联信息：`{key}`")
            return

        origin = data.get("origin_message_id")
        req = data.get("request_id")
        proj_id = data.get("project_id")

        if project is None and proj_id:
            try:
                project = self.project_manager.get_project(proj_id)
            except Exception:
                project = None

        lines = []
        lines.append(f"**origin_message_id**: `{origin}`")
        if req:
            lines.append(f"**request_id**: `{req}`")
        if data.get("chat_id"):
            lines.append(f"**chat_id**: `{data.get('chat_id')}`")
        if proj_id:
            lines.append(f"**project_id**: `{proj_id}`")

        replies = data.get("reply_message_ids") or []
        runs = data.get("task_run_ids") or []

        lines.append("")
        lines.append(f"### 📨 回复消息 ({len(replies)})")
        for mid in replies[-10:]:
            lines.append(f"- `{mid}`")
        lines.append("")
        lines.append(f"### 🧵 任务 run_id ({len(runs)})")
        for rid in runs[-10:]:
            lines.append(f"- `{rid}`")

        footer = "提示：`/trace <id>` 支持 origin/reply/run_id/request_id"
        if project:
            msg_type, card_content = CardBuilder.build_project_response_card(
                project,
                "🔎 关联查询",
                "\n".join(lines),
                show_buttons=False,
                footer=footer,
            )
        else:
            msg_type, card_content = CardBuilder.build_smart_response_card(
                project=None,
                title="🔎 关联查询",
                content="\n".join(lines),
                working_dir=self.get_working_dir(chat_id),
                show_buttons=False,
            )
        self.reply_message(message_id, card_content, msg_type=msg_type)
