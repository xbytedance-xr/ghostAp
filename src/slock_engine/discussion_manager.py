"""Inter-agent discussion protocol for the Slock multi-agent collaboration engine.

This module implements the discussion manager that orchestrates structured
conversations between agents — handling trigger detection, round execution,
convergence checking, token budget control, and conclusion summarization.

Thread-safety note: Discussion execution runs within the engine's bounded
executor. The DiscussionThread dataclass is mutable and should only be
mutated by one coroutine at a time (ensured by the engine's task scheduling).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Optional

from .models import (
    AgentIdentity,
    AgentStatus,
    DiscussionConfig,
    DiscussionMessage,
    DiscussionStatus,
    DiscussionThread,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Uncertainty markers that can trigger a discussion (bilingual)
UNCERTAINTY_MARKERS: tuple[str, ...] = (
    "不确定",
    "需要确认",
    "建议讨论",
    "I'm not sure",
    "I am not sure",
    "needs review",
    "uncertain",
    "not confident",
    "需要讨论",
    "请确认",
)

# Convergence signal keywords (bilingual)
CONVERGENCE_SIGNALS: tuple[str, ...] = (
    "AGREE",
    "同意",
    "没有其他建议",
    "LGTM",
    "no further suggestions",
    "looks good",
)

# Regex pattern for @mention detection
AT_MENTION_PATTERN: re.Pattern[str] = re.compile(r"@(\w+)")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DiscussionCancelledError(Exception):
    """Raised when a discussion is cancelled via cooperative cancellation event."""
    pass


# ---------------------------------------------------------------------------
# DiscussionManager
# ---------------------------------------------------------------------------


class DiscussionManager:
    """Orchestrates inter-agent discussions within the Slock engine.

    Responsibilities:
    - Detect when a discussion should be triggered based on rules, @mentions,
      or uncertainty markers in agent output.
    - Manage the discussion lifecycle: start, execute rounds, check convergence,
      summarize conclusions.
    - Enforce token budget constraints to prevent runaway conversations.
    """

    def __init__(
        self,
        *,
        engine: Any = None,
        memory_manager: Any = None,
        config: Optional[DiscussionConfig] = None,
        on_unavailable_notify: Optional[Any] = None,
    ) -> None:
        """Initialize the discussion manager.

        Args:
            engine: Reference to SlockEngine (typed as Any to avoid circular import).
            memory_manager: Reference to MemoryManager for context retrieval.
            config: Default discussion configuration. Uses DiscussionConfig defaults
                    if not provided.
            on_unavailable_notify: Optional callback(agent_id, reason) called when an
                    agent is unavailable during discussion (for user notification).
        """
        self._engine = engine
        self._memory_manager = memory_manager
        self._config = config or DiscussionConfig()
        self._on_unavailable_notify = on_unavailable_notify

        # Task 26: Cooldown and depth limit tracking
        self._last_discussion_time: dict[str, float] = {}
        self._discussion_depth: dict[str, int] = {}
        self._cooldown_seconds = 60.0
        self._max_depth = 3

        # Task 27: Discussion-task binding
        self._task_bindings: dict[str, str] = {}  # thread_id -> task_id

        logger.info(
            "DiscussionManager initialized (max_rounds=%d, token_budget=%d)",
            self._config.max_rounds,
            self._config.token_budget,
        )

    # ------------------------------------------------------------------
    # Trigger Detection
    # ------------------------------------------------------------------

    def should_trigger_discussion(
        self,
        agent: AgentIdentity,
        result_content: str,
        config: Optional[DiscussionConfig] = None,
    ) -> Optional[DiscussionThread]:
        """Determine whether an agent's output should trigger a discussion.

        Three trigger strategies are evaluated in order:
        1. Rule trigger — role-based routing (e.g. "coder->reviewer").
        2. @mention trigger — explicit @AgentName in content.
        3. Uncertainty trigger — presence of uncertainty markers.

        Args:
            agent: The agent that produced the result.
            result_content: The text output from the agent's execution.
            config: Optional override config; defaults to instance config.

        Returns:
            A new DiscussionThread if triggered, or None.
        """
        cfg = config or self._config

        # Cooldown check
        if self._is_on_cooldown(agent.agent_id):
            logger.debug("Discussion suppressed: agent %s is on cooldown", agent.agent_id)
            return None

        # --- Strategy 1: Rule-based trigger ---
        thread = self._check_rule_trigger(agent, cfg)
        if thread is not None:
            logger.info(
                "Discussion triggered by rule for agent %s (role=%s)",
                agent.agent_id,
                agent.role,
            )
            return thread

        # --- Strategy 2: @mention trigger ---
        thread = self._check_mention_trigger(agent, result_content, cfg)
        if thread is not None:
            logger.info(
                "Discussion triggered by @mention in output of agent %s",
                agent.agent_id,
            )
            return thread

        # --- Strategy 3: Uncertainty trigger ---
        thread = self._check_uncertainty_trigger(agent, result_content, cfg)
        if thread is not None:
            logger.info(
                "Discussion triggered by uncertainty markers in agent %s output",
                agent.agent_id,
            )
            return thread

        return None

    def _check_rule_trigger(
        self, agent: AgentIdentity, config: DiscussionConfig
    ) -> Optional[DiscussionThread]:
        """Check if the agent's role matches any trigger rule."""
        for rule in config.trigger_rules:
            parts = rule.split("->")
            if len(parts) != 2:
                logger.warning("Invalid trigger rule format: %s", rule)
                continue
            source_role, target_role = parts[0].strip(), parts[1].strip()
            if agent.role == source_role:
                target_agent_id = self._find_agent_by_role(target_role)
                if target_agent_id is not None:
                    thread = DiscussionThread(
                        thread_id=str(uuid.uuid4()),
                        participants=[agent.agent_id, target_agent_id],
                        config=config,
                        trigger_reason=f"rule:{rule}",
                    )
                    return thread
                else:
                    logger.debug(
                        "Rule '%s' matched but no agent with role '%s' found",
                        rule,
                        target_role,
                    )
        return None

    def _check_mention_trigger(
        self, agent: AgentIdentity, content: str, config: DiscussionConfig
    ) -> Optional[DiscussionThread]:
        """Check if content contains @AgentName mentions."""
        matches = AT_MENTION_PATTERN.findall(content)
        if not matches:
            return None

        for mentioned_name in matches:
            target_agent_id = self._find_agent_by_name(mentioned_name)
            if target_agent_id is not None:
                thread = DiscussionThread(
                    thread_id=str(uuid.uuid4()),
                    participants=[agent.agent_id, target_agent_id],
                    config=config,
                    trigger_reason=f"mention:@{mentioned_name}",
                )
                return thread

        return None

    def _check_uncertainty_trigger(
        self, agent: AgentIdentity, content: str, config: DiscussionConfig
    ) -> Optional[DiscussionThread]:
        """Check if content contains uncertainty markers."""
        content_lower = content.lower()
        for marker in UNCERTAINTY_MARKERS:
            if marker.lower() in content_lower:
                # Find a suitable discussion partner — prefer reviewer role
                target_agent_id = self._find_agent_by_role("reviewer")
                if target_agent_id is None:
                    target_agent_id = self._find_agent_by_role("planner")
                if target_agent_id is None:
                    logger.debug(
                        "Uncertainty detected but no suitable partner found"
                    )
                    return None
                thread = DiscussionThread(
                    thread_id=str(uuid.uuid4()),
                    participants=[agent.agent_id, target_agent_id],
                    config=config,
                    trigger_reason=f"uncertainty:{marker}",
                )
                return thread

        return None

    # ------------------------------------------------------------------
    # Cooldown & Depth Limit (Task 26)
    # ------------------------------------------------------------------

    def _is_on_cooldown(self, agent_id: str) -> bool:
        """Check if agent is on discussion cooldown."""
        last_time = self._last_discussion_time.get(agent_id, 0)
        return (time.time() - last_time) < self._cooldown_seconds

    def _record_discussion_participation(self, agent_id: str) -> None:
        """Record that an agent participated in a discussion."""
        self._last_discussion_time[agent_id] = time.time()

    def _check_depth_limit(self, parent_thread_id: Optional[str] = None) -> bool:
        """Check if discussion depth limit would be exceeded.

        Returns True if within limits, False if exceeded.
        """
        if parent_thread_id is None:
            return True
        depth = self._discussion_depth.get(parent_thread_id, 0)
        return depth < self._max_depth

    def _increment_depth(self, thread_id: str, parent_thread_id: Optional[str] = None) -> None:
        """Increment the depth counter for a new sub-discussion."""
        parent_depth = self._discussion_depth.get(parent_thread_id, 0) if parent_thread_id else 0
        self._discussion_depth[thread_id] = parent_depth + 1

    # ------------------------------------------------------------------
    # Discussion-Task Binding (Task 27)
    # ------------------------------------------------------------------

    def bind_to_task(self, thread_id: str, task_id: str) -> None:
        """Bind a discussion thread to a specific task."""
        self._task_bindings[thread_id] = task_id
        logger.debug("Discussion %s bound to task %s", thread_id, task_id)

    def get_bound_task(self, thread_id: str) -> Optional[str]:
        """Get the task_id bound to a discussion thread."""
        return self._task_bindings.get(thread_id)

    def unbind_task(self, thread_id: str) -> None:
        """Remove task binding when discussion completes."""
        self._task_bindings.pop(thread_id, None)

    # ------------------------------------------------------------------
    # Discussion Lifecycle
    # ------------------------------------------------------------------

    def start_discussion(
        self, thread: DiscussionThread, initial_content: str
    ) -> DiscussionThread:
        """Start a discussion by adding the initial message and activating the thread.

        Args:
            thread: The discussion thread (from should_trigger_discussion).
            initial_content: The content that triggered the discussion.

        Returns:
            The activated thread with the initial message appended.
        """
        if not thread.participants:
            logger.error("Cannot start discussion with no participants")
            return thread

        initial_message = DiscussionMessage(
            message_id=str(uuid.uuid4()),
            sender_agent_id=thread.participants[0],
            receiver_agent_id=(
                thread.participants[1] if len(thread.participants) > 1 else ""
            ),
            content=initial_content,
            round_num=0,
            timestamp=time.time(),
            token_count=len(initial_content) // 4,
        )

        thread.messages.append(initial_message)
        thread.total_tokens_used += initial_message.token_count
        thread.status = DiscussionStatus.ACTIVE

        logger.info(
            "Discussion %s started with %d participants",
            thread.thread_id[:8],
            len(thread.participants),
        )
        return thread

    def execute_round(self, thread: DiscussionThread) -> DiscussionThread:
        """Execute one round of discussion — determine next speaker and get response.

        Alternates between participants. Builds context from the last 3 messages
        and asks the responding agent to provide their perspective.

        Checks cancellation_event before ACP call for cooperative cancellation.

        Args:
            thread: The active discussion thread.

        Returns:
            The thread with the new response message appended.

        Raises:
            DiscussionCancelledError: If cancellation_event is set (e.g., by watchdog).
        """
        if not thread.is_active:
            logger.warning(
                "Cannot execute round on inactive thread %s (status=%s)",
                thread.thread_id[:8],
                thread.status.value,
            )
            return thread

        # Check for cooperative cancellation before executing
        cancellation_event = getattr(thread, 'cancellation_event', None)
        if cancellation_event and cancellation_event.is_set():
            raise DiscussionCancelledError(
                f"Discussion {thread.thread_id[:8]} cancelled before round execution"
            )

        # Determine next respondent (alternate between participants)
        current_round = thread.current_round + 1
        num_participants = len(thread.participants)
        if num_participants < 2:
            logger.error("Discussion requires at least 2 participants")
            return thread

        # Simple alternation: round 0 = participant[0], round 1 = participant[1], etc.
        respondent_idx = current_round % num_participants
        respondent_id = thread.participants[respondent_idx]

        # Determine the other agent (sender of request)
        sender_idx = (current_round - 1) % num_participants
        sender_id = thread.participants[sender_idx]

        # Build discussion prompt with recent context
        prompt = self._build_round_prompt(thread, respondent_id)

        # Check cancellation again right before the ACP call (most expensive operation)
        if cancellation_event and cancellation_event.is_set():
            raise DiscussionCancelledError(
                f"Discussion {thread.thread_id[:8]} cancelled before ACP call"
            )

        # Execute agent turn
        response = self._execute_agent_turn(respondent_id, prompt, thread=thread)

        # Estimate token usage
        token_count = len(response) // 4

        # Create response message
        message = DiscussionMessage(
            message_id=str(uuid.uuid4()),
            sender_agent_id=respondent_id,
            receiver_agent_id=sender_id,
            content=response,
            round_num=current_round,
            timestamp=time.time(),
            token_count=token_count,
        )

        thread.messages.append(message)
        thread.total_tokens_used += token_count

        logger.debug(
            "Discussion %s round %d: agent %s responded (%d tokens)",
            thread.thread_id[:8],
            current_round,
            respondent_id[:8],
            token_count,
        )
        return thread

    def _build_round_prompt(
        self, thread: DiscussionThread, respondent_id: str
    ) -> str:
        """Build the discussion prompt for the next respondent.

        Includes the agent's role context, last 3 messages, and instructions.
        """
        # Collect last 3 messages for context
        recent_messages = thread.messages[-3:]
        context_lines: list[str] = []

        context_lines.append("=== Discussion Context ===")
        context_lines.append(f"Thread: {thread.thread_id[:8]}")
        context_lines.append(f"Trigger: {thread.trigger_reason}")
        context_lines.append(f"Round: {thread.current_round + 1}/{thread.config.max_rounds}")
        context_lines.append("")
        context_lines.append("--- Recent Messages ---")

        for msg in recent_messages:
            sender_label = msg.sender_agent_id[:8] if msg.sender_agent_id else "unknown"
            context_lines.append(f"[{sender_label}] (round {msg.round_num}):")
            context_lines.append(msg.content)
            context_lines.append("")

        context_lines.append("--- Your Turn ---")
        context_lines.append(
            "Review the above discussion and provide your perspective. "
            "If you agree with the conclusion, state 'AGREE'. "
            "If you have improvements, provide them concisely."
        )

        return "\n".join(context_lines)

    # ------------------------------------------------------------------
    # Convergence & Budget
    # ------------------------------------------------------------------

    def check_convergence(self, thread: DiscussionThread) -> bool:
        """Check whether the discussion has converged.

        Convergence is detected when:
        1. The last message contains explicit convergence signals (AGREE, LGTM, etc.)
        2. The last two messages from the same agent are highly similar
           (exceeding convergence_threshold using word overlap).

        Args:
            thread: The discussion thread to check.

        Returns:
            True if the discussion has converged, False otherwise.
        """
        if len(thread.messages) < 2:
            return False

        last_msg = thread.messages[-1]
        last_content_upper = last_msg.content.upper()

        # Check explicit convergence signals in the last message
        for signal in CONVERGENCE_SIGNALS:
            if signal.upper() in last_content_upper:
                logger.debug(
                    "Convergence detected: signal '%s' in last message", signal
                )
                return True

        # Check if last two messages from the same agent are highly similar
        # (indicates the agent is repeating itself — no new information)
        same_sender_messages = [
            m for m in thread.messages if m.sender_agent_id == last_msg.sender_agent_id
        ]
        if len(same_sender_messages) >= 2:
            prev_msg = same_sender_messages[-2]
            similarity = self._calculate_text_similarity(
                prev_msg.content, last_msg.content
            )
            if similarity > thread.config.convergence_threshold:
                logger.debug(
                    "Convergence detected: similarity %.2f > threshold %.2f",
                    similarity,
                    thread.config.convergence_threshold,
                )
                return True

        return False

    def check_budget(self, thread: DiscussionThread) -> bool:
        """Check whether the token budget is still available.

        Args:
            thread: The discussion thread.

        Returns:
            True if budget remains, False if exhausted.
        """
        has_budget = thread.total_tokens_used < thread.config.token_budget
        if not has_budget:
            logger.info(
                "Token budget exhausted for thread %s (%d/%d)",
                thread.thread_id[:8],
                thread.total_tokens_used,
                thread.config.token_budget,
            )
        return has_budget

    # ------------------------------------------------------------------
    # Conclusion & Summary
    # ------------------------------------------------------------------

    def summarize_conclusion(self, thread: DiscussionThread) -> str:
        """Generate a conclusion summary for the completed discussion.

        Attempts to use LLM summarization; falls back to a structured
        summary of the last message and participant list.

        Args:
            thread: The completed discussion thread.

        Returns:
            The conclusion summary text.
        """
        if not thread.messages:
            conclusion = "No messages in discussion."
            thread.conclusion = conclusion
            thread.completed_at = time.time()
            return conclusion

        # Build summary prompt
        all_content = "\n".join(
            f"[Round {m.round_num}] {m.sender_agent_id[:8]}: {m.content}"
            for m in thread.messages
        )
        prompt = (
            "Summarize the following inter-agent discussion. "
            "Provide the key points and final conclusion in 2-3 sentences:\n\n"
            f"{all_content}"
        )

        # Attempt LLM summarization (placeholder)
        conclusion = self._call_llm_for_summary(prompt)

        thread.conclusion = conclusion
        thread.completed_at = time.time()

        logger.info(
            "Discussion %s concluded (status=%s, rounds=%d, tokens=%d)",
            thread.thread_id[:8],
            thread.status.value,
            thread.current_round,
            thread.total_tokens_used,
        )
        return conclusion

    # ------------------------------------------------------------------
    # Full Discussion Loop
    # ------------------------------------------------------------------

    def run_discussion(
        self, thread: DiscussionThread, initial_content: str,
        *, on_round_complete=None,
    ) -> DiscussionThread:
        """Run the full discussion loop until convergence, timeout, or budget exhaustion.

        Steps:
        1. Start discussion with initial content.
        2. Loop up to max_rounds:
           a. Check budget — break if exhausted.
           b. Execute a round.
           c. Call on_round_complete callback if provided.
           d. Check convergence — break if converged.
        3. If loop ends without convergence, mark as TIMEOUT.
        4. Summarize conclusion.

        Args:
            thread: The discussion thread to execute.
            initial_content: The content that initiated the discussion.
            on_round_complete: Optional callback(thread) invoked after each round.

        Returns:
            The completed discussion thread.
        """
        thread = self.start_discussion(thread, initial_content)

        max_rounds = thread.config.max_rounds

        for round_num in range(max_rounds):
            # Check budget before executing round
            if not self.check_budget(thread):
                thread.status = DiscussionStatus.BUDGET_EXHAUSTED
                self.summarize_conclusion(thread)
                logger.info(
                    "Discussion %s ended: budget exhausted at round %d",
                    thread.thread_id[:8],
                    round_num,
                )
                break

            # Execute round
            thread = self.execute_round(thread)

            # Notify caller of round completion (for card updates)
            if on_round_complete is not None:
                try:
                    on_round_complete(thread)
                except Exception as cb_exc:
                    logger.debug("on_round_complete callback error: %s", str(cb_exc))

            # Check convergence after round
            if self.check_convergence(thread):
                thread.status = DiscussionStatus.CONVERGED
                self.summarize_conclusion(thread)
                logger.info(
                    "Discussion %s converged at round %d",
                    thread.thread_id[:8],
                    round_num + 1,
                )
                break
        else:
            # Loop completed without convergence
            thread.status = DiscussionStatus.TIMEOUT
            self.summarize_conclusion(thread)
            logger.info(
                "Discussion %s timed out after %d rounds",
                thread.thread_id[:8],
                max_rounds,
            )

        return thread

    # ------------------------------------------------------------------
    # Stop Discussion
    # ------------------------------------------------------------------

    def stop_discussion(self, thread: DiscussionThread) -> DiscussionThread:
        """Manually stop an active discussion.

        Sets status to MANUALLY_STOPPED, timestamps completion, and
        summarizes whatever conclusion is available.

        Args:
            thread: The discussion thread to stop.

        Returns:
            The stopped thread with conclusion.
        """
        thread.status = DiscussionStatus.MANUALLY_STOPPED
        thread.completed_at = time.time()
        self.summarize_conclusion(thread)

        logger.info(
            "Discussion %s manually stopped (rounds=%d, tokens=%d)",
            thread.thread_id[:8],
            thread.current_round,
            thread.total_tokens_used,
        )
        return thread

    # ------------------------------------------------------------------
    # Placeholder / Integration Methods
    # ------------------------------------------------------------------

    def _execute_agent_turn(self, agent_id: str, prompt: str, thread=None) -> str:
        """Execute a single agent turn in the discussion.

        Uses the engine's public API to build a prompt (with memory context)
        and run an ACP session for the target agent.

        Args:
            agent_id: The ID of the agent to execute.
            prompt: The discussion prompt to send.
            thread: Optional DiscussionThread for timeout calculation and cancellation.

        Returns:
            The agent's response text.
        """
        fallback = f"[placeholder response from {agent_id[:8]}]"

        if self._engine is None:
            logger.debug(
                "No engine available; returning placeholder for agent %s",
                agent_id[:8],
            )
            self._notify_unavailable(agent_id, "engine not available")
            return fallback

        # Calculate per-turn timeout from discussion config
        turn_timeout = 60  # default fallback
        if thread and hasattr(thread, 'config') and thread.config:
            config = thread.config
            disc_timeout = getattr(config, 'discussion_timeout', None)
            max_rounds = getattr(config, 'max_rounds', None)
            if disc_timeout and max_rounds and max_rounds > 0:
                turn_timeout = disc_timeout / max_rounds
        elif self._config:
            disc_timeout = getattr(self._config, 'discussion_timeout', None)
            max_rounds = getattr(self._config, 'max_rounds', None)
            if disc_timeout and max_rounds and max_rounds > 0:
                turn_timeout = disc_timeout / max_rounds

        try:
            # Resolve agent identity via public API
            agent = self._engine.get_agent(agent_id)
            if agent is None:
                logger.warning(
                    "Agent %s not found in registry; returning placeholder",
                    agent_id[:8],
                )
                self._notify_unavailable(agent_id, "agent not found in registry")
                return fallback

            # Build full prompt with system prompt, memory, and discussion context
            # via public API (memory=None triggers auto-read)
            full_prompt = self._engine.build_agent_prompt(agent, prompt)

            # Execute via public API with calculated timeout
            response = self._engine.run_agent_session(agent, full_prompt, timeout=turn_timeout)
            if response:
                logger.debug(
                    "Agent %s responded (length=%d, timeout=%.1fs)",
                    agent_id[:8],
                    len(response),
                    turn_timeout,
                )
                return response

            logger.warning(
                "ACP session returned None for agent %s; using fallback",
                agent_id[:8],
            )
            return fallback

        except Exception as exc:
            logger.error(
                "Error executing agent turn for %s: %s",
                agent_id[:8],
                exc,
                exc_info=True,
            )
            return fallback

    def _notify_unavailable(self, agent_id: str, reason: str) -> None:
        """Notify via callback that an agent is unavailable during discussion."""
        if self._on_unavailable_notify:
            try:
                self._on_unavailable_notify(agent_id, reason)
            except Exception as exc:
                logger.debug("on_unavailable_notify callback failed: %s", str(exc))

    def _call_llm_for_summary(self, prompt: str) -> str:
        """Call LLM to generate a discussion summary.

        Creates a temporary one-shot ACP session to run the summarization
        prompt, then closes the session. Falls back to a structured summary
        if the LLM call fails.

        Args:
            prompt: The summarization prompt.

        Returns:
            The summary text (falls back to structured format).
        """
        fallback = (
            "Discussion summary (auto-generated fallback): "
            "Participants exchanged perspectives. "
            "See thread messages for full details."
        )

        if self._engine is None:
            logger.debug("No engine available for LLM summary; using fallback.")
            return fallback

        try:
            from ..agent_session import create_engine_session, close_session_safely

            agent_type = getattr(self._engine, "agent_type", "coco") or "coco"
            cwd = getattr(self._engine, "root_path", ".") or "."

            session = create_engine_session(
                agent_type=agent_type,
                cwd=cwd,
                thread_id=f"slock_summary_{id(prompt) & 0xFFFF:04x}",
                auto_approve=True,
            )
            if session is None:
                logger.warning("Failed to create summary ACP session; using fallback.")
                return fallback

            try:
                result = session.send_prompt(prompt, timeout=60)
                if result and result.text:
                    return result.text
                logger.debug("LLM summary session returned empty; using fallback.")
                return fallback
            finally:
                close_session_safely(session)

        except Exception as exc:
            logger.error(
                "Error calling LLM for summary: %s", exc, exc_info=True
            )
            return fallback

    def _find_agent_by_role(self, role: str, channel_id: str = "") -> Optional[str]:
        """Find an agent ID by its role using registry public API.

        Args:
            role: The role to search for (e.g. "reviewer", "coder").
            channel_id: Optional channel_id for cross-group isolation.

        Returns:
            The agent_id if found, None otherwise.
        """
        if self._engine is not None:
            try:
                registry = getattr(self._engine, "registry", None) or getattr(self._engine, "_registry", None)
                if registry is not None and hasattr(registry, "list_agents"):
                    agents = registry.list_agents(channel_id=channel_id) if channel_id else registry.list_agents()
                    for agent in agents:
                        if hasattr(agent, "role") and agent.role == role:
                            return agent.agent_id
            except Exception as exc:
                logger.debug("Error finding agent by role '%s': %s", role, str(exc))
        return None

    def _find_agent_by_name(self, name: str, channel_id: str = "") -> Optional[str]:
        """Find an agent ID by its display name using registry public API.

        Args:
            name: The agent name to search for (case-insensitive).
            channel_id: Optional channel_id for cross-group isolation.

        Returns:
            The agent_id if found, None otherwise.
        """
        if self._engine is not None:
            try:
                registry = getattr(self._engine, "registry", None) or getattr(self._engine, "_registry", None)
                if registry is not None:
                    # Try find_by_name first (dedicated API)
                    if hasattr(registry, "find_by_name"):
                        agent = registry.find_by_name(name, channel_id=channel_id) if channel_id else registry.find_by_name(name)
                        if agent:
                            return agent.agent_id
                    # Fallback: iterate list_agents
                    if hasattr(registry, "list_agents"):
                        agents = registry.list_agents(channel_id=channel_id) if channel_id else registry.list_agents()
                        for agent in agents:
                            agent_name = getattr(agent, "name", "")
                            if agent_name.lower() == name.lower():
                                return agent.agent_id
            except Exception as exc:
                logger.debug("Error finding agent by name '%s': %s", name, str(exc))
        return None

    def _calculate_text_similarity(self, text1: str, text2: str) -> float:
        """Calculate simple text similarity using Jaccard word overlap.

        Computes the ratio of shared words to total unique words between
        two text segments.

        Args:
            text1: First text segment.
            text2: Second text segment.

        Returns:
            Similarity score between 0.0 and 1.0.
        """
        if not text1 or not text2:
            return 0.0

        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())

        if not words1 and not words2:
            return 1.0
        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2

        return len(intersection) / len(union) if union else 0.0
