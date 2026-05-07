"""RotatingRendererMixin: shared template methods for Loop/Spec renderers.

Eliminates code duplication between LoopRenderer and SpecRenderer for:
- _EngineView wrapper class
- _build_view_session (parameterized by engine type/name/emoji/cmd)
- _render_error_view (identical implementation)
- _render_status_view (template with engine-specific reporter hook)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.card.events import CardEvent
from src.card.render.budget import RenderBudget
from src.card.state.models import CardMetadata

if TYPE_CHECKING:
    from src.card.session import CardSession


class EngineView:
    """Lightweight wrapper around engine snapshot for view methods."""

    __slots__ = ("engine_name", "project")

    def __init__(self, snap: Any) -> None:
        self.engine_name: str = snap.engine_name
        self.project = snap.ext.get("project")


class RotatingRendererMixin:
    """Mixin providing shared view-rendering template methods.

    Subclasses MUST define these hook properties/methods:
    - _engine_type: str  (e.g. "loop", "spec")
    - _mode_prefix: str  (e.g. "Loop", "Spec")
    - _mode_emoji: str  (e.g. "🔁", "📋")
    - _engine_cmd: str  (e.g. "/loop", "/spec")
    - _get_reporter(self)  → reporter object with format_status/format_criteria_section/get_progress_info
    """

    _engine_type: str
    _mode_prefix: str
    _mode_emoji: str
    _engine_cmd: str

    def _get_reporter(self):
        """Return the engine-specific reporter. Must be overridden."""
        raise NotImplementedError

    def _build_view_session(self, chat_id: str, message_id: str, engine_name: str, state: dict) -> "CardSession":
        """Create a fresh CardSession for view rendering."""
        metadata = CardMetadata(
            engine_type=self._engine_type,
            mode_name=self._mode_prefix,
            mode_emoji=self._mode_emoji,
            tool_name=engine_name,
            compact=state.get("compact", False),
            expanded=state.get("expanded", False),
            expand_ac=state.get("expand_ac", False),
        )
        return self.create_session(chat_id, message_id, metadata, budget=RenderBudget(engine_cmd=self._engine_cmd))

    def _render_error_view(self, message_id: str, chat_id: str, project, engine, state, error_msg: str):
        """Render an error card — identical for all rotating engines."""
        engine_name = engine.engine_name
        session = self._build_view_session(chat_id, message_id, engine_name, state)
        session.dispatch(CardEvent.started())
        session.dispatch(CardEvent.failed(error_msg))

    def _render_status_view(self, message_id: str, chat_id: str, project, engine, state):
        """Render the standard status view with criteria, progress, and warning."""
        reporter = self._get_reporter()
        engine_name = engine.engine_name
        engine_project = engine.project

        session = self._build_view_session(chat_id, message_id, engine_name, state)
        session.dispatch(CardEvent.started())

        status_content = reporter.format_status(engine_project)
        session.dispatch(CardEvent.text_delta("_status", status_content))

        # Criteria
        criteria_section = reporter.format_criteria_section(engine_project)
        session.dispatch(CardEvent.criteria_updated(
            criteria_section,
            satisfied_count=engine_project.satisfied_count,
            total_count=engine_project.total_criteria,
        ))

        # Progress
        progress_info = reporter.get_progress_info(engine_project)
        if progress_info.get("is_running"):
            session.dispatch(CardEvent.progress_updated(
                current=engine_project.satisfied_count,
                total=engine_project.total_criteria,
                label="执行中",
            ))

        # Warning
        warning = self.check_warning_banner(engine_project.duration(), is_executing=progress_info.get("is_running", False))
        if warning:
            session.dispatch(CardEvent.warning_updated(warning))

        # Terminal state if not running
        if not progress_info.get("is_running"):
            session.dispatch(CardEvent.completed())
