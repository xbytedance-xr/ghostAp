"""Unified task/chitchat classification for the Slock engine.

Single source of truth for determining whether a user message is casual chitchat
(to be ignored) or a valid task (to be routed to agents). Used by both the
dispatcher auto-activation logic and the TaskRouter routing path.
"""

from __future__ import annotations

import re
from typing import Final

# ---------------------------------------------------------------------------
# Compiled pattern constants (module-level singletons)
# ---------------------------------------------------------------------------

# Explicit greeting/acknowledgment/reaction patterns — always chitchat
_CHITCHAT_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"^(你好|嗨|hi|hello|hey|yo|早上好|晚上好|下午好|早安|晚安)[!！。.~]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(谢谢|thanks|thank\s*you|thx|ok|好的|收到了?|了解|明白|嗯|对|知道了|okok|alright)[!！。.~]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(哈{2,}|嘿嘿|呵呵|lol|haha|😂|👍|🙏|666|nb|yes|no|k|y|n|nice|cool|got\s*it|roger|ack)[!！。.~]*$",
        re.IGNORECASE,
    ),
)

# CJK Unified Ideographs detector
_CJK_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\u4e00-\u9fff]")

# Pure punctuation/emoji — no word characters and no CJK
_PURE_PUNCT_EMOJI: Final[re.Pattern[str]] = re.compile(
    r"^[^\w\u4e00-\u9fff]+$",
    re.UNICODE,
)

# CJK chitchat keywords — short CJK messages matching these are still chitchat
# even if they pass the CJK whitelist rule (length >= 2)
_CJK_CHITCHAT_KEYWORDS: Final[re.Pattern[str]] = re.compile(
    r"^(你好啊|谢谢啦|好的呢|收到啦|了解了|明白了|知道啦|没事|没问题|"
    r"可以的|行的|好吧|好哒|嗯嗯|对对|是的|好滴|ok啦|谢啦|"
    r"早啊|晚安啊|你好呀|好嘞|今天天气不错|下班了|吃什么|周末去哪|周末快乐)[!！。.~?？]*$",
    re.IGNORECASE,
)

_EN_CHITCHAT_PHRASES: Final[re.Pattern[str]] = re.compile(
    r"^(good\s+morning|good\s+night|good\s+evening|how\s+are\s+you)[!！。.~?？]*$",
    re.IGNORECASE,
)

# Developer short-command whitelist — these ≤3 char tokens should NOT be filtered
_DEV_TERM_WHITELIST: Final[frozenset[str]] = frozenset({
    "fix", "bug", "wip", "test", "deploy", "run", "build", "lint",
    "push", "pull", "sync", "ship", "dev", "ci", "cd", "doc",
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class TaskClassifier:
    """Singleton-style classifier determining if a message is chitchat or a task.

    Usage:
        from src.slock_engine.task_classifier import TaskClassifier

        if TaskClassifier.is_chitchat("你好"):
            # ignore
        else:
            # route to agent
    """

    @staticmethod
    def classify(text: str) -> tuple[bool, float]:
        """Classify message as chitchat or task with confidence score.

        Returns:
            (is_chitchat, confidence) where confidence is 0.0-1.0.
            High confidence (>=0.9) means regex matched definitively.
            Low confidence (<0.7) means ambiguous — consider LLM fallback.
        """
        stripped = text.strip()
        if not stripped:
            return (True, 1.0)

        # Rule 2: Explicit blacklist patterns — high confidence
        for pattern in _CHITCHAT_PATTERNS:
            if pattern.match(stripped):
                return (True, 0.95)
        if _EN_CHITCHAT_PHRASES.match(stripped):
            return (True, 0.95)

        # Rule 3 & 4: CJK-aware classification
        has_cjk = bool(_CJK_PATTERN.search(stripped))
        if has_cjk:
            # Rule 3: CJK chitchat keywords with short length
            if len(stripped) <= 6 and _CJK_CHITCHAT_KEYWORDS.match(stripped):
                return (True, 0.9)
            # Rule 4: CJK content with length >= 2 is likely a valid task
            if len(stripped) >= 2:
                # Longer CJK messages are more likely tasks
                conf = min(0.6 + len(stripped) * 0.02, 0.95)
                return (False, conf)

        # Rule 5: Pure punctuation/emoji
        try:
            if _PURE_PUNCT_EMOJI.match(stripped):
                return (True, 0.95)
        except (re.error, TypeError):
            pass

        # Rule 6: Developer term whitelist (short but valid commands)
        if stripped.lower() in _DEV_TERM_WHITELIST:
            return (False, 0.85)

        # Rule 7: Very short non-CJK messages — lower confidence
        if len(stripped) <= 3:
            return (True, 0.6)

        # Longer messages default to task with moderate confidence
        conf = min(0.6 + len(stripped) * 0.01, 0.9)
        return (False, conf)

    @staticmethod
    def is_chitchat(text: str) -> bool:
        """Return True if the message is casual chitchat that should NOT be routed.

        For confidence-aware classification, use classify() instead.
        """
        is_chat, _ = TaskClassifier.classify(text)
        return is_chat

    @staticmethod
    def is_task(text: str) -> bool:
        """Convenience inverse of is_chitchat."""
        return not TaskClassifier.is_chitchat(text)

    @staticmethod
    def classify_with_uncertainty(
        text: str,
        *,
        task_threshold: float = 0.7,
        chat_threshold: float = 0.7,
    ) -> tuple[str, float]:
        """Classify message with explicit uncertainty state.

        Args:
            text: The message text to classify.
            task_threshold: Minimum confidence to classify as "task".
            chat_threshold: Minimum confidence to classify as "chat".

        Returns:
            Tuple of (classification, confidence) where classification is one of:
            - "task": High confidence the message is a task
            - "chat": High confidence the message is chitchat
            - "uncertain": Low confidence, needs clarification
        """
        is_chitchat, confidence = TaskClassifier.classify(text)

        if is_chitchat:
            if confidence >= chat_threshold:
                return ("chat", confidence)
            return ("uncertain", confidence)
        else:
            if confidence >= task_threshold:
                return ("task", confidence)
            return ("uncertain", confidence)
