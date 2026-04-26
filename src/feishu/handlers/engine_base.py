"""Base engine handler for common engine lifecycle management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from ...tasking import TaskPriority, TaskSpec
from .base import BaseHandler

if TYPE_CHECKING:
    from ...project import ProjectContext

logger = logging.getLogger(__name__)


class BaseEngineHandler(BaseHandler):
    """
    Abstract base class for engine handlers (DeepHandler, LoopHandler).
    Provides common lifecycle management logic.
    """

    # ------------------------------------------------------------------
    # Repo-lock + conflict-card helper (DRY for scheduled closures)
    # ------------------------------------------------------------------

    def _run_with_repo_lock_or_conflict_card(
        self, root_path: str, chat_id: str, body_fn, message_id: str, command_text: str,
    ) -> None:
        """Execute *body_fn* under the repo lock, sending a conflict card on failure.

        Delegates to :meth:`LockHelper.handle_lock_conflict`.
        """
        self.lock_helper.handle_lock_conflict(
            body_fn, root_path, chat_id, message_id, command_text,
        )

    def _get_engine_manager(self):
        """Subclasses must return their specific engine manager."""
        raise NotImplementedError

    def _get_engine_name_prefix(self) -> str:
        """Subclasses must return their engine name prefix (e.g. 'Deep', 'Loop')."""
        raise NotImplementedError

    def _get_task_type(self) -> str:
        """Subclasses must return their task type (e.g. 'deep_engine', 'loop_engine')."""
        raise NotImplementedError

    def _show_status(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Subclasses must implement status display."""
        raise NotImplementedError

    def _create_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"], engine_name: str, root_path: str
    ):
        """Subclasses must implement callback creation."""
        raise NotImplementedError

    def _pause_engine_generic(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, status_paused_enum: Any = None
    ):
        """Generic pause logic."""
        root_path = project.root_path if project else self.get_working_dir(chat_id)
        manager = self._get_engine_manager()
        engine = manager.get(chat_id, root_path)

        if not engine:
            engine = manager.get_active_engine(chat_id)

        if engine and engine.is_running:
            engine.pause()
            self._show_status(message_id, chat_id, project=project)
            return

        self.reply_message(message_id, f"当前没有正在执行的 {self._get_engine_name_prefix()} 任务")

    def _resume_engine_generic(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None, status_paused_enum: Any = None
    ):
        """Generic resume logic."""
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        manager = self._get_engine_manager()
        engine = manager.get(chat_id, root_path)

        if not engine:
            paused = [e for e in manager.list_engines(chat_id) if e.project and e.project.status == status_paused_enum]
            if len(paused) == 1:
                engine = paused[0]
            elif len(paused) > 1:
                self.reply_message(
                    message_id,
                    f"⚠️ 有多个项目存在可恢复的 {self._get_engine_name_prefix()} 任务，请查看状态后切换项目再恢复",
                )
                return

        if engine and engine.project and engine.project.status == status_paused_enum:
            if project is None:
                try:
                    project = self.project_manager.find_project_by_path(engine.root_path, chat_id=chat_id)
                except Exception:
                    project = None

            callbacks = self._create_callbacks(message_id, chat_id, project, engine.engine_name, engine.root_path)

            def run_resume():
                engine.resume(callbacks)

            request_id = self.ensure_request_id(
                message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
            )
            queue_key = f"{chat_id}:{self._get_task_type()}:{project.project_id if project else root_path}"

            spec = TaskSpec(
                chat_id=chat_id,
                queue_key=queue_key,
                name=f"{self._get_task_type()}_resume",
                task_type=self._get_task_type(),
                project_id=project.project_id if project else None,
                message_id=message_id,
                origin_message_id=message_id,
                request_id=request_id,
                priority=TaskPriority.HIGH,
            )
            handle = self.scheduler.submit(spec, lambda ctx: run_resume())
            try:
                self.ctx.message_linker.link_task(message_id, handle.run_id)
            except Exception as e:
                logger.debug(
                    "link_task失败(%s_resume): message_id=%s, run_id=%s, err=%s",
                    self._get_task_type(),
                    message_id,
                    handle.run_id,
                    e,
                )
            self._show_status(message_id, chat_id, project=project)
        else:
            self.reply_message(message_id, f"当前没有可恢复的 {self._get_engine_name_prefix()} 任务")

    def _stop_engine_generic(self, message_id: str, chat_id: str, project: Optional["ProjectContext"] = None):
        """Generic stop logic."""
        if project is None:
            project = self.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.get_working_dir(chat_id)
        manager = self._get_engine_manager()
        engine = manager.get(chat_id, root_path)

        if not engine:
            running = manager.get_active_engines(chat_id)
            if len(running) == 1:
                engine = running[0]
            elif len(running) > 1:
                self.reply_message(
                    message_id, f"⚠️ 有多个项目正在执行 {self._get_engine_name_prefix()} 任务，请先切换项目再停止"
                )
                return

        if not engine or not engine.is_running:
            self.reply_message(message_id, f"📊 当前没有正在执行的 {self._get_engine_name_prefix()} 任务")
            return

        engine.stop()
        self._show_status(message_id, chat_id, project=project)

    def _safe_execute_engine(
        self,
        executor_func: callable,
        task_id: str,
        chat_id: str,
        message_id: str,
        project: Optional["ProjectContext"],
        engine_name: str,
        reporter: Any,
        request_id: Optional[str],
        action_prefix: str = "deep",
        command_text: str = "",
    ):
        """
        Execute engine logic with standardized error handling and reporting.

        Acquires the repo lock, runs *executor_func*, and on conflict renders
        a conflict card.  The ``_run_engine_body`` indirection has been inlined
        as a closure for clarity.
        """
        import asyncio

        from ...card import CardBuilder, EngineCardState
        from ...repo_lock import LockConflictError
        from ...utils.errors import get_error_detail

        root_path = getattr(project, "root_path", None) if project else None

        def _body():
            try:
                executor_func()
            except LockConflictError:
                raise
            except Exception as e:
                if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
                    logger.warning(f"{self._get_engine_name_prefix()} Engine 执行超时 (task_id={task_id}): {get_error_detail(e)}")
                else:
                    logger.error(f"{self._get_engine_name_prefix()} Engine 执行异常: {get_error_detail(e)}", exc_info=True)

                err_msg = get_error_detail(e)
                error_content = reporter.format_error(err_msg)
                error_title = reporter.get_error_title()

                ref_note = self.format_ref_note(message_id, request_id) if request_id else ""
                final_content = f"{error_content}\n\n{ref_note}" if ref_note else error_content

                err_msg_type, err_card = CardBuilder.build_engine_card(
                    project=project,
                    state=EngineCardState(
                        title=error_title,
                        content=final_content,
                        engine_name=f"{self._get_engine_name_prefix()}({engine_name})"
                        if not engine_name.startswith(self._get_engine_name_prefix())
                        else engine_name,
                        show_buttons=False,
                        action_prefix=action_prefix,
                    ),
                )
                try:
                    self.reply_message(message_id, err_card, msg_type=err_msg_type)
                except Exception as send_err:
                    logger.error(
                        "%s Engine 发送错误卡片失败: %s", self._get_engine_name_prefix(), send_err
                    )

        self.lock_helper.handle_lock_conflict(
            _body, root_path, chat_id, message_id, command_text,
        )

    def _safe_lifecycle_action(
        self,
        action_func: callable,
        action_name: str,
        chat_id: str,
        message_id: str,
        project: Optional["ProjectContext"] = None,
    ):
        """
        Execute lifecycle actions (pause, resume, stop) with safe error handling.

        Args:
            action_func: Lambda or function to execute the action
            action_name: Name of action for logging (e.g. "pause", "resume")
            chat_id: Chat ID
            message_id: Origin message ID
            project: Project context (optional)
        """
        import asyncio

        try:
            action_func()
        except Exception as e:
            engine_prefix = self._get_engine_name_prefix()
            from ...utils.errors import get_error_detail

            if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
                logger.warning(f"{engine_prefix} {action_name} 操作超时: {get_error_detail(e)}")
            else:
                logger.error(f"{engine_prefix} {action_name} 操作异常: {get_error_detail(e)}", exc_info=True)

            # Send simple error reply for lifecycle actions
            err_detail = get_error_detail(e)
            self.reply_message(message_id, f"❌ {action_name}失败: {err_detail}")
