from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from src.card.thresholds import THRESHOLDS
from ...utils.errors import get_error_detail

if TYPE_CHECKING:
    from ...card.direct_session import DirectCardSession
    from ..handlers.base import BaseHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stream throttle
# ---------------------------------------------------------------------------

class _StreamThrottle:
    """Lightweight stream/plan throttle for engine renderers.

    Throttle semantics:
    - check_throttle(text_len, force) → bool
    - update_stream_state(text_len)
    - check_plan_throttle(plan_content, force) → bool
    - update_plan_state(plan_content)
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self.last_stream_ts: float = 0.0
        self.last_stream_text_len: int = 0
        self.last_plan_ts: float = 0.0
        self.last_plan_content: str = ""

    def check_throttle(
        self,
        text_len: int,
        force: bool = False,
        min_interval: Optional[float] = None,
        min_new_chars: Optional[int] = None,
    ) -> bool:
        """Return True if update should proceed, False if throttled."""
        if force:
            return True
        now = time.monotonic()
        if min_interval is None:
            min_interval = self._settings.deep_stream_interval
        if min_new_chars is None:
            min_new_chars = self._settings.deep_stream_min_chars
        if (now - self.last_stream_ts) < min_interval and (text_len - self.last_stream_text_len) < min_new_chars:
            return False
        return True

    def update_stream_state(self, text_len: int) -> None:
        """Update throttle state after a stream update."""
        self.last_stream_ts = time.monotonic()
        self.last_stream_text_len = text_len

    def check_plan_throttle(self, plan_content: str, force: bool = False, min_interval: float = 1.5) -> bool:
        """Return True if plan update should proceed."""
        if force:
            return True
        now = time.monotonic()
        if plan_content and (plan_content != self.last_plan_content or (now - self.last_plan_ts) > min_interval):
            return True
        return False

    def update_plan_state(self, plan_content: str) -> None:
        """Update plan throttle state."""
        self.last_plan_ts = time.monotonic()
        self.last_plan_content = plan_content


# ---------------------------------------------------------------------------
# DirectCardSession factory — replacement for _create_engine_sender
# ---------------------------------------------------------------------------

def _create_direct_session(
    handler: "BaseHandler",
    chat_id: str,
    message_id: str,
    *,
    session_id: str | None = None,
) -> "DirectCardSession":
    """Factory: create a DirectCardSession for engine renderers.

    This replaces the deprecated _create_engine_sender factory. Engine renderers
    use DirectCardSession.send(card_json) for create/update semantics, with
    CardDelivery handling binding management and re-anchoring internally.
    """
    return handler.create_direct_card_session(
        chat_id, reply_to=message_id, session_id=session_id
    )


def _count_tagged_nodes(obj: Any) -> int:
    """Recursively count dicts containing a ``"tag"`` key (Feishu element nodes)."""
    count = 0
    if isinstance(obj, dict):
        if "tag" in obj:
            count += 1
        for v in obj.values():
            count += _count_tagged_nodes(v)
    elif isinstance(obj, list):
        for item in obj:
            count += _count_tagged_nodes(item)
    return count



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
            "compact": False,
            "expanded": False,
            "expand_ac": False,
            "view_mode": "status",
            "view_context": {},
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

    def update_ui_state(self, project_id: str, **kwargs) -> None:
        """Update specific fields in the UI state."""
        state = self.get_ui_state(project_id)
        state.update(kwargs)

    def _check_warning_banner(self, duration: float, is_executing: bool = True) -> str | None:
        if not is_executing:
            return None
        timeout_raw = getattr(self.settings, "engine_timeout_warning_seconds", 0)
        duration_s = duration if isinstance(duration, (int, float)) else 0
        timeout_s = timeout_raw if isinstance(timeout_raw, (int, float)) else 0
        if timeout_s > 0 and duration_s > timeout_s:
            return "执行耗时较长，若无响应可尝试停止后重试"
        return None

    def _generate_progress_bar(self, current: int, total: int) -> str:
        """Generate emoji progress bar like ✅✅⬜️."""
        if total <= 0:
            return ""

        # Limit max length to avoid UI overflow on mobile
        MAX_BAR_LEN = 10

        # If total is small, show exact counts
        if total <= MAX_BAR_LEN:
            return "✅" * current + "⬜️" * (total - current)

        # If total is large, scale it down
        ratio = current / total
        filled = int(ratio * MAX_BAR_LEN)
        empty = MAX_BAR_LEN - filled
        return "✅" * filled + "⬜️" * empty + f" ({current}/{total})"

    def _render_collapsible_section(
        self, content: str, total_items: int, expanded: bool, completed_count: int = 0
    ) -> str:
        """
        Render a section that can be collapsed if too long.
        Generic version of LoopRenderer._render_ac_section.

        Args:
            content: The full content string (e.g. list of ACs, or long text)
            total_items: Total number of items (or approx lines/paragraphs)
            expanded: Whether the section is currently expanded
            completed_count: Number of completed items (for AC lists), used to generate summary
        """
        if not content or total_items == 0:
            return content

        # Threshold for collapsing
        COLLAPSE_THRESHOLD = THRESHOLDS["COLLAPSE_ITEM_THRESHOLD"]

        # If few items or expanded, show all
        if total_items <= COLLAPSE_THRESHOLD or expanded:
            return content

        # Folding logic: Filter out completed items or truncate text
        # Simple text processing approach assuming list format with checkmarks
        # (Compatible with Loop Engine AC format)
        lines = content.split("\n")
        kept_lines = []
        hidden_count = 0

        for line in lines:
            if "✅" in line:
                hidden_count += 1
            else:
                kept_lines.append(line)

        # If we couldn't identify completed items by checkmark, but it's long text (Spec mode)
        # We might want to just truncate
        if hidden_count == 0 and len(lines) > THRESHOLDS["COLLAPSE_LINE_THRESHOLD"]:  # Long text fallback
            summary = f"📄 内容较长 (共 {len(lines)} 行)，点击下方'展开'查看全部"
            return f"{summary}\n\n" + "\n".join(lines[:THRESHOLDS["COLLAPSE_DISPLAY_LINES"]]) + "\n..."

        if hidden_count == 0:
            return content

        # Add summary of hidden items
        summary = f"✅ 已通过 {hidden_count} 项 (点击下方'展开'查看全部)"

        final_lines = []
        inserted = False
        for line in kept_lines:
            final_lines.append(line)
            # Try to insert summary after a header if present
            if ("验收标准" in line or "Criteria" in line) and not inserted:
                final_lines.append(summary)
                inserted = True

        if not inserted:
            # If header not found, prepend
            final_lines.insert(0, summary)

        return "\n".join(final_lines)

    def _check_and_truncate_payload(self, card_content: str, max_size: int | None = None) -> str:
        """
        Check if card content exceeds size limit and truncate if necessary.
        Attempts to preserve JSON structure while truncating text fields.
        Also checks tagged-node count against CARD_NODE_BUDGET.
        """
        import json
        from src.card.thresholds import THRESHOLDS

        if max_size is None:
            max_size = THRESHOLDS["CARD_BYTE_BUDGET"]

        if len(card_content.encode("utf-8")) <= max_size:
            # Still check node count even if byte size is OK
            try:
                card_check = json.loads(card_content)
                node_budget = THRESHOLDS["CARD_NODE_BUDGET"]
                if _count_tagged_nodes(card_check) > node_budget:
                    logger.warning(
                        "Card node count %d exceeds budget %d, will truncate",
                        _count_tagged_nodes(card_check), node_budget,
                    )
                    # Fall through to truncation logic
                else:
                    return card_content
            except (json.JSONDecodeError, Exception):
                return card_content
        else:
            pass  # Fall through to truncation logic

        logger.warning(
            "Card payload size %d exceeds limit %d, attempting truncation", len(card_content.encode("utf-8")), max_size
        )

        try:
            card = json.loads(card_content)

            # Helper to recursively truncate strings in the dict
            # Enhanced to be smarter: prioritize preserving structure
            def truncate_recursive(obj, depth=0):
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
                                obj[k] = v[:8000] + "...(已截断)"
                            else:
                                obj[k] = truncate_recursive(v, depth + 1)
                        else:
                            obj[k] = truncate_recursive(v, depth + 1)
                elif isinstance(obj, list):
                    # If list is too long, truncate items
                    if len(obj) > 60:
                        obj = obj[:60]
                        # We might need to add a "more" item if possible, but structure varies.
                        # For now just slice.

                    for i in range(len(obj)):
                        obj[i] = truncate_recursive(obj[i], depth + 1)
                elif isinstance(obj, str):
                    # Fallback for strings in other locations
                    if len(obj) > 10000:
                        return obj[:10000] + "...(已截断)"
                return obj

            # First pass: try smart truncation on deep content
            card_copy = json.loads(json.dumps(card))  # Deep copy
            truncated_card = truncate_recursive(card_copy)

            # Add a warning note to the card body if possible
            warning_element = {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": "⚠️ 内容过长已自动截断，请在电脑端查看完整详情"}],
            }

            if "body" in truncated_card and isinstance(truncated_card.get("body", {}).get("elements"), list):
                truncated_card["body"]["elements"].append(warning_element)
            elif isinstance(truncated_card.get("elements"), list):
                truncated_card["elements"].append(warning_element)

            truncated_content = json.dumps(truncated_card, ensure_ascii=False)

            # Double check size after smart truncation
            if len(truncated_content.encode("utf-8")) > max_size:
                # If still too big, try more aggressive truncation
                # Keep header and extract a brief text summary from the original card
                summary_text = "内容过长无法完整展示。"
                try:
                    # Try to extract some meaningful text from the original content fields
                    def _extract_text(obj, limit=2000):
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

                    extracted = _extract_text(card)
                    if extracted:
                        summary_text = extracted[:2000] + "\n\n...(内容过长已截断)"
                except Exception:
                    logger.debug("failed to extract summary text", exc_info=True)

                fallback_card = {
                    "config": card.get("config", {"wide_screen_mode": True}),
                    "header": card.get("header", {"title": {"tag": "plain_text", "content": "⚠️ 卡片过大"}}),
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

    def _patch_or_send(
        self,
        message_id: str,
        chat_id: str,
        card_content: str,
        msg_type: str = "interactive",
        origin_message_id: Optional[str] = None,
    ):
        """
        Try to patch an existing message (origin_message_id), fallback to sending a reply.
        Automatically checks and truncates payload size.
        """
        # Safety check
        if msg_type == "interactive":
            card_content = self._check_and_truncate_payload(card_content)

        patched = False
        if origin_message_id:
            patched = self.handler.update_card(origin_message_id, card_content)

        if not patched:
            self.handler.reply_card(message_id, card_content)
