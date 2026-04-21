import json
import logging
import time
import os
import asyncio
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .ws_client import FeishuWSClient
    from ..agent.intent_recognizer import IntentResult, TaskStep
    from ..project import ProjectContext

import lark_oapi as lark
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from ..agent.intent_recognizer import IntentType
from ..utils.errors import get_error_detail
from .emoji import EmojiReaction
from .message_formatter import FeishuMessageFormatter as fmt
from ..card.styles import UI_TEXT

logger = logging.getLogger(__name__)

class MessageDispatcher:
    """Handles the dispatching of user messages and intents to appropriate engines/modes."""

    def __init__(self, client: Any):
        self.client = client

    def process_with_intent(
        self,
        message_id: str,
        chat_id: str,
        text: str,
        project: Optional['ProjectContext'] = None,
        *,
        shell_fast_tracked: bool = False,
    ):
        """SMART mode routing logic."""
        from ..mode import InteractionMode

        _pid = project.project_id if project else None
        current_mode, is_in_programming = self.client._get_effective_mode(chat_id, project_id=_pid)

        # Control-plane commands: handle consistently in all modes
        if self.client._is_deep_command(text):
            self.client._add_reaction(message_id, EmojiReaction.on_smart_mode())
            self.client._add_reaction(message_id, EmojiReaction.on_processing())
            self.client._handle_deep_command(message_id, chat_id, text, project)
            return

        if self.client._is_loop_command(text):
            self.client._add_reaction(message_id, EmojiReaction.on_smart_mode())
            self.client._add_reaction(message_id, EmojiReaction.on_processing())
            self.client._handle_loop_command(message_id, chat_id, text, project)
            return

        if self.client._is_spec_command(text):
            self.client._add_reaction(message_id, EmojiReaction.on_smart_mode())
            self.client._add_reaction(message_id, EmojiReaction.on_processing())
            self.client._handle_spec_command(message_id, chat_id, text, project)
            return

        if self.client._is_interceptable_command(text):
            self.client._handle_intercepted_command(message_id, chat_id, text, project)
            return

        # Worktree mode
        if project and self.client._is_worktree_awaiting_goal(project):
            self.client._add_reaction(message_id, EmojiReaction.on_processing())
            self.client._handle_worktree_execute(message_id, chat_id, text, project)
            return

        # Programming mode (Coco / Claude / TTADK): exit or forward to active session
        if is_in_programming:
            if self.client._is_exit_command(text):
                self.client._add_reaction(message_id, EmojiReaction.on_coco_mode())
                if self.client._should_defer_exit(chat_id=chat_id, project_id=_pid):
                    self.client._request_deferred_exit(message_id=message_id, chat_id=chat_id, project_id=_pid)
                    self.client._reply_message(message_id, UI_TEXT["ws_exit_deferred_msg"])
                    return
                self.client._exit_current_mode(message_id, chat_id, project=project)
                return

            from ..thread import get_current_thread_id
            if not get_current_thread_id() and self.client.settings.thread_programming_enabled:
                pending, handler = self.client._is_one_shot_pending(chat_id, _pid, current_mode)
                if pending:
                    if not shell_fast_tracked:
                        self.client._dispatch_to_thread(message_id, chat_id, text, project, current_mode, handler)
                        return
                    is_in_programming = False

            self.client._add_reaction(message_id, EmojiReaction.on_coco_mode())
            self.client._add_reaction(message_id, EmojiReaction.on_processing())
            if current_mode == InteractionMode.COCO:
                self.client._handle_coco_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.CLAUDE:
                self.client._handle_claude_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.AIDEN:
                self.client._handle_aiden_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.CODEX:
                self.client._handle_codex_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.GEMINI:
                self.client._handle_gemini_message(message_id, chat_id, text, project)
            elif current_mode == InteractionMode.TTADK:
                self.client._ttadk_handler.handle_message(message_id, chat_id, text, project)
            else:
                self.client._show_help(message_id, chat_id)
            return

        # SMART mode: image-only messages bypass intent recognition
        with self.client._pending_image_lock:
            is_image_only = message_id in self.client._pending_image_only
        if is_image_only:
            self.client._add_reaction(message_id, EmojiReaction.on_coco_mode())
            self.client._add_reaction(message_id, EmojiReaction.on_processing())
            self.client._handle_coco_message(message_id, chat_id, text, project)
            return

        # SMART mode: intent recognition
        self.client._add_reaction(message_id, EmojiReaction.on_smart_mode())
        self.client._add_reaction(message_id, EmojiReaction.on_processing())

        try:
            intent_result = self.client._intent_recognizer.recognize(text, current_mode.value)
        except Exception as e:
            logger.error("意图识别异常: %s", get_error_detail(e))
            working_dir = self.client._get_working_dir(chat_id)
            self.client._submit_shell_command(message_id, chat_id, text, working_dir, project)
            return

        logger.info(
            "意图识别: %s (置信度: %.2f, 任务数: %d)",
            intent_result.primary_intent.value,
            intent_result.confidence,
            len(intent_result.tasks),
        )

        if intent_result.is_multi_task:
            self.execute_multi_tasks(message_id, chat_id, intent_result, project)
        else:
            self.execute_single_task(
                message_id,
                chat_id,
                intent_result.tasks[0] if intent_result.tasks else None,
                text,
                project,
                shell_fast_tracked=shell_fast_tracked,
            )

    def execute_multi_tasks(
        self, message_id: str, chat_id: str, intent_result: 'IntentResult', project: Optional['ProjectContext'] = None
    ):
        """执行多任务计划（逐步执行；遇到失败停止后续步骤）。"""
        tasks = intent_result.tasks

        task_list = [{"description": task.description or self.get_task_description(task)} for task in tasks]
        plan_msg = fmt.format_multi_task_plan(task_list)
        self.client._reply_message(message_id, plan_msg)

        self.client._add_reaction(message_id, EmojiReaction.on_multi_task_start())

        all_success = True
        for i, task in enumerate(tasks, 1):
            success = self.execute_task_step(
                message_id, chat_id, task, step_num=i, total_steps=len(tasks), project=project
            )

            if task.intent in {
                IntentType.ENTER_COCO,
                IntentType.ENTER_CLAUDE,
                IntentType.ENTER_AIDEN,
                IntentType.ENTER_CODEX,
                IntentType.ENTER_GEMINI,
                IntentType.TTADK_MESSAGE,
            }:
                break

            if not success:
                all_success = False
                self.client._reply_message(message_id, f"⚠️ 步骤 {i} 执行失败，后续步骤已取消")
                break

        if all_success:
            self.client._add_reaction(message_id, EmojiReaction.on_multi_task_done())
        else:
            self.client._add_reaction(message_id, EmojiReaction.on_error())

    def execute_single_task(
        self,
        message_id: str,
        chat_id: str,
        task: Optional['TaskStep'],
        original_text: str,
        project: Optional['ProjectContext'] = None,
        *,
        shell_fast_tracked: bool = False,
    ):
        """执行单一任务步骤（模式切换/系统命令/引擎命令/执行 shell 等）。"""
        from ..thread import get_current_thread_id
        if not task:
            if self.client.settings.thread_programming_enabled and not get_current_thread_id():
                active_thread = self.client._find_active_thread(chat_id)
                if active_thread:
                    mode_display = active_thread.mode.upper() if active_thread.mode else "编程"
                    self.client._reply_message(
                        message_id,
                        UI_TEXT["ws_active_topic_msg"].format(name=mode_display),
                    )
                    return
            self.client._reply_message(message_id, "🤔 无法理解你的意图")
            return

        intent = task.intent
        data = task.data

        if intent == IntentType.ENTER_COCO:
            try:
                project_id = project.project_id if project else None
                self.client._system_handler.handle_select_acp_tool(
                    message_id, chat_id, "coco", project_id=project_id
                )
            except Exception as e:
                logger.warning("展示 Coco 模型选择卡失败，回退直接进入: %s", get_error_detail(e))
                self.client._enter_coco_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_COCO:
            self.client._exit_coco_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_MODE:
            self.client._exit_current_mode(message_id, chat_id, project=project)

        elif intent == IntentType.CHANGE_DIR:
            path = data.get("path", "")
            self.client._change_directory(message_id, chat_id, path, project)

        elif intent == IntentType.COCO_MESSAGE:
            if data.get("command") == "info":
                self.client._show_coco_info(message_id, chat_id, project)
            else:
                self.client._handle_coco_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.ENTER_CLAUDE:
            self.client._enter_claude_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_CLAUDE:
            self.client._exit_claude_mode(message_id, chat_id, project=project)

        elif intent == IntentType.CLAUDE_MESSAGE:
            if data.get("command") == "info":
                self.client._show_claude_info(message_id, chat_id, project)
            else:
                self.client._handle_claude_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.ENTER_AIDEN:
            self.client._enter_aiden_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_AIDEN:
            self.client._exit_aiden_mode(message_id, chat_id, project=project)

        elif intent == IntentType.AIDEN_MESSAGE:
            if data.get("command") == "info":
                self.client._show_aiden_info(message_id, chat_id, project)
            else:
                self.client._handle_aiden_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.ENTER_CODEX:
            self.client._enter_codex_mode(message_id, chat_id, project=project)

        elif intent == IntentType.ENTER_GEMINI:
            self.client._enter_gemini_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_CODEX:
            self.client._exit_codex_mode(message_id, chat_id, project=project)

        elif intent == IntentType.EXIT_GEMINI:
            self.client._exit_gemini_mode(message_id, chat_id, project=project)

        elif intent == IntentType.CODEX_MESSAGE:
            if data.get("command") == "info":
                self.client._show_codex_info(message_id, chat_id, project)
            else:
                self.client._handle_codex_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.GEMINI_MESSAGE:
            if data.get("command") == "info":
                self.client._show_gemini_info(message_id, chat_id, project)
            else:
                self.client._handle_gemini_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.TTADK_MESSAGE:
            from ..mode import InteractionMode

            if data.get("command") == "info":
                self.client._show_ttadk_info(message_id, chat_id, project)
            elif str(original_text or "").strip().lower() in {"/ttadk", "/enter_ttadk"}:
                self.client._handle_ttadk_command(message_id, chat_id, project)
            else:
                _pid = project.project_id if project else None
                mode = self.client._mode_manager.get_mode(chat_id, project_id=_pid)
                if mode != InteractionMode.TTADK:
                    self.client._enter_ttadk_mode(message_id, chat_id, project=project)
                self.client._ttadk_handler.handle_message(message_id, chat_id, original_text, project)

        elif intent == IntentType.SHOW_HELP:
            self.client._show_full_help(message_id, chat_id, project)

        elif intent == IntentType.SHOW_TOOLS:
            self.client._system_handler.show_tools_list(message_id, chat_id, project)

        elif intent == IntentType.TOOLS_STATUS:
            self.client._system_handler.show_tools_status(message_id, chat_id, project)

        elif intent == IntentType.CREATE_PROJECT:
            name = data.get("name", "")
            path = data.get("path", "")
            working_dir = self.client._get_working_dir(chat_id)

            if not path:
                path = working_dir

            if not name:
                name = os.path.basename(os.path.normpath(path))
                if not name or name in (".", "/", "~"):
                    name = f"project_{int(time.time())}"

            self.client._create_project(message_id, chat_id, name, path)

        elif intent == IntentType.SWITCH_PROJECT:
            name = data.get("name", "")
            if name:
                self.client._switch_project(message_id, chat_id, name)
            else:
                self.client._show_project_board(message_id, chat_id)

        elif intent == IntentType.LIST_PROJECTS:
            self.client._show_project_board(message_id, chat_id)

        elif intent == IntentType.CLOSE_PROJECT:
            name = data.get("name", "")
            if name:
                self.client._close_project(message_id, chat_id, name)
            else:
                self.client._reply_message(message_id, "❌ 请指定要关闭的项目名称")

        elif intent == IntentType.PROJECT_STATUS:
            self.client._show_project_status(message_id, chat_id, project)

        elif intent == IntentType.ENTER_DEEP:
            requirement = data.get("requirement") or original_text
            self.client._start_deep_engine(message_id, chat_id, requirement, project)

        elif intent == IntentType.DEEP_STATUS:
            arg = (data.get("arg") or "").strip().lower()
            if arg in ("all", "-a", "--all"):
                self.client._show_deep_board(message_id, chat_id)
            else:
                self.client._show_deep_status(message_id, chat_id, project)

        elif intent == IntentType.STOP_DEEP:
            arg = (data.get("arg") or "").strip().lower()
            if arg in ("all", "-a", "--all"):
                self.client._stop_all_deep_engines(message_id, chat_id)
            else:
                self.client._stop_deep_engine(message_id, chat_id, project)

        elif intent == IntentType.DEEP_UPDATE:
            update_message = data.get("message")
            if update_message:
                self.client._update_deep_context(message_id, chat_id, update_message, project)
            else:
                self.client._reply_message(message_id, "📝 请提供上下文信息\n\n用法: `/deep_update <上下文描述>`")

        elif intent == IntentType.ENTER_LOOP:
            requirement = data.get("requirement") or original_text
            self.client._start_loop_engine(message_id, chat_id, requirement, project)

        elif intent == IntentType.LOOP_STATUS:
            self.client._show_loop_status(message_id, chat_id, project)

        elif intent == IntentType.STOP_LOOP:
            self.client._stop_loop_engine(message_id, chat_id, project)

        elif intent == IntentType.LOOP_PAUSE:
            self.client._pause_loop_engine(message_id, chat_id, project)

        elif intent == IntentType.LOOP_RESUME:
            self.client._resume_loop_engine(message_id, chat_id, project)

        elif intent == IntentType.LOOP_GUIDE:
            guide_message = data.get("message")
            if guide_message:
                self.client._update_loop_guidance(message_id, chat_id, guide_message, project)
            else:
                self.client._reply_message(message_id, "📝 请提供引导信息\n\n用法: `/loop_guide <引导描述>`")

        elif intent == IntentType.ENTER_SPEC:
            requirement = data.get("requirement") or original_text
            self.client._start_spec_engine(message_id, chat_id, requirement, project)

        elif intent == IntentType.SPEC_STATUS:
            self.client._show_spec_status(message_id, chat_id, project)

        elif intent == IntentType.STOP_SPEC:
            self.client._stop_spec_engine(message_id, chat_id, project)

        elif intent == IntentType.SPEC_PAUSE:
            self.client._pause_spec_engine(message_id, chat_id, project)

        elif intent == IntentType.SPEC_RESUME:
            self.client._resume_spec_engine(message_id, chat_id, project)

        elif intent == IntentType.SPEC_GUIDE:
            guide_message = data.get("message")
            if guide_message:
                self.client._update_spec_guidance(message_id, chat_id, guide_message, project)
            else:
                self.client._reply_message(message_id, "📝 请提供引导信息\n\n用法: `/spec_guide <引导描述>`")

        elif intent == IntentType.SHELL_COMMAND:
            working_dir = self.client._get_working_dir(chat_id)
            cmd = data.get("command") or original_text
            if shell_fast_tracked:
                # Already on shell queue — execute directly to avoid nested-task deadlock
                self.client._system_handler.execute_shell_and_reply(message_id, chat_id, cmd, working_dir, project)
            else:
                self.client._submit_shell_command(message_id, chat_id, cmd, working_dir, project)

            if project:
                project.add_conversation("user", cmd, message_id)
                self.client._context_manager.update_context(
                    project.project_id,
                    conversation={"role": "user", "content": cmd, "source_mode": "shell", "message_id": message_id},
                )

        elif intent == IntentType.UNKNOWN:
            self.client._reply_message(message_id, fmt.format_unknown_intent())

    def execute_task_step(
        self,
        message_id: str,
        chat_id: str,
        task: 'TaskStep',
        step_num: int,
        total_steps: int,
        project: Optional['ProjectContext'] = None,
    ) -> bool:
        """执行一个 TaskStep，并返回是否成功。"""
        intent = task.intent
        data = task.data
        desc = task.description or self.get_task_description(task)

        logger.info("执行步骤 %d/%d: %s", step_num, total_steps, desc)

        try:
            if intent == IntentType.ENTER_COCO:
                self.client._enter_coco_mode(message_id, chat_id, silent=True, project=project)
                self.client._reply_message(message_id, f"✅ 步骤 {step_num}: 已进入 Coco 模式")
                return True

            elif intent == IntentType.EXIT_COCO:
                success = self.client._coco_manager.end_session(chat_id)
                if success:
                    self.client._reply_message(message_id, f"✅ 步骤 {step_num}: 已退出 Coco 模式")
                return success

            elif intent == IntentType.CHANGE_DIR:
                path = data.get("path", "")
                if not path:
                    current_dir = self.client._get_working_dir(chat_id)
                    self.client._reply_message(message_id, f"✅ 步骤 {step_num}: 当前目录 {current_dir}")
                    return True

                success, result = self.client._set_working_dir(chat_id, path)
                if success:
                    self.client._reply_message(message_id, f"✅ 步骤 {step_num}: 已切换到 {result}")
                else:
                    self.client._reply_message(message_id, f"❌ 步骤 {step_num}: {result}")
                return success

            elif intent == IntentType.CREATE_PROJECT:
                name = data.get("name", "")
                path = data.get("path", "")
                if not name:
                    name = f"project_{int(time.time())}"
                if not path:
                    path = self.client._get_working_dir(chat_id)
                project_id = name.lower().replace(" ", "_").replace("-", "_")
                success, msg, new_project = self.client._project_manager.create_project(
                    project_id=project_id, project_name=name, root_path=path, chat_id=chat_id
                )
                if success:
                    self.client._reply_message(message_id, f"✅ 步骤 {step_num}: 已创建项目 {name}")
                    project = new_project
                else:
                    self.client._reply_message(message_id, f"❌ 步骤 {step_num}: {msg}")
                return success

            elif intent == IntentType.SWITCH_PROJECT:
                name = data.get("name", "")
                if name:
                    found_project = self.client._project_manager.find_project_by_name(name)
                    if found_project:
                        success, msg = self.client._project_manager.set_active_project(chat_id, found_project.project_id)
                        if success:
                            self.client._reply_message(message_id, f"✅ 步骤 {step_num}: 已切换到项目 {name}")
                        return success
                return False

            elif intent == IntentType.SHELL_COMMAND:
                cmd = data.get("command", task.description)
                if cmd:
                    working_dir = self.client._get_working_dir(chat_id)
                    self.client._system_handler.execute_shell_and_reply(message_id, chat_id, cmd, working_dir, project)
                return True
            elif intent == IntentType.TTADK_MESSAGE:
                self.client._enter_ttadk_mode(message_id, chat_id, silent=True, project=project)
                self.client._reply_message(message_id, f"✅ 步骤 {step_num}: 已进入 TTADK 模式")
                return True

            else:
                return False

        except Exception as e:
            logger.error("执行步骤 %d 异常: %s", step_num, get_error_detail(e))
            return False

    def get_task_description(self, task: 'TaskStep') -> str:
        """为 TaskStep 生成可读描述（用于多任务计划展示）。"""
        intent = task.intent
        data = task.data

        if intent == IntentType.ENTER_COCO:
            return "进入 Coco 编程模式"
        elif intent == IntentType.ENTER_CLAUDE:
            return "进入 Claude 编程模式"
        elif intent == IntentType.ENTER_AIDEN:
            return "进入 Aiden 编程模式"
        elif intent == IntentType.ENTER_CODEX:
            return "进入 Codex 编程模式"
        elif intent == IntentType.ENTER_GEMINI:
            return "进入 Gemini 编程模式"
        elif intent == IntentType.TTADK_MESSAGE:
            return "进入 TTADK 编程模式"
        elif intent == IntentType.EXIT_COCO:
            return "退出 Coco 模式"
        elif intent == IntentType.EXIT_CLAUDE:
            return "退出 Claude 模式"
        elif intent == IntentType.EXIT_AIDEN:
            return "退出 Aiden 模式"
        elif intent == IntentType.EXIT_CODEX:
            return "退出 Codex 模式"
        elif intent == IntentType.EXIT_GEMINI:
            return "退出 Gemini 模式"
        elif intent == IntentType.CHANGE_DIR:
            path = data.get("path", "")
            return f"切换到目录: {path}" if path else "查看当前目录"
        elif intent == IntentType.CREATE_PROJECT:
            name = data.get("name", "")
            return f"创建项目: {name}" if name else "创建新项目"
        elif intent == IntentType.SWITCH_PROJECT:
            name = data.get("name", "")
            return f"切换到项目: {name}" if name else "切换项目"
        elif intent == IntentType.LIST_PROJECTS:
            return "查看项目列表"
        elif intent == IntentType.CLOSE_PROJECT:
            name = data.get("name", "")
            return f"关闭项目: {name}" if name else "关闭项目"
        elif intent == IntentType.PROJECT_STATUS:
            return "查看项目状态"
        elif intent == IntentType.SHELL_COMMAND:
            return "执行命令"
        else:
            return "未知操作"

