"""Autonomous Resolution First — resolve ambiguity before asking the user.

When an agent encounters vague or incomplete requirements, this module attempts
autonomous resolution (reasonable assumptions, context search, memory lookup)
before falling back to structured clarification questions.

Design decisions:
- D-1: Roles may proceed under reasonable assumptions and annotate them in output.
- D-2: Max 2 structured questions per task; after that, force delivery with uncertainty.
- NFR-2: Each resolution attempt has a 15-second timeout.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional

from src.utils.async_helpers import safe_wait_for

if TYPE_CHECKING:
    from .memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# Timeout for a single autonomous resolution attempt (seconds)
RESOLVE_TIMEOUT_SECONDS: float = 15.0

# Maximum structured questions allowed per task before forcing delivery
MAX_QUESTIONS_PER_TASK: int = 2

# Markers in agent output that suggest the agent is uncertain/blocked
AMBIGUITY_MARKERS: frozenset[str] = frozenset({
    "不确定",
    "需要确认",
    "需要更多信息",
    "需要澄清",
    "信息不足",
    "无法确定",
    "not sure",
    "need clarification",
    "need more information",
    "unclear requirements",
    "ambiguous",
    "cannot determine",
})


class ResolveStatus(Enum):
    """Result of an autonomous resolution attempt."""

    RESOLVED = "resolved"                     # Successfully resolved with assumptions
    NEEDS_CLARIFICATION = "needs_clarification"  # Cannot proceed, must ask user
    TIMEOUT = "timeout"                       # Resolution attempt timed out
    SKIPPED = "skipped"                       # Question limit reached, force delivery


@dataclass
class ResolveResult:
    """Outcome of an autonomous resolution attempt."""

    status: ResolveStatus
    # The resolved/augmented task description (when RESOLVED)
    resolved_text: str = ""
    # Assumptions made during resolution (included in final output)
    assumptions: list[str] = field(default_factory=list)
    # Reasoning trace for debugging/auditing
    reasoning_trace: str = ""
    # Structured question (when NEEDS_CLARIFICATION)
    structured_question: str = ""
    # Duration of the resolution attempt
    duration_s: float = 0.0


@dataclass
class _TaskQuestionState:
    """Tracks question count per task to enforce MAX_QUESTIONS_PER_TASK."""

    question_count: int = 0
    last_question_time: float = 0.0


class AutonomousResolver:
    """Attempts to resolve ambiguous requirements autonomously before asking the user.

    Usage:
        resolver = AutonomousResolver(llm_callback=my_llm_fn)
        result = await resolver.attempt_resolve(task_text, context, memory)
        if result.status == ResolveStatus.NEEDS_CLARIFICATION:
            send_card(result.structured_question)
    """

    def __init__(
        self,
        llm_callback: Optional[object] = None,
        resolve_timeout: float = RESOLVE_TIMEOUT_SECONDS,
        max_questions: int = MAX_QUESTIONS_PER_TASK,
    ):
        self._llm_callback = llm_callback
        self._resolve_timeout = resolve_timeout
        self._max_questions = max_questions
        # task_id -> question state
        self._task_states: dict[str, _TaskQuestionState] = {}
        self._created_at: dict[str, float] = {}  # task_id → creation timestamp for TTL
        # Resolution learning: cache of successful resolutions keyed by normalized task pattern
        self._resolution_cache: dict[str, ResolveResult] = {}
        self._cache_max_size: int = 200

    def has_ambiguity_markers(self, text: str) -> bool:
        """Check if text contains markers indicating uncertainty/ambiguity."""
        text_lower = text.lower()
        return any(marker in text_lower for marker in AMBIGUITY_MARKERS)

    def get_question_count(self, task_id: str) -> int:
        """Return the number of questions already asked for a task."""
        state = self._task_states.get(task_id)
        return state.question_count if state else 0

    def can_ask_question(self, task_id: str) -> bool:
        """Check if we're still within the question budget for this task."""
        return self.get_question_count(task_id) < self._max_questions

    async def attempt_resolve(
        self,
        task_text: str,
        context: str = "",
        memory: Optional["MemoryManager"] = None,
        task_id: str = "",
        channel_id: str = "",
    ) -> ResolveResult:
        """Attempt autonomous resolution of an ambiguous task.

        Steps:
        1. Gather context from memory (L2/L3) if available
        2. Ask LLM to make reasonable assumptions and resolve ambiguity
        3. Return resolved text with assumptions, or NEEDS_CLARIFICATION

        Subject to RESOLVE_TIMEOUT_SECONDS timeout.
        """
        start_time = time.monotonic()

        # Check question budget — if exhausted, force delivery with assumptions
        if task_id and not self.can_ask_question(task_id):
            logger.info(
                "autonomous_resolver: question limit reached for task %s, forcing delivery",
                task_id,
            )
            return ResolveResult(
                status=ResolveStatus.SKIPPED,
                resolved_text=task_text,
                reasoning_trace="Question limit reached; proceeding with best-effort assumptions.",
                duration_s=time.monotonic() - start_time,
            )

        # Check resolution cache for a previously learned pattern
        cached = self.lookup_cached_resolution(task_text)
        if cached is not None:
            return ResolveResult(
                status=ResolveStatus.RESOLVED,
                resolved_text=cached.resolved_text,
                assumptions=cached.assumptions,
                reasoning_trace="Resolved from learned cache (no LLM call needed).",
                duration_s=time.monotonic() - start_time,
            )

        try:
            result = await safe_wait_for(
                self._do_resolve(task_text, context, memory, channel_id),
                timeout=self._resolve_timeout,
                action="autonomous_resolve",
            )
            result.duration_s = time.monotonic() - start_time
            return result
        except asyncio.TimeoutError:
            logger.warning(
                "autonomous_resolver: resolution timed out after %.1fs for task: %s",
                self._resolve_timeout, task_text[:100],
            )
            return ResolveResult(
                status=ResolveStatus.TIMEOUT,
                resolved_text=task_text,
                reasoning_trace="超时，基于已有信息做出最佳判断",
                duration_s=time.monotonic() - start_time,
            )
        except Exception as e:
            logger.error(
                "autonomous_resolver: unexpected error during resolution: %s",
                e, exc_info=True,
            )
            return ResolveResult(
                status=ResolveStatus.NEEDS_CLARIFICATION,
                reasoning_trace=f"Resolution failed with error: {e}",
                duration_s=time.monotonic() - start_time,
            )

    async def _do_resolve(
        self,
        task_text: str,
        context: str,
        memory: Optional["MemoryManager"],
        channel_id: str,
    ) -> ResolveResult:
        """Core resolution logic — gather context and invoke LLM for assumption-based resolution."""
        # Step 1: Gather additional context from memory
        memory_context = ""
        if memory and channel_id:
            try:
                memory_context = self._gather_memory_context(memory, channel_id)
            except Exception as e:
                logger.debug("autonomous_resolver: failed to gather memory context: %s", e)

        # Step 2: Build resolution prompt
        prompt = self._build_resolution_prompt(task_text, context, memory_context)

        # Step 3: Invoke LLM for resolution
        if self._llm_callback is None:
            # No LLM available — cannot resolve autonomously
            return ResolveResult(
                status=ResolveStatus.NEEDS_CLARIFICATION,
                reasoning_trace="No LLM callback configured for autonomous resolution.",
            )

        llm_response = await self._invoke_llm(prompt)
        if llm_response is None:
            return ResolveResult(
                status=ResolveStatus.NEEDS_CLARIFICATION,
                reasoning_trace="LLM returned empty response.",
            )

        # Step 4: Parse LLM response to determine if resolution succeeded
        return self._parse_resolution_response(llm_response, task_text)

    def _gather_memory_context(self, memory: "MemoryManager", channel_id: str) -> str:
        """Gather relevant context from L2 (group shared memory)."""
        try:
            # Read recent shared context for the channel
            l2_path = memory._channel_l2_path(channel_id)
            import os
            if os.path.isfile(l2_path):
                with open(l2_path, "r", encoding="utf-8") as f:
                    content = f.read()
                # Limit to last 2000 chars to stay within budget
                return content[-2000:] if len(content) > 2000 else content
        except Exception:
            pass
        return ""

    def _build_resolution_prompt(self, task_text: str, context: str, memory_context: str) -> str:
        """Build the LLM prompt for autonomous resolution."""
        parts = [
            "你是一个任务解析助手。用户提出了一个可能模糊或不完整的需求。",
            "优先通过合理假设或上下文推断自主解决问题。如果需要补充信息，可使用网络搜索。仅在确实无法继续时才提出澄清问题。",
            "你的目标是：",
            "1. 基于上下文做出合理假设来消除歧义",
            "2. 如果能合理推断出用户意图，输出 RESOLVED: 后跟明确化的任务描述和你的假设",
            "3. 如果确实无法推断，输出 NEEDS_CLARIFICATION: 后跟你需要了解的具体问题",
            "",
            f"用户任务: {task_text}",
        ]
        if context:
            parts.append(f"\n当前上下文: {context}")
        if memory_context:
            parts.append(f"\n历史记忆参考: {memory_context}")
        parts.append("\n请直接输出 RESOLVED: 或 NEEDS_CLARIFICATION: 开头的回复。")
        return "\n".join(parts)

    async def _invoke_llm(self, prompt: str) -> Optional[str]:
        """Invoke the configured LLM callback."""
        if self._llm_callback is None:
            return None
        try:
            # Support both sync and async callbacks
            if asyncio.iscoroutinefunction(self._llm_callback):
                return await self._llm_callback(prompt)
            else:
                return self._llm_callback(prompt)
        except Exception as e:
            logger.warning("autonomous_resolver: LLM invocation failed: %s", e)
            return None

    def _parse_resolution_response(self, response: str, original_task: str) -> ResolveResult:
        """Parse LLM response into a ResolveResult."""
        response_stripped = response.strip()

        if response_stripped.upper().startswith("RESOLVED:"):
            resolved_text = response_stripped[len("RESOLVED:"):].strip()
            # Extract assumptions (lines starting with "假设:" or "Assumption:")
            assumptions = []
            lines = resolved_text.split("\n")
            for line in lines:
                line_s = line.strip()
                if line_s.startswith("假设:") or line_s.startswith("假设："):
                    assumptions.append(line_s[3:].strip())
                elif line_s.lower().startswith("assumption:"):
                    assumptions.append(line_s[len("assumption:"):].strip())

            return ResolveResult(
                status=ResolveStatus.RESOLVED,
                resolved_text=resolved_text,
                assumptions=assumptions,
                reasoning_trace=f"LLM resolved with {len(assumptions)} assumption(s).",
            )

        # Default: needs clarification
        clarification_text = response_stripped
        if clarification_text.upper().startswith("NEEDS_CLARIFICATION:"):
            clarification_text = clarification_text[len("NEEDS_CLARIFICATION:"):].strip()

        return ResolveResult(
            status=ResolveStatus.NEEDS_CLARIFICATION,
            reasoning_trace=f"LLM could not resolve: {clarification_text[:200]}",
        )

    def record_question_asked(self, task_id: str) -> None:
        """Record that a structured question was asked for this task."""
        if task_id not in self._task_states:
            self._task_states[task_id] = _TaskQuestionState()
        self._created_at.setdefault(task_id, time.monotonic())
        state = self._task_states[task_id]
        state.question_count += 1
        state.last_question_time = time.time()

    def learn_resolution(self, task_text: str, result: ResolveResult) -> None:
        """Persist a successful resolution for future reuse.

        Called after user confirms the resolved output was acceptable. Caches the
        resolution keyed by a normalized version of the task text, so similar future
        tasks can skip the LLM call.
        """
        from ..config import get_settings as _get_settings
        if not getattr(_get_settings(), "slock_resolution_learning_enabled", True):
            return
        if result.status != ResolveStatus.RESOLVED or not result.resolved_text:
            return
        key = self._normalize_task_key(task_text)
        if not key:
            return
        # LRU eviction: remove oldest entry if at capacity
        if len(self._resolution_cache) >= self._cache_max_size:
            oldest_key = next(iter(self._resolution_cache))
            del self._resolution_cache[oldest_key]
        self._resolution_cache[key] = result
        logger.debug("autonomous_resolver: learned resolution for key=%s", key[:50])

    def lookup_cached_resolution(self, task_text: str) -> Optional[ResolveResult]:
        """Check if a similar task was previously resolved — return cached result or None."""
        from ..config import get_settings as _get_settings
        if not getattr(_get_settings(), "slock_resolution_learning_enabled", True):
            return None
        key = self._normalize_task_key(task_text)
        if not key:
            return None
        cached = self._resolution_cache.get(key)
        if cached:
            logger.info("autonomous_resolver: cache hit for key=%s", key[:50])
        return cached

    @staticmethod
    def _normalize_task_key(text: str) -> str:
        """Normalize task text into a cache-friendly key.

        Strips whitespace, lowercases, removes punctuation for fuzzy matching.
        """
        import re
        normalized = " ".join(text.lower().split())
        # Remove non-alphanumeric, non-CJK-letter characters
        normalized = re.sub(r"[^\w\s]", "", normalized)
        # Collapse whitespace
        normalized = " ".join(normalized.split())
        return normalized[:200]

    def format_structured_question(
        self,
        attempts_summary: str,
        blocker: str,
        candidates: list[str],
    ) -> str:
        """Format a structured clarification question for the user.

        Format:
        - 已尝试: <summary of what was tried>
        - 卡点: <specific blocker description>
        - 候选方案: 1. ... 2. ... 3. ...

        Returns markdown string suitable for Feishu card.
        """
        parts = [
            "🤔 **需要您的输入**",
            "",
            f"**已尝试:** {attempts_summary}",
            "",
            f"**卡点:** {blocker}",
            "",
            "**候选方案:**",
        ]
        for i, candidate in enumerate(candidates[:3], 1):
            parts.append(f"{i}. {candidate}")

        parts.append("")
        parts.append("请回复方案编号（如 `1`）或直接描述您的期望。")
        return "\n".join(parts)

    def cleanup_task(self, task_id: str) -> None:
        """Remove tracking state for a completed task."""
        self._task_states.pop(task_id, None)
        self._created_at.pop(task_id, None)

    def cleanup_stale(self, ttl: float = 3600.0) -> int:
        """Remove task states older than ttl seconds. Returns count of removed entries."""
        now = time.monotonic()
        stale_keys = [
            k for k, created in self._created_at.items()
            if now - created > ttl
        ]
        for k in stale_keys:
            self._task_states.pop(k, None)
            self._created_at.pop(k, None)
        return len(stale_keys)
