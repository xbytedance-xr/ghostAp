from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from ...acp import ACPEvent, ACPEventRenderer, ACPEventType
from ...card.events import CardEvent, CardEventType
from ...card.orchestrator import TaskOrchestrator
from ...card.render.budget import RenderBudget
from ...card.state.models import CardMetadata
from ...card.stream_bridge import ACPStreamBridge
from ...card.task_registry import TaskRegistry, tasks_from_plan_entries
from ...card.ui_text import UI_TEXT
from ...deep_engine import DeepEngineCallbacks
from ...deep_engine.models import DeepProject, DeepProjectStatus
from ...project import ContextSourceMode
from ..emoji import EmojiReaction
from .base import BaseRenderer, _dispatch_text_block, _StreamThrottle

if TYPE_CHECKING:
    from ...card.protocols import Dispatchable
    from ...card.session import CardSession
    from ...project import ProjectContext
    from ..handlers.deep import DeepHandler

logger = logging.getLogger(__name__)

class DeepRenderer(BaseRenderer):
    """
    Handles UI rendering and state management for Deep Engine interactions.
    Separated from DeepHandler to improve maintainability.
    """

    def __init__(self, handler: "DeepHandler") -> None:
        super().__init__(handler)
        self._current_session: "Dispatchable | None" = None

    def get_active_session(self) -> "Dispatchable | None":
        """Return the currently active deep engine session."""
        return self._current_session

    def create_deep_callbacks(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"],
        engine_name: str = "Coco",
        root_path: Optional[str] = None,
        initial_message_id: Optional[str] = None,
    ) -> DeepEngineCallbacks:
        request_id = self.handler.ensure_request_id(
            message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
        )
        # Build lifecycle hooks for side effects (emoji, context persistence)
        context_update_fn = None
        if project:
            def context_update_fn(state):
                """Persist deep engine results to project context."""
                self.handler.context_manager.update_context(
                    project.project_id,
                    deep_result={"data": {"completed": True}},
                    chat_id=chat_id,
                )
                ctx = self.handler.context_manager.store.get(project.project_id, chat_id=chat_id)
                if ctx:
                    ctx.create_version(
                        reason="deep_engine_done",
                        source_mode=ContextSourceMode.DEEP_ENGINE,
                        summary="Deep Engine completed",
                    )

        hooks = self._build_hooks(
            message_id,
            include_context_hook=True,
            context_update_fn=context_update_fn,
            chat_id=chat_id,
            engine_type="deep",
        )

        # New pipeline: CardSession (event-driven)
        metadata = CardMetadata(
            project_name=project.project_id if project else None,
            mode_name="Deep",
            mode_emoji="🧠",
            engine_type="deep",
            tool_name=engine_name,
            working_dir=project.root_path if project else None,
        )
        _deep_budget = RenderBudget(engine_cmd="/deep")

        # Create thinking-phase session (the first card)
        session: Dispatchable = self.create_session(chat_id, message_id, metadata, hooks=hooks, budget=_deep_budget)
        self._current_session = session

        # ACP event renderer for structured content
        renderer = ACPEventRenderer()
        stream_bridge = ACPStreamBridge(session)
        task_registry = TaskRegistry()
        # Progress tracking state
        _start_time = [time.time()]
        _tool_count = [0]
        _plan_steps = [0]
        _phase = ["analyzing"]  # "analyzing" | "executing"
        _current_task_id = [""]

        def _task_payload() -> list[dict]:
            return [
                {"task_id": s.task_id, "name": s.name, "status": s.status}
                for s in task_registry.get_snapshot()
            ]

        def _pick_current_task_id(preferred: str = "") -> str:
            snapshot = task_registry.get_snapshot()
            if preferred:
                for item in snapshot:
                    if item.task_id == preferred and item.status == "in_progress":
                        return preferred
            for item in reversed(snapshot):
                if item.status == "in_progress":
                    return item.task_id
            return ""

        def _dispatch_task_list(preferred_current_id: str = "") -> None:
            tasks = _task_payload()
            if not tasks:
                return
            current = _pick_current_task_id(preferred_current_id or _current_task_id[0])
            _current_task_id[0] = current
            session.dispatch(CardEvent(
                type=CardEventType.TASK_LIST_UPDATED,
                payload={"tasks": tasks, "current_task_id": current},
            ))

        def _upsert_task(task_id: str, name: str, status: str) -> None:
            task_id = str(task_id or "").strip()
            name = str(name or "").strip()
            if not task_id:
                return
            if status not in {"pending", "in_progress", "completed", "failed"}:
                status = "pending"

            existing = task_registry.get(task_id)
            if existing is None:
                task_registry.register(task_id=task_id, name=name or "子任务", status=status)
                return

            if (
                name
                and TaskOrchestrator._is_generic_task_label(existing.name)
                and not TaskOrchestrator._is_generic_task_label(name)
            ):
                task_registry.update_name(task_id, name)
            task_registry.update_status(task_id, status)

        def _handle_plan_task_list(event: ACPEvent) -> bool:
            if event.event_type != ACPEventType.PLAN_UPDATE or not event.plan:
                return False
            tasks = tasks_from_plan_entries(event.plan.entries)
            if not tasks:
                return False
            current = ""
            for task in tasks:
                task_id = str(task.get("task_id") or "")
                status = str(task.get("status") or "pending")
                _upsert_task(task_id, str(task.get("name") or ""), status)
                if not current and status == "in_progress":
                    current = task_id
            _dispatch_task_list(current)
            return True

        def _handle_agent_task_list(event: ACPEvent) -> bool:
            if event.event_type not in {
                ACPEventType.TOOL_CALL_START,
                ACPEventType.TOOL_CALL_UPDATE,
                ACPEventType.TOOL_CALL_DONE,
            }:
                return False
            is_agent_task_event = TaskOrchestrator.is_agent_task_event(event)
            tool_call = event.tool_call
            task_id = str(getattr(tool_call, "id", "") or "").strip()
            if not task_id:
                return False
            if not is_agent_task_event and task_registry.get(task_id) is None:
                return False
            if event.event_type == ACPEventType.TOOL_CALL_DONE:
                raw_status = str(getattr(tool_call, "status", "") or "").strip().lower()
                status = "failed" if raw_status == "failed" else "completed"
            else:
                status = "in_progress"
            _upsert_task(task_id, TaskOrchestrator._extract_agent_task_label(tool_call), status)
            _dispatch_task_list(task_id)
            return True

        def on_analyzing_done(deep_project: DeepProject):
            # Start the card session
            session.dispatch(CardEvent.started())
            content = UI_TEXT["deep_exec_start"].format(project_name=deep_project.name, root_path=deep_project.root_path)
            _dispatch_text_block(session, "_main_text", content)
            _phase[0] = "executing"

        def on_event(event: ACPEvent):
            """Process ACP events and dispatch them to the main Deep CardSession."""
            renderer.process_event(event)

            # Track progress for progress_updated dispatch
            if event.event_type == ACPEventType.TOOL_CALL_START:
                _tool_count[0] += 1

            # Detect PLAN_UPDATE and render the shared task-list component on
            # the main Deep card. Deep intentionally stays single-card because
            # Feishu cannot reliably keep multiple live cards updated in sync.
            if event.event_type == ACPEventType.PLAN_UPDATE:
                if event.plan and event.plan.entries:
                    steps = len(event.plan.entries)
                    if steps > 0:
                        _plan_steps[0] = steps
                        _phase[0] = "executing"

                _handle_plan_task_list(event)
            else:
                if event.event_type != ACPEventType.TOOL_CALL_DONE:
                    _handle_agent_task_list(event)
                stream_bridge.on_event(event)
                if event.event_type == ACPEventType.TOOL_CALL_DONE:
                    _handle_agent_task_list(event)

            # Dispatch progress on the main Deep card.
            if event.event_type == ACPEventType.TOOL_CALL_START:
                label = UI_TEXT["deep_phase_executing"] if _phase[0] == "executing" else UI_TEXT["deep_phase_planning"]
                progress_event = CardEvent.progress_updated(
                    current=_tool_count[0],
                    total=max(_plan_steps[0], _tool_count[0]),
                    label=label,
                )
                session.dispatch(progress_event)

            # Check for warning banner based on elapsed time
            elapsed = time.time() - _start_time[0]
            warning = self.check_warning_banner(elapsed, is_executing=(_phase[0] == "executing"))
            if warning:
                session.dispatch(CardEvent.warning_updated(warning))

        def on_project_done(deep_project: DeepProject):
            stream_bridge.close_open_blocks()
            # Build execution summary
            snap = self._get_engine(chat_id, root_path, project)
            tool_calls_count = snap.tool_calls_count if snap else _tool_count[0]
            summary = UI_TEXT["deep_exec_completed"].format(tool_calls_count=tool_calls_count)

            if deep_project.status == DeepProjectStatus.COMPLETED:
                session.dispatch(CardEvent.completed(summary=summary))
            else:
                session.dispatch(CardEvent.failed(UI_TEXT["deep_exec_incomplete"]))
            self._current_session = None

        def on_error(error: str):
            stream_bridge.close_open_blocks()
            session.dispatch(CardEvent.failed(error))
            self._current_session = None

        return DeepEngineCallbacks(
            on_analyzing_done=on_analyzing_done,
            on_event=on_event,
            on_project_done=on_project_done,
            on_error=on_error,
        )

    def _on_card_split_completed(self, reason: str, hint: str | None, bridge_phrase: str | None = None) -> None:
        self._pending_split_hint = hint

    def _get_engine(self, chat_id: str, root_path: Optional[str], project: Optional["ProjectContext"]):
        """Helper to get the deep engine instance via snapshot interface."""
        rp = root_path or (project.root_path if project else "")
        if rp:
            snap = self.ctx.deep_engine_manager.snapshot(chat_id, rp)
            if snap:
                return snap
        try:
            snaps = self.ctx.deep_engine_manager.snapshot_active(chat_id)
            if len(snaps) == 1:
                return snaps[0]
        except Exception:
            logger.debug("failed to get running engine snapshot", exc_info=True)
        return None

    def render_deep_status(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
    ):
        if project is None:
            project = self.handler.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.handler.get_working_dir(chat_id)
        snap = self.ctx.deep_engine_manager.snapshot(chat_id, root_path)
        reporter = self.ctx.progress_reporter

        if not snap or not snap.ext.get("project"):
            snaps = self.ctx.deep_engine_manager.snapshot_active(chat_id)
            if len(snaps) == 1 and snaps[0].ext.get("project"):
                snap = snaps[0]
            elif len(snaps) > 1:
                self.handler.show_deep_board(message_id, chat_id)
                return
            else:
                # No active engine — build a simple status card via CardSession snapshot
                engine_name = self.handler.get_engine_name(
                    chat_id, project_id=(project.project_id if project else None)
                )
                metadata = CardMetadata(
                    project_name=project.project_id if project else None,
                    mode_name="Deep",
                    mode_emoji="🧠",
                    engine_type="deep",
                    tool_name=engine_name,
                    working_dir=project.root_path if project else None,
                )
                session = self.create_session(chat_id, message_id, metadata, budget=RenderBudget(engine_cmd="/deep"))
                session.dispatch(CardEvent.started())
                session.dispatch(CardEvent.text_started("_status"))
                session.dispatch(CardEvent.text_delta("_status", UI_TEXT["deep_status_empty"]))
                session.dispatch(CardEvent.text_done("_status"))
                session.dispatch(CardEvent.completed())
                # Card was auto-delivered by the session
                return

        engine_name = snap.engine_name
        deep_project = snap.ext.get("project")
        deep_progress = snap.ext.get("progress")

        if project is None:
            try:
                project = self.handler.project_manager.find_project_by_path(snap.root_path, chat_id=chat_id)
            except Exception:
                project = None

        status_content = reporter.format_status(deep_project)
        status_title = reporter.get_status_title()
        progress_info = reporter.get_progress_info(
            deep_project,
            completed=snap.completed_steps,
            total=snap.total_steps,
        )

        deep_project_id = progress_info["project_id"]
        state = self.get_ui_state(deep_project_id) if deep_project_id else self.get_default_ui_state()

        # Build status card via CardSession
        metadata = CardMetadata(
            project_name=project.project_id if project else None,
            mode_name="Deep",
            mode_emoji="🧠",
            engine_type="deep",
            tool_name=engine_name,
            working_dir=project.root_path if project else None,
        )
        session = self.create_session(chat_id, message_id, metadata, budget=RenderBudget(engine_cmd="/deep"))
        session.dispatch(CardEvent.started())

        # Apply collapsing to status content
        status_content = self._render_collapsible_section(
            status_content, total_items=len(status_content.split("\n")), expanded=state.get("expand_ac", False)
        )

        # Dispatch content
        session.dispatch(CardEvent.text_delta("_status", f"**{status_title}**\n\n{status_content}"))

        # Progress
        if progress_info.get("progress_bar"):
            session.dispatch(CardEvent.progress_updated(
                current=progress_info.get("completed", 0),
                total=progress_info.get("total", 0),
                label=UI_TEXT["deep_progress_executing"] if progress_info["is_executing"] else UI_TEXT["deep_progress_done"],
            ))

        # Warning banner
        warning_banner = self.check_warning_banner(
            snap.duration_seconds,
            is_executing=progress_info["is_executing"],
        )
        if warning_banner:
            session.dispatch(CardEvent.warning_updated(warning_banner))

        # Terminal state
        if not progress_info["is_executing"]:
            session.dispatch(CardEvent.completed())
        # If still executing, leave session open (card delivered via dispatch)
