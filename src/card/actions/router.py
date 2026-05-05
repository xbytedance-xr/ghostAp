"""ActionRouter: routes inbound user actions (button clicks) to CardEvents or toast responses.

Extracted from CardSession to reduce single-class cognitive load.
CardSession.inbound_action() delegates to this collaborator.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from src.card.engine_meta import ENGINE_CMD_MAP, ENGINE_NAME_MAP
from src.card.events import CardEvent
from src.card.ui_text import UI_TEXT

logger = logging.getLogger(__name__)


class ActionRouter:
    """Routes action_id → CardEvent factory → dispatch, or returns toast dict on error.

    Thread-safety: relies on the owning CardSession for closed/terminal_reason checks.
    """

    def __init__(
        self,
        session_id: str,
        engine_type: str,
        action_registry: dict[str, Callable[[dict], CardEvent]],
    ) -> None:
        self._session_id = session_id
        self._engine_type = engine_type
        self._action_registry = action_registry

    @property
    def action_registry(self) -> dict[str, Callable[[dict], CardEvent]]:
        return self._action_registry

    def route_closed(self, action_id: str, terminal_reason: str) -> dict:
        """Generate toast response for actions on a closed session."""
        engine_cmd = ENGINE_CMD_MAP.get(self._engine_type or "", UI_TEXT["card_session_fallback_cmd"])
        engine_name = ENGINE_NAME_MAP.get(self._engine_type or "", "")

        if terminal_reason == "ttl_expired":
            toast_text = UI_TEXT["card_session_toast_btn_ttl_expired"].format(engine_cmd=engine_cmd, engine_name=engine_name)
        elif terminal_reason == "completed":
            toast_text = UI_TEXT["card_session_toast_btn_completed"].format(engine_cmd=engine_cmd, engine_name=engine_name)
        elif terminal_reason == "failed":
            toast_text = UI_TEXT["card_session_toast_failed"].format(engine_cmd=engine_cmd, engine_name=engine_name)
        elif terminal_reason == "cancelled":
            toast_text = UI_TEXT["card_session_toast_cancelled"].format(engine_cmd=engine_cmd, engine_name=engine_name)
        else:
            toast_text = UI_TEXT["card_session_toast_closed"].format(engine_cmd=engine_cmd, engine_name=engine_name)

        return {"toast": {"type": "info", "content": toast_text}}

    def resolve(self, action_id: str, payload: dict) -> CardEvent | dict:
        """Resolve action_id to a CardEvent, or return a toast dict on error.

        Returns:
            CardEvent if action resolved successfully.
            dict (toast response) if action is unknown or factory fails.
        """
        factory = self._action_registry.get(action_id)
        if factory is None:
            logger.warning("ActionRouter %s: unknown action '%s'", self._session_id, action_id)
            engine_cmd = ENGINE_CMD_MAP.get(self._engine_type or "", UI_TEXT["card_session_fallback_cmd"])
            base_text = UI_TEXT["card_session_toast_unknown_action"].format(engine_cmd=engine_cmd)
            return {"toast": {"type": "warning", "content": base_text}}

        try:
            return factory(payload)
        except Exception as exc:
            logger.warning("ActionRouter %s: factory error for '%s': %s", self._session_id, action_id, repr(exc))
            return {"toast": {"type": "error", "content": UI_TEXT["card_session_toast_factory_error"]}}
