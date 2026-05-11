"""WorktreeRenderer: session lifecycle management for worktree engine.

Follows the same pattern as DeepRenderer/SpecRenderer,
extracting CardSession management from WorktreeHandler.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from ...card.actions.dispatch import build_worktree_action_registry
from ...card.events import CardEvent, CardEventType
from ...card.render.budget import RenderBudget
from ...card.state.models import CardMetadata
from .base import BaseRenderer

if TYPE_CHECKING:
    from ...card.protocols import Dispatchable
    from ...card.session import CardSession
    from ..handlers.worktree import WorktreeHandler

logger = logging.getLogger(__name__)


class _WorktreeGCHook:
    """Hook that removes a worktree session from the renderer's dict on terminal.

    This provides proactive GC — when a session TTL-expires or completes,
    the session is immediately removed from WorktreeRenderer._sessions
    instead of waiting for the next get_or_create_session() access.
    """

    __slots__ = ("_renderer", "_project_id")

    def __init__(self, renderer: "WorktreeRenderer", project_id: str) -> None:
        self._renderer = renderer
        self._project_id = project_id

    def on_dispatched(self, event, state) -> None:
        pass  # no-op

    def on_terminal(self, state, reason) -> None:
        with self._renderer._lock:
            self._renderer._sessions.pop(self._project_id, None)


class WorktreeRenderer(BaseRenderer):
    """Manages CardSession lifecycle for worktree engine flows.

    Responsibilities:
    - Session creation/lookup (TTL is managed by CardSession internally)
    - Session closure and cleanup
    - Throttled progress callback factory
    """

    def __init__(self, handler: "WorktreeHandler") -> None:
        super().__init__(handler)
        self._sessions: dict[str, "Dispatchable"] = {}  # project_id → active session
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    def get_or_create_session(
        self, chat_id: str, project_id: str, *, reply_to: str | None = None, tool_name: str = ""
    ) -> "CardSession":
        """Get or create a CardSession for a worktree project.

        Thread-safe: acquires internal lock.
        Cleans up closed sessions lazily on access.
        """
        with self._lock:
            # Clean up closed session if present
            session = self._sessions.get(project_id)
            if session and session.closed:
                del self._sessions[project_id]
                session = None

            if session:
                return session

            metadata = self.build_unit_metadata(
                CardMetadata(
                    engine_type="worktree", mode_name="Worktree", mode_emoji="🌳",
                    tool_name=tool_name or "",
                ),
                unit_id=project_id,
                unit_kind="worktree",
                unit_label=project_id,
            )
            hooks = self._build_hooks(reply_to or "", chat_id=chat_id)
            gc_hook = _WorktreeGCHook(self, project_id)
            all_hooks = (*hooks, gc_hook) if hooks else (gc_hook,)
            session = self.create_session(
                chat_id=chat_id,
                message_id=reply_to or "",
                metadata=metadata,
                hooks=all_hooks,
                budget=RenderBudget(engine_cmd="/worktree"),
                action_registry=build_worktree_action_registry(),
                notify_callback=self.handler.send_text_to_chat,
            )
            self._sessions[project_id] = session
            return session

    def close_session(self, project_id: str) -> None:
        """Close and remove a worktree session. Thread-safe."""
        with self._lock:
            session = self._sessions.pop(project_id, None)
        if session:
            session.close()

    def get_session(self, project_id: str) -> "CardSession | None":
        """Get existing session without creating. Thread-safe."""
        with self._lock:
            session = self._sessions.get(project_id)
            if session and not session.closed:
                return session
        return None
