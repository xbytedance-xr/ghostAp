
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Any

if TYPE_CHECKING:
    from ..handlers.base import BaseHandler

logger = logging.getLogger(__name__)

class BaseRenderer:
    """
    Base class for renderers handling UI state and message sending.
    """

    def __init__(self, handler: "BaseHandler") -> None:
        self.handler = handler
        self.ctx = handler.ctx
        self.settings = handler.settings
        # project_id -> state dict
        self.ui_states: dict[str, dict[str, Any]] = {}

    def get_default_ui_state(self) -> dict[str, Any]:
        """
        Return the default UI state dictionary.
        Subclasses should override this to provide specific defaults.
        """
        return {
            "compact": self.settings.card_deep_compact_default,
            "expanded": False,
        }

    def get_ui_state(self, project_id: str) -> dict[str, Any]:
        """
        Get the UI state for a specific project.
        Initializes with defaults if not present.
        """
        if not project_id:
            return self.get_default_ui_state()
            
        if project_id not in self.ui_states:
            self.ui_states[project_id] = self.get_default_ui_state()
            
        return self.ui_states[project_id]

    def update_ui_state(self, project_id: str, **kwargs):
        """Update specific fields in the UI state."""
        state = self.get_ui_state(project_id)
        state.update(kwargs)

    def _patch_or_send(
        self, 
        message_id: str, 
        chat_id: str, 
        card_content: str, 
        msg_type: str = "interactive", 
        origin_message_id: Optional[str] = None
    ):
        """
        Try to patch an existing message (origin_message_id), fallback to sending a reply.
        """
        patched = False
        if origin_message_id:
            patched = self.handler.patch_message(origin_message_id, card_content, max_retries=1)
        
        if not patched:
            self.handler.reply_message(
                message_id, 
                card_content, 
                msg_type=msg_type, 
                origin_message_id=origin_message_id
            )
