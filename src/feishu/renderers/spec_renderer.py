from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from ...card.events import CardEvent
from ...card.render.budget import RenderBudget
from ...card.state.models import CardMetadata
from ...card.ui_text import UI_TEXT
from ...spec_engine import SpecEngineCallbacks
from ...spec_engine.models import (
    ReviewResult,
    SpecCycle,
    SpecPhase,
    SpecProject,
)
from ._rotating_mixin import EngineView, RotatingRendererMixin
from ._spec_stream_processor import SpecStreamProcessor
from .base import BaseRenderer, _StreamThrottle

if TYPE_CHECKING:
    from ...card.protocols import Dispatchable
    from ...card.session import CardSession
    from ...card.session.rotator import SessionRotator
    from ...project import ProjectContext
    from ..handlers.base import BaseHandler
    from ..handlers.spec import SpecHandler

logger = logging.getLogger(__name__)


class SpecRenderer(RotatingRendererMixin, BaseRenderer):
    """
    Handles UI rendering and state management for Spec Engine interactions.
    """

    _engine_type = "spec"
    _mode_prefix = "Spec"
    _mode_emoji = "📋"
    _engine_cmd = "/spec"

    def __init__(self, handler: "SpecHandler") -> None:
        super().__init__(handler)
        self._current_session: "Dispatchable | None" = None
        self._last_cycle: int | None = None
        self._last_perspective: str | None = None
        self._pending_split_hint: str | None = None

    def get_active_session(self) -> "Dispatchable | None":
        """Return the currently active spec engine session (rotator)."""
        return self._current_session

    def notify_cycle_change(self, *, current_cycle: int, perspective: str | None) -> None:
        """Hook into the spec engine cycle/perspective lifecycle."""
        changed_cycle = self._last_cycle is not None and current_cycle != self._last_cycle
        changed_perspective = self._last_perspective is not None and perspective != self._last_perspective
        if changed_cycle or changed_perspective:
            session = self._current_session
            if session is not None and not getattr(session, "closed", False):
                persp = perspective or "—"
                self._dispatch_card_split(
                    session,
                    reason="cycle_changed",
                    hint=f"进入 cycle {current_cycle} · {persp}",
                    bridge_phrase="续接：",
                )
        self._last_cycle = current_cycle
        self._last_perspective = perspective

    def _on_card_split_completed(self, reason: str, hint: str | None, bridge_phrase: str | None = None) -> None:
        self._pending_split_hint = hint

    def _get_reporter(self):
        return self.ctx.spec_reporter

    def get_default_ui_state(self) -> dict:
        return super().get_default_ui_state()

    def create_spec_callbacks(
        self, message_id: str, chat_id: str, project: Optional["ProjectContext"],
        engine_name: str = "Coco", model_name: str = "",
    ) -> SpecEngineCallbacks:
        request_id = self.handler.ensure_request_id(
            message_id, chat_id=chat_id, project_id=(project.project_id if project else None)
        )
        reporter = self.ctx.spec_reporter

        spec_project_id = project.project_id if project else self.handler.get_working_dir(chat_id)

        # Build metadata for card sessions
        metadata = CardMetadata(
            engine_type="spec",
            mode_name="Spec",
            mode_emoji="📋",
            tool_name=engine_name,
            model_name=model_name or None,
        )

        # Session rotator: manages atomic session rotation at cycle boundaries
        # Hooks handle emoji reactions automatically on terminal events
        hooks = self._build_hooks(message_id)

        _spec_budget = RenderBudget(engine_cmd="/spec")
        # Spec engine runs can take hours (review cycles, build phases).
        # Use spec_execution_timeout as session TTL to prevent premature idle-timeout.
        _spec_ttl = float(self.settings.spec_execution_timeout)
        rotator: SessionRotator = self._create_rotator(
            chat_id, message_id, metadata,
            hooks=hooks, budget=_spec_budget, ttl_seconds=_spec_ttl,
        )
        self._current_session = rotator

        _throttle = _StreamThrottle(
            min_interval=self.settings.deep_stream_interval,
            min_chars=self.settings.deep_stream_min_chars,
        )

        processor = SpecStreamProcessor(
            rotator=rotator,
            reporter=reporter,
            metadata=metadata,
            hooks=hooks,
            budget=_spec_budget,
            spec_project_id=spec_project_id,
            message_id=message_id,
            chat_id=chat_id,
            renderer=self,
            project_root_path=project.root_path if project else "",
            throttle=_throttle,
        )

        return processor.build_callbacks()

    def render_current_view(
        self,
        message_id: str,
        chat_id: str,
        project: Optional["ProjectContext"] = None,
        origin_message_id: Optional[str] = None,
    ):
        if project is None:
            project = self.handler.project_manager.get_active_project(chat_id)

        root_path = project.root_path if project else self.handler.get_working_dir(chat_id)
        snap = self.ctx.spec_engine_manager.snapshot(chat_id, root_path)

        spec_project_id = project.project_id if project else root_path
        state = self.get_ui_state(spec_project_id)

        view_mode = state.get("view_mode", "status")
        view_context = state.get("view_context", {})

        if not snap or not snap.ext.get("project"):
            snaps = self.ctx.spec_engine_manager.snapshot_active(chat_id)
            if len(snaps) == 1 and snaps[0].ext.get("project"):
                snap = snaps[0]
            else:
                engine_name = self.handler.get_engine_name(
                    chat_id, project_id=(project.project_id if project else None)
                )
                metadata = CardMetadata(
                    engine_type="spec",
                    mode_name="Spec",
                    mode_emoji="📋",
                    tool_name=engine_name,
                )
                session = self.create_session(chat_id, message_id, metadata, budget=RenderBudget(engine_cmd="/spec"))
                session.dispatch(CardEvent.started())
                session.dispatch(CardEvent.text_started("_status"))
                session.dispatch(CardEvent.text_delta("_status", UI_TEXT["spec_status_empty"]))
                session.dispatch(CardEvent.text_done("_status"))
                session.dispatch(CardEvent.completed())
                return

        # Build a lightweight view object for internal methods
        engine = EngineView(snap)

        if view_mode == "status":
            self._render_status_view(message_id, chat_id, project, engine, state)
        elif view_mode == "cycle_done":
            cycle_num = view_context.get("cycle_num")
            self._render_cycle_view(message_id, chat_id, project, engine, state, cycle_num)
        elif view_mode == "review_done":
            cycle_num = view_context.get("cycle_num")
            self._render_review_view(message_id, chat_id, project, engine, state, cycle_num)
        elif view_mode == "error":
            error_msg = view_context.get("error", "未知错误")
            self._render_error_view(message_id, chat_id, project, engine, state, error_msg)
        else:
            self._render_status_view(message_id, chat_id, project, engine, state)

    def _render_cycle_view(self, message_id: str, chat_id: str, project, engine, state, cycle_num):
        reporter = self.ctx.spec_reporter
        engine_name = engine.engine_name
        spec_project = engine.project

        cycle = next((c for c in spec_project.cycles if c.cycle_number == cycle_num), None)
        if not cycle:
            self._render_status_view(message_id, chat_id, project, engine, state)
            return

        session = self._build_view_session(chat_id, message_id, engine_name, state)
        session.dispatch(CardEvent.started())

        content = reporter.format_cycle_done(cycle_num, cycle)
        session.dispatch(CardEvent.text_delta("_cycle_done", content))

        criteria_section = reporter.format_criteria_section(spec_project)
        session.dispatch(CardEvent.criteria_updated(
            criteria_section,
            satisfied_count=spec_project.satisfied_count,
            total_count=spec_project.total_criteria,
        ))

        session.dispatch(CardEvent.completed())

    def _render_review_view(self, message_id: str, chat_id: str, project, engine, state, cycle_num):
        reporter = self.ctx.spec_reporter
        engine_name = engine.engine_name
        spec_project = engine.project

        cycle = next((c for c in spec_project.cycles if c.cycle_number == cycle_num), None)
        if not cycle or not cycle.review_result:
            self._render_status_view(message_id, chat_id, project, engine, state)
            return

        session = self._build_view_session(chat_id, message_id, engine_name, state)
        session.dispatch(CardEvent.started())

        content = reporter.format_review_result(cycle.review_result, cycle_num)
        session.dispatch(CardEvent.text_delta("_review", content))

        criteria_section = reporter.format_criteria_section(spec_project)
        session.dispatch(CardEvent.criteria_updated(
            criteria_section,
            satisfied_count=spec_project.satisfied_count,
            total_count=spec_project.total_criteria,
        ))

        warning = self.check_warning_banner(spec_project.duration(), is_executing=True)
        if warning:
            session.dispatch(CardEvent.warning_updated(warning))

        session.dispatch(CardEvent.completed())

    def build_error_card(
        self,
        *,
        project,
        engine_name: str,
        error_msg: str,
        state: Optional[dict] = None,
        project_id: Optional[str] = None,
        engine_project_id: Optional[str] = None,
        footer_note: Optional[str] = None,
        terminal_state: Optional[str] = None,
    ) -> tuple[str, str]:
        """Build error card via CardSession and return (msg_type, card_content).

        Used by SpecHandler for inline error cards.
        """
        ui_state = state or self.get_default_ui_state()
        metadata = CardMetadata(
            engine_type="spec",
            mode_name="Spec",
            mode_emoji="📋",
            tool_name=engine_name,
            compact=ui_state.get("compact", False),
            expanded=ui_state.get("expanded", False),
            expand_ac=ui_state.get("expand_ac", False),
        )
        # Use a snapshot-only session (no TTL timer, no delivery)
        factory = self._get_session_factory()
        session = factory.create_snapshot(metadata=metadata)
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.failed(error_msg))

        # Snapshot returns the rendered card
        result = session.snapshot()
        if result:
            msg_type, card_content = result
            return (msg_type, card_content)
        return ("interactive", "{}")
