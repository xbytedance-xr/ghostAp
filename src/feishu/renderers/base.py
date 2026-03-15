
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional, Any

if TYPE_CHECKING:
    from ..handlers.base import BaseHandler

logger = logging.getLogger(__name__)


class SmartSender:
    """
    Helper for sending messages with smart state management:
    - Tracks current message ID for updates (Patch)
    - Auto-re-anchors (sends new message) if Patch fails
    - Manages threading context
    - Handles throttling state
    """
    def __init__(
        self, 
        handler: "BaseHandler", 
        message_id: str, 
        chat_id: str, 
        initial_message_id: Optional[str] = None
    ) -> None:
        self.handler = handler
        self.settings = handler.settings
        self.message_id = message_id
        self.chat_id = chat_id
        
        self.current_message_id: Optional[str] = initial_message_id
        self.thread_root_message_id: Optional[str] = initial_message_id
        
        # Throttling state
        self.last_stream_ts: float = 0.0
        self.last_stream_text_len: int = 0
        self.last_plan_ts: float = 0.0
        self.last_plan_content: str = ""

    def check_throttle(
        self, 
        text_len: int, 
        force: bool = False, 
        min_interval: Optional[float] = None, 
        min_new_chars: Optional[int] = None
    ) -> bool:
        """Return True if update should proceed, False if throttled."""
        if force:
            return True
        
        now = time.monotonic()
        if min_interval is None:
            min_interval = self.settings.deep_stream_interval
        if min_new_chars is None:
            min_new_chars = self.settings.deep_stream_min_chars
            
        if (now - self.last_stream_ts) < min_interval and (text_len - self.last_stream_text_len) < min_new_chars:
            return False
        return True

    def update_stream_state(self, text_len: int):
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

    def update_plan_state(self, plan_content: str):
        self.last_plan_ts = time.monotonic()
        self.last_plan_content = plan_content

    def send(
        self, 
        card_content: str, 
        msg_type: str = "interactive", 
        is_update: bool = False, 
        throttle: bool = False, 
        request_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Smart send/patch logic with auto-re-anchoring.
        Returns the message_id of the sent/updated message.
        """
        # 1. Try update existing card
        if is_update and self.current_message_id:
            if self.handler.patch_message(self.current_message_id, card_content, max_retries=1, throttle=throttle):
                return self.current_message_id

            # Patch failed (e.g. message deleted), log and fall through to create new message (Re-anchor)
            logger.warning("SmartSender: Patch failed for %s, re-anchoring...", self.current_message_id)
        
        # 2. Create new message (Only if not updating or no existing message or patch failed)
        # When creating new message, we cannot throttle (must succeed).
        use_thread = self.settings.default_reply_mode == "thread"
        result_id = None
        
        if use_thread:
            reply_to = self.thread_root_message_id or self.message_id
            result_id = self.handler.reply_message(
                reply_to, card_content, msg_type=msg_type,
                origin_message_id=self.message_id, request_id=request_id,
                reply_in_thread=True,
            )
            # If this is the first reply in a thread-based interaction, mark it as root
            if not self.thread_root_message_id and result_id:
                self.thread_root_message_id = result_id
        else:
            result_id = self.handler.send_message(
                self.chat_id, card_content, msg_type, 
                origin_message_id=self.message_id, request_id=request_id
            )

        if result_id:
            self.current_message_id = result_id
            
        return result_id


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

    def _render_collapsible_section(self, content: str, total_items: int, expanded: bool, completed_count: int = 0) -> str:
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
        COLLAPSE_THRESHOLD = 3
        
        # If few items or expanded, show all
        if total_items <= COLLAPSE_THRESHOLD or expanded:
            return content

        # Folding logic: Filter out completed items or truncate text
        # Simple text processing approach assuming list format with checkmarks
        # (Compatible with Loop Engine AC format)
        lines = content.split('\n')
        kept_lines = []
        hidden_count = 0
        
        for line in lines:
            if "✅" in line:
                hidden_count += 1
            else:
                kept_lines.append(line)
        
        # If we couldn't identify completed items by checkmark, but it's long text (Spec mode)
        # We might want to just truncate
        if hidden_count == 0 and len(lines) > 10: # Long text fallback
             summary = f"📄 内容较长 (共 {len(lines)} 行)，点击下方'展开'查看全部"
             return f"{summary}\n\n" + "\n".join(lines[:5]) + "\n..."
        
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

    def _check_and_truncate_payload(self, card_content: str, max_size: int = 30 * 1024) -> str:
        """
        Check if card content exceeds size limit and truncate if necessary.
        Attempts to preserve JSON structure while truncating text fields.
        """
        import json
        
        if len(card_content.encode('utf-8')) <= max_size:
            return card_content
            
        logger.warning("Card payload size %d exceeds limit %d, attempting truncation", 
                      len(card_content.encode('utf-8')), max_size)
                      
        try:
            card = json.loads(card_content)
            
            # Helper to recursively truncate strings in the dict
            # Enhanced to be smarter: prioritize preserving structure
            def truncate_recursive(obj, depth=0):
                if depth > 20: # Anti-recursion depth limit
                    return obj
                    
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        # Don't truncate structural keys
                        if k in ("tag", "type", "actions", "elements", "modules", "columns", "fields"):
                            obj[k] = truncate_recursive(v, depth + 1)
                        # Content fields - aggressive truncation if needed
                        elif k in ("content", "text", "value", "placeholder", "alt"):
                            if isinstance(v, str) and len(v) > 500:
                                obj[k] = v[:500] + "...(已截断)"
                            else:
                                obj[k] = truncate_recursive(v, depth + 1)
                        else:
                            obj[k] = truncate_recursive(v, depth + 1)
                elif isinstance(obj, list):
                    # If list is too long, truncate items
                    if len(obj) > 20:
                        obj = obj[:20]
                        # We might need to add a "more" item if possible, but structure varies.
                        # For now just slice.
                    
                    for i in range(len(obj)):
                        obj[i] = truncate_recursive(obj[i], depth + 1)
                elif isinstance(obj, str):
                    # Fallback for strings in other locations
                    if len(obj) > 1000: 
                        return obj[:1000] + "...(已截断)"
                return obj

            # First pass: try smart truncation on deep content
            card_copy = json.loads(json.dumps(card)) # Deep copy
            truncated_card = truncate_recursive(card_copy)
            
            # Add a warning note to the card body if possible
            warning_element = {
                "tag": "note",
                "elements": [{
                    "tag": "plain_text", 
                    "content": "⚠️ 内容过长已自动截断，请在电脑端查看完整详情"
                }]
            }
            
            if "body" in truncated_card and "elements" in truncated_card["body"]:
                # Append to existing body
                truncated_card["body"]["elements"].append(warning_element)
            elif "elements" in truncated_card:
                 # Root level elements (older schema)
                truncated_card["elements"].append(warning_element)
            
            truncated_content = json.dumps(truncated_card, ensure_ascii=False)
            
            # Double check size after smart truncation
            if len(truncated_content.encode('utf-8')) > max_size:
                # If still too big, try more aggressive truncation
                # Just keep header and a simple body
                fallback_card = {
                    "config": card.get("config", {"wide_screen_mode": True}),
                    "header": card.get("header", {"title": {"tag": "plain_text", "content": "⚠️ 卡片过大"}}),
                    "body": {
                        "elements": [
                            {
                                "tag": "div",
                                "text": {
                                    "tag": "plain_text", 
                                    "content": "内容极其庞大，无法展示摘要。请在电脑端查看。"
                                }
                            }
                        ]
                    }
                }
                return json.dumps(fallback_card, ensure_ascii=False)
                
            return truncated_content
        except Exception as e:
            logger.error("Failed to truncate payload: %s", e)
            return card_content

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
        Automatically checks and truncates payload size.
        """
        # Safety check
        if msg_type == "interactive":
            card_content = self._check_and_truncate_payload(card_content)

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
