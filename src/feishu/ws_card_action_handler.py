"""Helpers for Feishu card action callback inspection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


SYSTEM_CARD_ACTIONS = {
    "show_status",
    "switch_project",
    "show_board",
    "refresh_board",
    "show_detail",
    "new_project_prompt",
    "select_ttadk_tool",
    "select_ttadk_model",
    "refresh_ttadk_models",
    "select_acp_tool",
    "select_acp_model",
    "refresh_acp_models",
    "select_ttadk_combined_tool",
    "load_more",
    "load_prev",
    "show_ttadk_menu",
    "show_acp_menu",
    "show_help_menu",
    "show_worktree_menu",
    "worktree_finish_selection",
    "worktree_select_tool",
    "worktree_select_model",
    "worktree_remove_item",
    "worktree_clear_items",
    "worktree_confirm_start",
    "worktree_execute_action",
    "worktree_merge",
    "worktree_cleanup",
    "worktree_retry_failed",
    "worktree_retry_all",
    "force_release_repo_lock",
    "confirm_lock",
    "cancel_lock",
    "confirm_force_release",
    "cancel_force_release",
    "enter_deep_prompt",
    "continue_degraded",
    "show_error_details",
    "retry_original",
    "help_category",
    "engine_stop",
    "deep_pause",
    "deep_stop",
    "deep_resume",
    "spec_pause",
    "spec_stop",
    "spec_resume",
    "spec_skip_retry",
    "spec_review_use_auto",
    "spec_review_finish_selection",
    "spec_review_select_tool",
    "spec_review_select_model",
    "spec_review_remove_item",
    "spec_review_clear_items",
    "show_spec_review_menu",
}


class CardActionFailureAction(str, Enum):
    ACK_AND_IGNORE = "ack_and_ignore"
    REPLY_FAILURE_CARD = "reply_failure_card"
    RAISE = "raise"


@dataclass(frozen=True)
class CardActionErrorClassification:
    action: CardActionFailureAction
    phase: str
    user_reachable: bool


def classify_card_action_error(error: Exception, *, phase: str) -> CardActionErrorClassification:
    if phase in {"payload_parse", "dedup"}:
        return CardActionErrorClassification(CardActionFailureAction.ACK_AND_IGNORE, phase, False)
    if phase == "dispatch":
        return CardActionErrorClassification(CardActionFailureAction.REPLY_FAILURE_CARD, phase, True)
    return CardActionErrorClassification(CardActionFailureAction.RAISE, phase, False)


class CardActionInspector:
    """Pure helpers for extracting stable card action callback fields."""

    @classmethod
    def value_dict(cls, action: Any) -> dict[str, Any]:
        value_raw = getattr(action, "value", None)
        if isinstance(value_raw, dict):
            return value_raw
        if isinstance(value_raw, str):
            try:
                parsed = json.loads(value_raw)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @classmethod
    def action_type(cls, action: Any) -> str:
        return str(cls.value_dict(action).get("action", "") or "")

    @classmethod
    def project_id(cls, action: Any) -> Optional[str]:
        project_id = cls.value_dict(action).get("project_id")
        return project_id if isinstance(project_id, str) and project_id else None

    @classmethod
    def is_system_action(cls, action: Any) -> bool:
        return cls.action_type(action) in SYSTEM_CARD_ACTIONS

    @classmethod
    def dedup_fingerprint(cls, action: Any) -> str:
        payload: dict[str, Any] = {"value": cls._normalize_value(getattr(action, "value", None))}
        for attr in ("option", "options", "form_value", "input_value"):
            extra = getattr(action, attr, None)
            if isinstance(extra, (str, int, float, bool, list, tuple, dict)):
                payload[attr] = cls._normalize_value(extra)

        try:
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            canonical = str(payload)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _normalize_value(value: Any) -> Any:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value
