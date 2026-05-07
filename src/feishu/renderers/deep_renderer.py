from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from ...acp import ACPEvent, ACPEventRenderer, ACPEventType
from ...card.events import CardEvent
from ...card.render.budget import RenderBudget
from ...card.state.models import CardMetadata
from ...card.ui_text import UI_TEXT
from ...deep_engine import DeepEngineCallbacks
from ...deep_engine.models import DeepProject, DeepProjectStatus
from ...project import ContextSourceMode
from ..emoji import EmojiReaction
from .base import BaseRenderer, _ACPStreamBridge, _dispatch_text_block, _StreamThrottle

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
        reporter = self.ctx.progress_reporter

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
        )
        session: Dispatchable = self.create_session(chat_id, message_id, metadata, hooks=hooks, budget=RenderBudget(engine_cmd="/deep"))
        self._current_session = session

        # ACP event renderer for structured content
        renderer = ACPEventRenderer()
        stream_bridge = _ACPStreamBridge(session)
        # Progress tracking state
        _start_time = [time.time()]
        _tool_count = [0]
        _plan_steps = [0]
        _phase = ["analyzing"]  # "analyzing" | "executing"

        def on_analyzing_done(deep_project: DeepProject):
            # Start the card session
            session.dispatch(CardEvent.started())
            content = f"🚀 ACP Deep 执行开始\n\n📂 **{deep_project.name}**\n🔗 路径: `{deep_project.root_path}`"
            _dispatch_text_block(session, "_main_text", content)
            _phase[0] = "executing"

        def on_event(event: ACPEvent):
            """Process ACP events and dispatch to CardSession."""
            renderer.process_event(event)

            # Track progress for progress_updated dispatch
            if event.event_type == ACPEventType.TOOL_CALL_START:
                _tool_count[0] += 1
                label = "🔄 执行中" if _phase[0] == "executing" else "🧠 分析/规划中"
                session.dispatch(CardEvent.progress_updated(
                    current=_tool_count[0],
                    total=max(_plan_steps[0], _tool_count[0]),
                    label=label,
                ))
            elif event.event_type == ACPEventType.PLAN_UPDATE:
                if event.plan and event.plan.entries:
                    steps = len(event.plan.entries)
                    if steps > 0:
                        _plan_steps[0] = steps
                        _phase[0] = "executing"

            # Convert ACP stream to unified programming-style card sections
            stream_bridge.on_event(event)

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
            summary = f"✅ 执行完成 · 工具调用: {tool_calls_count}"

            # Dispatch completion with summary (hooks fire automatically via CardSession)
            if deep_project.status == DeepProjectStatus.COMPLETED:
                session.dispatch(CardEvent.completed(summary=summary))
            else:
                session.dispatch(CardEvent.failed("执行未完成"))
            self._current_session = None

        def on_error(error: str):
            stream_bridge.close_open_blocks()
            # Dispatch failure (hooks fire automatically via CardSession)
            session.dispatch(CardEvent.failed(error))
            self._current_session = None

        return DeepEngineCallbacks(
            on_analyzing_done=on_analyzing_done,
            on_event=on_event,
            on_project_done=on_project_done,
            on_error=on_error,
        )

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
                label="执行中" if progress_info["is_executing"] else "已完成",
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
