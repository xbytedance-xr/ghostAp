"""Sidebar Channel — lightweight inter-agent communication below formal discussion.

Provides fire-and-forget messages (FYI/QUESTION/OFFER) between agents
that are injected into the recipient's next prompt cycle without triggering
the full DiscussionThread state machine.

Thread-safe: uses a lock per channel for concurrent access from executor threads.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SidebarMsgType(Enum):
    FYI = "fyi"
    QUESTION = "question"
    OFFER = "offer"


@dataclass
class SidebarMessage:
    sender_id: str
    sender_name: str
    recipient_id: str
    msg_type: SidebarMsgType
    content: str
    timestamp: float = field(default_factory=time.time)
    ttl_seconds: float = 180.0  # 3 minute TTL

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.timestamp) > self.ttl_seconds


# Output markers regex — agent output is scanned for these
_SIDEBAR_PATTERN = re.compile(
    r"\[(?P<type>FYI|QUESTION|OFFER):@(?P<recipient>[^\]]+)\]\s*(?P<content>.+?)(?=\n\[(?:FYI|QUESTION|OFFER):|$)",
    re.DOTALL,
)

# Rate limit: max messages per agent per window
_MAX_MESSAGES_PER_AGENT = 3
_RATE_WINDOW_SECONDS = 300.0  # 5 minutes


class SidebarChannel:
    """Per-channel sidebar message bus.

    Messages are stored in-memory with TTL expiry. No persistence.
    """

    def __init__(self, max_pending_per_agent: int = 5) -> None:
        self._lock = threading.Lock()
        # recipient_id -> deque of pending messages
        self._inbox: dict[str, deque[SidebarMessage]] = {}
        self._max_pending = max_pending_per_agent
        # sender_id -> list of timestamps for rate limiting
        self._send_log: dict[str, list[float]] = {}

    def post(self, msg: SidebarMessage) -> bool:
        """Post a sidebar message. Returns False if rate-limited."""
        with self._lock:
            # Rate limit check
            now = time.time()
            log = self._send_log.setdefault(msg.sender_id, [])
            # Prune old entries
            log[:] = [t for t in log if (now - t) < _RATE_WINDOW_SECONDS]
            if len(log) >= _MAX_MESSAGES_PER_AGENT:
                return False
            log.append(now)

            # Add to recipient inbox
            inbox = self._inbox.setdefault(msg.recipient_id, deque(maxlen=self._max_pending))
            inbox.append(msg)
            return True

    def get_pending(self, recipient_id: str) -> list[SidebarMessage]:
        """Consume all pending non-expired messages for a recipient."""
        with self._lock:
            inbox = self._inbox.get(recipient_id)
            if not inbox:
                return []
            # Filter expired, consume all
            messages = [m for m in inbox if not m.is_expired]
            inbox.clear()
            return messages

    def expire_stale(self) -> int:
        """Remove expired messages from all inboxes. Returns count removed."""
        removed = 0
        with self._lock:
            for recipient_id, inbox in list(self._inbox.items()):
                before = len(inbox)
                fresh = deque(
                    (m for m in inbox if not m.is_expired),
                    maxlen=self._max_pending,
                )
                removed += before - len(fresh)
                if fresh:
                    self._inbox[recipient_id] = fresh
                else:
                    del self._inbox[recipient_id]
        return removed

    @staticmethod
    def parse_output_markers(output: str) -> tuple[str, list[tuple[str, str, str]]]:
        """Parse sidebar markers from agent output.

        Returns:
            (cleaned_output, [(msg_type, recipient_name, content), ...])
        """
        markers: list[tuple[str, str, str]] = []
        cleaned = output

        for match in _SIDEBAR_PATTERN.finditer(output):
            msg_type = match.group("type")
            recipient = match.group("recipient").strip()
            content = match.group("content").strip()
            if content and len(content) <= 500:
                markers.append((msg_type, recipient, content))

        if markers:
            # Remove marker lines from output
            cleaned = _SIDEBAR_PATTERN.sub("", output).strip()

        return cleaned, markers

    def render_pending_for_prompt(self, recipient_id: str) -> str:
        """Render pending messages as a prompt section. Consumes messages."""
        messages = self.get_pending(recipient_id)
        if not messages:
            return ""

        lines: list[str] = []
        for msg in messages:
            type_label = {"fyi": "FYI", "question": "Q", "offer": "Offer"}[msg.msg_type.value]
            lines.append(f"[{type_label} from @{msg.sender_name}] {msg.content[:300]}")

        return "\n# Sidebar Messages\n" + "\n".join(lines) + (
            "\n(这些是队友的非正式消息。你可以在回复中简要回应，或忽略不相关的。)"
        )
