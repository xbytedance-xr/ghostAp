"""Payload truncation utilities for Feishu card content.

Extracted from BaseRenderer to keep renderer focused on session lifecycle.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.card.engine_meta import engine_type_to_cmd
from src.card.thresholds import THRESHOLDS
from src.card.ui_text import UI_TEXT
from src.utils.errors import get_error_detail

logger = logging.getLogger(__name__)


def count_tagged_nodes(obj: Any) -> int:
    """Recursively count dicts containing a ``"tag"`` key (Feishu element nodes)."""
    count = 0
    if isinstance(obj, dict):
        if "tag" in obj:
            count += 1
        for v in obj.values():
            count += count_tagged_nodes(v)
    elif isinstance(obj, list):
        for item in obj:
            count += count_tagged_nodes(item)
    return count


def check_and_truncate_payload(
    card_content: str, max_size: int | None = None, *, engine_type: str | None = None
) -> str:
    """Check if card content exceeds size limit and truncate if necessary.

    Attempts to preserve JSON structure while truncating text fields.
    Also checks tagged-node count against CARD_NODE_BUDGET.
    """
    if max_size is None:
        max_size = THRESHOLDS["CARD_BYTE_BUDGET"]

    if len(card_content.encode("utf-8")) <= max_size:
        # Still check node count even if byte size is OK
        try:
            card_check = json.loads(card_content)
            node_budget = THRESHOLDS["CARD_NODE_BUDGET"]
            if count_tagged_nodes(card_check) > node_budget:
                logger.warning(
                    "Card node count %d exceeds budget %d, will truncate",
                    count_tagged_nodes(card_check),
                    node_budget,
                )
                # Fall through to truncation logic
            else:
                return card_content
        except (json.JSONDecodeError, Exception):
            return card_content
    else:
        pass  # Fall through to truncation logic

    logger.warning(
        "Card payload size %d exceeds limit %d, attempting truncation",
        len(card_content.encode("utf-8")),
        max_size,
    )

    try:
        card = json.loads(card_content)

        # Helper to recursively truncate strings in the dict
        def truncate_recursive(obj: Any, depth: int = 0) -> Any:
            if depth > 20:  # Anti-recursion depth limit
                return obj

            if isinstance(obj, dict):
                for k, v in obj.items():
                    # Don't truncate structural keys
                    if k in ("tag", "type", "actions", "elements", "modules", "columns", "fields"):
                        obj[k] = truncate_recursive(v, depth + 1)
                    # Content fields - aggressive truncation if needed
                    elif k in ("content", "text", "value", "placeholder", "alt"):
                        # Only truncate when truly large — small strings are
                        # labels/buttons and should never be mangled.
                        if isinstance(v, str) and len(v) > 8000:
                            obj[k] = v[:8000] + UI_TEXT["truncation_suffix"]
                        else:
                            obj[k] = truncate_recursive(v, depth + 1)
                    else:
                        obj[k] = truncate_recursive(v, depth + 1)
            elif isinstance(obj, list):
                # If list is too long, truncate items
                if len(obj) > 60:
                    obj = obj[:60]

                for i in range(len(obj)):
                    obj[i] = truncate_recursive(obj[i], depth + 1)
            elif isinstance(obj, str):
                # Fallback for strings in other locations
                if len(obj) > 10000:
                    return obj[:10000] + UI_TEXT["truncation_suffix"]
            return obj

        # First pass: try smart truncation on deep content
        card_copy = json.loads(json.dumps(card))  # Deep copy
        truncated_card = truncate_recursive(card_copy)

        # Add a warning note to the card body if possible
        cmd_hint = engine_type_to_cmd(engine_type, fallback="")
        if cmd_hint:
            trunc_msg = UI_TEXT["truncation_warning_with_cmd"].format(cmd_hint=cmd_hint)
        else:
            trunc_msg = UI_TEXT["truncation_warning_generic"]
        warning_element = {
            "tag": "markdown",
            "content": trunc_msg,
            "text_size": "notation",
        }

        if "body" in truncated_card and isinstance(truncated_card.get("body", {}).get("elements"), list):
            truncated_card["body"]["elements"].append(warning_element)
        elif isinstance(truncated_card.get("elements"), list):
            truncated_card["elements"].append(warning_element)

        truncated_content = json.dumps(truncated_card, ensure_ascii=False)

        # Double check size after smart truncation
        if len(truncated_content.encode("utf-8")) > max_size:
            # If still too big, try more aggressive truncation
            summary_text = UI_TEXT["truncation_fallback_prefix"]
            try:
                extracted = _extract_text(card)
                if extracted:
                    summary_text = extracted[:2000] + UI_TEXT["truncation_fallback_suffix"]
            except Exception:
                logger.debug("failed to extract summary text", exc_info=True)

            fallback_card = {
                "schema": "2.0",
                "config": {**card.get("config", {"wide_screen_mode": True}), "update_multi": True},
                "header": card.get("header", {"title": {"tag": "plain_text", "content": UI_TEXT["truncation_card_header"]}}),
                "body": {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": summary_text,
                        }
                    ]
                },
            }
            return json.dumps(fallback_card, ensure_ascii=False)

        return truncated_content
    except Exception as e:
        logger.error("Failed to truncate payload: %s", get_error_detail(e))
        return card_content


def _extract_text(obj: Any, limit: int = 2000) -> str:
    """Try to extract meaningful text from card content fields."""
    if isinstance(obj, str) and len(obj) > 50:
        return obj[:limit]
    if isinstance(obj, dict):
        for k in ("content", "text"):
            v = obj.get(k)
            if isinstance(v, str) and len(v) > 50:
                return v[:limit]
        for v in obj.values():
            r = _extract_text(v, limit)
            if r:
                return r
    if isinstance(obj, list):
        for item in obj[:5]:
            r = _extract_text(item, limit)
            if r:
                return r
    return ""
