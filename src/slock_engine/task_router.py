"""Task Router — message routing and Task Claim mechanism for Slock Engine.

Implements:
- @mention routing: direct message to specific agent
- Skill-based routing: score agents by skill profile and assign to best match
- Task Claim: exclusive lock mechanism (first-come-first-served, timeout release)
- Fallback routing: degrade to busy agents when no IDLE agent available
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from .agent_registry import _normalize_at_token
from .models import AgentIdentity, AgentStatus, SkillProfile
from .task_classifier import TaskClassifier

if TYPE_CHECKING:
    from .memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# Weights for automatic task assignment scoring
# Skill relevance is prioritized over raw success rate because a highly relevant
# agent with moderate history should beat an irrelevant agent with perfect history.
_WEIGHT_SKILL_RELEVANCE = 0.45
_WEIGHT_SUCCESS_RATE = 0.25
_WEIGHT_AVAILABILITY = 0.30

# Confidence penalty: agents with very few completed tasks get their success_rate
# discounted.  Without this, an agent completing 1/1 tasks scores 100% success,
# beating a reliable agent at 48/50 (96%).  The penalty ramps linearly from 0 to 1
# over CONFIDENCE_MIN_TASKS executions.
_CONFIDENCE_MIN_TASKS = 5


class RoutingStatus(Enum):
    """Result status from route_message."""

    ASSIGNED = "assigned"       # An idle agent was found
    QUEUE_WAIT = "queue_wait"   # All agents busy but running; caller should wait/retry
    NO_MATCH = "no_match"       # No agent scored (chitchat or no agents at all)


@dataclass
class RoutingResult:
    """Extended routing result that conveys both the selected agent and routing status."""

    status: RoutingStatus
    agent: Optional[AgentIdentity] = None
    busy_count: int = 0  # number of running agents (useful for queue estimation)


class TaskClaim:
    """Exclusive lock for task claiming — thread-safe with optional file persistence.

    When persist_path is set, claim state is saved to disk on every mutation
    and loaded on construction, surviving process restarts.
    """

    def __init__(
        self,
        default_ttl: float = 600.0,
        persist_path: Optional[str] = None,
        memory_manager: Optional["MemoryManager"] = None,
    ):
        self._claims: dict[str, tuple[str, float]] = {}  # task_id -> (agent_id, claimed_at)
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._default_ttl = default_ttl
        self._persist_path = persist_path
        self._memory_manager = memory_manager
        self._on_expired_callbacks: list[Callable] = []
        if persist_path:
            self._load_from_disk()

    def claim(self, task_id: str, agent_id: str, ttl: Optional[float] = None) -> bool:
        """Attempt to claim a task. Returns True if successful, False if already claimed."""
        now = time.time()
        effective_ttl = ttl if ttl is not None else self._default_ttl

        with self._lock:
            if task_id in self._claims:
                holder_id, claimed_at = self._claims[task_id]
                # Check if claim has expired
                if now - claimed_at >= effective_ttl:
                    # Expired — allow reclaim
                    self._claims[task_id] = (agent_id, now)
                    logger.info("Task %s reclaimed by %s (previous claim expired)", task_id, agent_id)
                    self._persist()
                    return True
                # Still held by another agent
                if holder_id != agent_id:
                    return False
                # Same agent re-claiming — idempotent
                return True
            # Not claimed — claim it
            self._claims[task_id] = (agent_id, now)
            self._persist()
            return True

    def release(self, task_id: str, agent_id: Optional[str] = None) -> bool:
        """Release a task claim. If agent_id is given, only release if held by that agent."""
        with self._lock:
            if task_id not in self._claims:
                return False
            if agent_id is not None:
                holder_id, _ = self._claims[task_id]
                if holder_id != agent_id:
                    return False
            del self._claims[task_id]
            self._persist()
            return True

    def get_holder(self, task_id: str) -> Optional[str]:
        """Return the agent_id holding this task, or None."""
        with self._lock:
            if task_id not in self._claims:
                return None
            holder_id, claimed_at = self._claims[task_id]
            # Check expiry
            if time.time() - claimed_at >= self._default_ttl:
                del self._claims[task_id]
                self._persist()
                return None
            return holder_id

    def is_claimed(self, task_id: str) -> bool:
        """Check if a task is currently claimed (not expired)."""
        return self.get_holder(task_id) is not None

    def force_assign(self, task_id: str, agent_id: str, operator_id: str = "") -> None:
        """Admin override: forcefully assign task regardless of current holder."""
        with self._lock:
            prev_agent_id = self._claims.get(task_id, ("", 0.0))[0]
            self._claims[task_id] = (agent_id, time.time())
            self._persist()
        if operator_id and self._memory_manager and hasattr(self._memory_manager, "append_audit_log"):
            self._memory_manager.append_audit_log(
                operator_id=operator_id,
                action="force_assign",
                target=task_id,
                detail=f"prev={prev_agent_id or 'none'} new={agent_id}",
            )

    def purge_expired(self) -> int:
        """Remove all expired claims. Returns the number purged."""
        now = time.time()
        purged = 0
        expired_tasks: list[tuple[str, str]] = []
        with self._lock:
            expired_keys = [
                tid for tid, (_, claimed_at) in self._claims.items()
                if now - claimed_at >= self._default_ttl
            ]
            for tid in expired_keys:
                agent_id = self._claims[tid][0]
                expired_tasks.append((tid, agent_id))
                del self._claims[tid]
                purged += 1
            if purged:
                self._persist()

        # Notify callbacks outside lock
        for tid, agent_id in expired_tasks:
            for cb in self._on_expired_callbacks:
                try:
                    cb(tid, agent_id)
                except Exception:
                    logger.warning("on_expired callback failed for task %s", tid)

        return purged

    def renew(self, task_id: str, agent_id: str) -> bool:
        """Heartbeat: renew a claim's timestamp to prevent expiry.

        Returns True if the claim was successfully renewed, False if the claim
        doesn't exist or is held by a different agent.
        """
        with self._lock:
            if task_id not in self._claims:
                return False
            holder_id, _ = self._claims[task_id]
            if holder_id != agent_id:
                return False
            self._claims[task_id] = (agent_id, time.time())
            self._persist()
            return True

    def on_expired(self, callback: Callable[[str, str], None]) -> None:
        """Register a callback invoked when a claim expires during purge_expired.

        Callback signature: (task_id: str, agent_id: str) -> None
        """
        self._on_expired_callbacks.append(callback)

    def get_active_claim_count(self, agent_id: Optional[str] = None) -> int:
        """Return count of active (non-expired) claims, optionally filtered by agent_id.

        Args:
            agent_id: If provided, count only claims held by this agent.
                      If None, count all active claims.

        Returns:
            Number of active claims.
        """
        now = time.time()
        with self._lock:
            if agent_id is None:
                return sum(
                    1 for _, (_, claimed_at) in self._claims.items()
                    if now - claimed_at < self._default_ttl
                )
            return sum(
                1 for _, (aid, claimed_at) in self._claims.items()
                if aid == agent_id and now - claimed_at < self._default_ttl
            )

    def get_claims_snapshot(self) -> dict[str, tuple[str, float]]:
        """Return a point-in-time copy of active claims for lock-free iteration."""
        now = time.time()
        with self._lock:
            expired_keys = [
                tid for tid, (_, claimed_at) in self._claims.items()
                if now - claimed_at >= self._default_ttl
            ]
            for tid in expired_keys:
                del self._claims[tid]
            if expired_keys:
                self._persist()
            return dict(self._claims)

    def _persist(self) -> None:
        """Write claims to disk (must be called under self._lock)."""
        if not self._persist_path:
            return
        import json
        import os

        try:
            os.makedirs(os.path.dirname(self._persist_path), exist_ok=True)
            tmp_path = self._persist_path + ".tmp"
            data = {
                tid: {"agent_id": aid, "claimed_at": cat}
                for tid, (aid, cat) in self._claims.items()
            }
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, self._persist_path)
        except OSError as e:
            logger.warning("TaskClaim persist failed: %s", str(e))

    def _load_from_disk(self) -> None:
        """Load claims from disk, pruning expired entries."""
        import json
        import os

        if not self._persist_path or not os.path.isfile(self._persist_path):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("TaskClaim load failed: %s", str(e))
            return

        now = time.time()
        for tid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            agent_id = entry.get("agent_id", "")
            claimed_at = entry.get("claimed_at", 0.0)
            if now - claimed_at < self._default_ttl:
                self._claims[tid] = (agent_id, claimed_at)


class TaskRouter:
    """Routes incoming messages to the appropriate agent.

    Routing priority:
    1. @mention → direct to named agent
    2. Task messages → broadcast to all for claim competition
    3. Normal messages → skill-based scoring to best-match agent
    """

    # Patterns to match plain @AgentName and Feishu XML-ish mention markup.
    _MENTION_PATTERN = re.compile(r"@([\w\-\u4e00-\u9fff]+)", re.UNICODE)
    _FEISHU_MENTION_PATTERN = re.compile(r"<at\b[^>]*>(.*?)</at>", re.IGNORECASE | re.DOTALL)

    _SKILL_PROFILE_TTL: float = 60.0  # seconds before cached profiles are considered stale

    def __init__(
        self,
        task_claim_ttl: float = 3600.0,
        persist_path: Optional[str] = None,
        memory_backend: Optional["MemoryManager"] = None,
        engine_status_getter: Optional[object] = None,
        session_affinity_window: float = 120.0,
    ):
        self._task_claim = TaskClaim(default_ttl=task_claim_ttl, persist_path=persist_path)
        # Lock hierarchy: engine._lock → router._lock (never acquire engine._lock while holding router._lock)
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._skill_profiles: dict[str, list[SkillProfile]] = {}  # agent_id -> profiles
        self._skill_profile_ts: dict[str, float] = {}  # agent_id -> last_load_time
        self._memory_backend = memory_backend
        self._round_robin_index = 0
        # Agent status is read from the engine via this getter (single source of truth).
        # Signature: (agent_id: str) -> AgentStatus
        self._engine_status_getter = engine_status_getter
        # Fallback status dict for standalone/test usage (when no engine_status_getter)
        self._fallback_statuses: dict[str, AgentStatus] = {}
        # Session affinity: sender_id → (agent_id, last_routed_at)
        # Consecutive messages from the same user within the affinity window
        # are preferentially routed to the same agent for multi-turn continuity.
        self._session_affinity: dict[str, tuple[str, float]] = {}
        self._session_affinity_window = session_affinity_window

    @property
    def task_claim(self) -> TaskClaim:
        return self._task_claim

    def get_agent_status(self, agent_id: str) -> AgentStatus:
        """Get an agent's current status from the engine (single source of truth).

        In production, delegates to engine.get_agent_status() via the getter.
        In standalone/test mode (no getter), reads from internal fallback dict.
        """
        if self._engine_status_getter is not None:
            return self._engine_status_getter(agent_id)
        return self._fallback_statuses.get(agent_id, AgentStatus.IDLE)

    def set_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        """Set agent status in fallback dict (test/standalone mode only).

        In production, status is managed exclusively by the engine.
        This method exists for backward compatibility with standalone tests.
        """
        self._fallback_statuses[agent_id] = status

    def set_skill_profiles(self, agent_id: str, profiles: list[SkillProfile]) -> None:
        """Set skill profiles for an agent."""
        with self._lock:
            self._skill_profiles[agent_id] = profiles
            self._skill_profile_ts[agent_id] = time.time()

    def get_skill_profiles(self, agent_id: str) -> list[SkillProfile]:
        """Get skill profiles for an agent (lazy-loads from memory if needed)."""
        self._ensure_skill_profiles_loaded(agent_id)
        with self._lock:
            return list(self._skill_profiles.get(agent_id, []))

    def _ensure_skill_profiles_loaded(self, agent_id: str) -> None:
        """Lazy-load skill profiles from memory backend with TTL caching.

        Only reads from disk if the memory backend is configured and the cached
        profiles are missing or older than _SKILL_PROFILE_TTL.
        """
        if self._memory_backend is None:
            return
        now = time.time()
        with self._lock:
            last_ts = self._skill_profile_ts.get(agent_id, 0.0)
            if now - last_ts < self._SKILL_PROFILE_TTL:
                return  # cache is fresh
        # Read outside lock to avoid holding it during disk I/O
        profiles = self._memory_backend.read_skill_profiles(agent_id)
        with self._lock:
            self._skill_profiles[agent_id] = profiles
            self._skill_profile_ts[agent_id] = now

    # ------------------------------------------------------------------
    # CHITCHAT filter — prevents casual/noise messages from reaching agents.
    # Design: explicit blacklist patterns + CJK whitelist rule.
    # Short messages containing CJK ideographs (verbs/nouns) are NOT filtered,
    # e.g. "修bug", "写测试", "部署", "加日志" are valid tasks.
    # ------------------------------------------------------------------

    _CHITCHAT_PATTERNS: tuple[re.Pattern, ...] = (
        re.compile(r"^(你好|嗨|hi|hello|hey|早上好|晚上好|下午好|早安|晚安)[!！。.~]*$", re.IGNORECASE),
        re.compile(r"^(谢谢|thanks|thank\s*you|thx|ok|好的|收到了?|了解|明白|嗯|对)[!！。.~]*$", re.IGNORECASE),
        re.compile(r"^(哈哈|嘿嘿|呵呵|lol|haha|😂|👍|🙏|666|nb|yes|no|k|y|n)[!！。.~]*$", re.IGNORECASE),
    )

    # Regex to detect CJK Unified Ideographs (Chinese characters)
    _CJK_PATTERN: re.Pattern = re.compile(r"[\u4e00-\u9fff]")

    # Pure punctuation/emoji pattern — messages consisting only of these are chitchat
    # Matches strings that contain NO word characters and NO CJK characters
    _PURE_PUNCT_EMOJI: re.Pattern = re.compile(
        r"^[^\w\u4e00-\u9fff]+$",
        re.UNICODE,
    )

    def _is_chitchat(self, text: str) -> bool:
        """Return True if message is casual chitchat that should not be routed to agents.

        Delegates to the unified TaskClassifier to ensure consistent behavior
        between dispatcher auto-activation and router message filtering.
        """
        return TaskClassifier.is_chitchat(text)

    def route_message(
        self,
        text: str,
        available_agents: list[AgentIdentity],
        *,
        skip_chitchat: bool = False,
    ) -> Optional[AgentIdentity]:
        """Route a message to the most appropriate agent.

        .. deprecated::
            Use :meth:`route_message_with_fallback` instead for consistent fallback semantics.

        Returns the target agent, or None if no suitable agent found.
        Only IDLE agents are considered for routing; agents in MOVING,
        RUNNING, or any other non-IDLE state are excluded.
        CHITCHAT messages are filtered out to prevent skill profile pollution.
        """
        if not available_agents:
            return None

        # Priority 1: @mention routing. Mentions and explicit force-prefixes are
        # intentional routing signals and should bypass casual-chat filtering.
        mentioned = self._extract_mention(text, available_agents)
        force_route = bool((text or "").strip().startswith("!"))

        # Task 18: CHITCHAT filter — prevent casual messages from reaching agents
        if not skip_chitchat and not mentioned and not force_route and self._is_chitchat(text):
            logger.debug("Message filtered as CHITCHAT, not routing to agents: %s", text[:50])
            return None

        # Hard filter: only consider IDLE agents
        idle_agents = [
            a for a in available_agents
            if self.get_agent_status(a.agent_id) == AgentStatus.IDLE
        ]
        if not idle_agents:
            return None

        # Priority 1: @mention routing
        mentioned = mentioned or self._extract_mention(text, idle_agents)
        if mentioned:
            return mentioned

        # Priority 2: Skill-based scoring for normal messages
        return self._score_and_assign(text, idle_agents)

    def update_skill_profile_for_task(
        self,
        agent_id: str,
        text: str,
        memory_backend: "MemoryManager",
        *,
        quality_score: float = 100.0,
    ):
        """Update an agent skill profile for a completed task, excluding chitchat."""
        if self._is_chitchat(text):
            logger.debug("Skipping skill profile update for CHITCHAT: %s", text[:50])
            return None
        skill_tags = self.extract_skill_keywords(text)
        profiles = memory_backend.record_skill_feedback(
            agent_id,
            skill_tags,
            quality_score=quality_score,
        )
        self.set_skill_profiles(agent_id, profiles)
        return profiles

    def route_message_with_fallback(
        self,
        text: str,
        available_agents: list[AgentIdentity],
        *,
        sender_id: str = "",
    ) -> RoutingResult:
        """Route with fallback: if no IDLE agent, degrade to busy agents or queue.

        Args:
            text: The message text to route.
            available_agents: All agents in the channel.
            sender_id: Optional sender identifier for session affinity routing.

        Returns a RoutingResult with status indicating whether assignment succeeded,
        or whether the caller should wait (QUEUE_WAIT) or give up (NO_MATCH).
        """
        if not available_agents:
            return RoutingResult(status=RoutingStatus.NO_MATCH)

        # CHITCHAT filter
        if self._is_chitchat(text):
            logger.debug("Message filtered as CHITCHAT, not routing: %s", text[:50])
            return RoutingResult(status=RoutingStatus.NO_MATCH)

        # Try IDLE agents first (normal path)
        idle_agents = [
            a for a in available_agents
            if self.get_agent_status(a.agent_id) == AgentStatus.IDLE
        ]
        if idle_agents:
            mentioned = self._extract_mention(text, idle_agents)
            if mentioned:
                self._record_affinity(sender_id, mentioned.agent_id)
                return RoutingResult(status=RoutingStatus.ASSIGNED, agent=mentioned)

            # Session affinity: prefer the agent that last served this sender
            if sender_id:
                current_skills = self.extract_skill_keywords(text)
                affinity_agent = self._get_affinity_agent(sender_id, idle_agents, current_skills)
                if affinity_agent:
                    self._record_affinity(sender_id, affinity_agent.agent_id, current_skills)
                    return RoutingResult(status=RoutingStatus.ASSIGNED, agent=affinity_agent)

            assigned = self._score_and_assign(text, idle_agents)
            if assigned:
                self._record_affinity(sender_id, assigned.agent_id, self.extract_skill_keywords(text))
                return RoutingResult(status=RoutingStatus.ASSIGNED, agent=assigned)

        # Fallback: check for RUNNING agents → queue wait signal
        running_agents = [
            a for a in available_agents
            if self.get_agent_status(a.agent_id) == AgentStatus.RUNNING
        ]
        if running_agents:
            # Degrade: pick highest-scored non-IDLE agent as fallback candidate
            scored = self._score_agents(text, available_agents)
            fallback_agent = scored[0][0] if scored else None
            return RoutingResult(
                status=RoutingStatus.QUEUE_WAIT,
                agent=fallback_agent,
                busy_count=len(running_agents),
            )

        # All agents in abnormal states — no match
        return RoutingResult(status=RoutingStatus.NO_MATCH)

    def rank_agents_for_claim(
        self,
        text: str,
        available_agents: list[AgentIdentity],
    ) -> list[AgentIdentity]:
        """Return all eligible agents ordered for claim competition.

        The first entry is the preferred claimant, but callers should offer the
        task to subsequent entries when an earlier claim fails.
        """
        if not available_agents:
            return []

        idle_agents = [
            a for a in available_agents
            if self.get_agent_status(a.agent_id) == AgentStatus.IDLE
        ]
        if not idle_agents:
            return []

        mentioned = self._extract_mention(text, idle_agents)
        scored = self._score_agents(text, idle_agents)
        ordered = [agent for agent, _score in scored]
        if mentioned is None:
            return ordered
        return [mentioned] + [agent for agent in ordered if agent.agent_id != mentioned.agent_id]

    # ------------------------------------------------------------------
    # Session Affinity: multi-turn continuity
    # ------------------------------------------------------------------

    def _record_affinity(self, sender_id: str, agent_id: str, skill_keywords: list[str] | None = None) -> None:
        """Record that sender was routed to agent (for affinity window)."""
        if not sender_id:
            return
        self._session_affinity[sender_id] = (agent_id, time.time(), skill_keywords or [])

    def _get_affinity_agent(
        self, sender_id: str, idle_agents: list[AgentIdentity], current_skills: list[str] | None = None
    ) -> Optional[AgentIdentity]:
        """Return the affinity agent if still IDLE, within time window, and topic unchanged.

        Topic-aware: if the current message's skill keywords differ significantly from
        the last routed message, the affinity is broken to allow re-routing to a more
        relevant agent. This prevents a coder from handling documentation questions
        just because they handled the previous code question.
        """
        if not sender_id:
            return None
        entry = self._session_affinity.get(sender_id)
        if entry is None:
            return None
        # Unpack (supports both old 2-tuple and new 3-tuple format)
        if len(entry) == 3:
            agent_id, last_ts, prev_skills = entry
        else:
            agent_id, last_ts = entry[0], entry[1]
            prev_skills = []
        if time.time() - last_ts > self._session_affinity_window:
            # Expired — remove stale entry
            del self._session_affinity[sender_id]
            return None

        # Topic change detection: if skill keywords changed, break affinity
        if current_skills and prev_skills:
            # If no overlap between previous and current skills, topic changed
            overlap = set(current_skills) & set(prev_skills)
            if not overlap and "general" not in current_skills:
                logger.debug(
                    "Session affinity broken (topic change): sender=%s prev_skills=%s new_skills=%s",
                    sender_id, prev_skills, current_skills,
                )
                del self._session_affinity[sender_id]
                return None

        # Check if agent is still idle
        for agent in idle_agents:
            if agent.agent_id == agent_id:
                logger.debug(
                    "Session affinity hit: sender=%s → agent=%s (%.1fs ago)",
                    sender_id, agent_id, time.time() - last_ts,
                )
                return agent
        return None

    def purge_stale_affinity(self) -> int:
        """Remove expired affinity entries. Returns count removed."""
        now = time.time()
        stale = [
            sid for sid, (_, ts) in self._session_affinity.items()
            if now - ts > self._session_affinity_window
        ]
        for sid in stale:
            del self._session_affinity[sid]
        return len(stale)

    def _extract_mention(self, text: str, agents: list[AgentIdentity]) -> Optional[AgentIdentity]:
        """Extract @mention and match to an agent.

        Resolution order: ``<at>...</at>`` markup inner text, then plain
        ``@token`` matches. Each candidate is normalized via
        ``_normalize_at_token`` and compared against agent.name and agent_id,
        keeping in sync with ``AgentRegistry.find_by_at_token``.
        """
        matches = self._FEISHU_MENTION_PATTERN.findall(text)
        matches.extend(self._MENTION_PATTERN.findall(text))
        if not matches:
            return None

        for mention in matches:
            normalized = _normalize_at_token(mention)
            if not normalized:
                continue
            for agent in agents:
                if _normalize_at_token(agent.name) == normalized:
                    return agent
                if agent.agent_id.lower() == normalized:
                    return agent
        return None

    def _score_and_assign(
        self,
        text: str,
        agents: list[AgentIdentity],
    ) -> Optional[AgentIdentity]:
        """Score agents by skill relevance and availability, return best match.

        Applies keyword-based pre-filter first to exclude obviously irrelevant agents
        before running full scoring (Slock Insight #4).
        """
        from ..config import get_settings
        settings = get_settings()

        candidates = agents
        if settings.slock_semantic_prefilter_enabled and len(agents) > 1:
            candidates = self._keyword_prefilter(text, agents)
            if not candidates:
                candidates = agents  # fallback if filter is too aggressive

        scored = self._score_agents(text, candidates)

        if not scored:
            return None

        best_score = scored[0][1]
        tied = [agent for agent, score in scored if abs(score - best_score) < 1e-9]
        if len(tied) == 1:
            return tied[0]

        with self._lock:
            selected = tied[self._round_robin_index % len(tied)]
            self._round_robin_index += 1
        return selected

    def _keyword_prefilter(
        self,
        text: str,
        agents: list[AgentIdentity],
    ) -> list[AgentIdentity]:
        """Quick keyword-based filter: exclude agents whose role is obviously irrelevant.

        Uses keyword matching between the task text and the agent's role/personality.
        Only agents that have at least minimal overlap pass through. Agents without
        a defined role always pass (generic agents should not be filtered out).
        """
        text_lower = text.lower()
        required_skills = self.extract_skill_keywords(text)
        passed: list[AgentIdentity] = []

        for agent in agents:
            # Agents without explicit role always pass
            role_text = (getattr(agent, "role", "") or "").lower()
            if not role_text:
                passed.append(agent)
                continue

            # Check direct keyword overlap between role and task text
            role_words = set(re.split(r"[\s,;/|]+", role_text))
            task_words = set(re.split(r"[\s,;/|]+", text_lower))

            # Direct word overlap
            if role_words & task_words:
                passed.append(agent)
                continue

            # Check if any required_skill tag appears in the role description
            skill_match = any(skill in role_text for skill in required_skills)
            if skill_match:
                passed.append(agent)
                continue

            # Check personality traits overlap
            traits = getattr(agent, "personality_traits", []) or []
            trait_text = " ".join(t.lower() for t in traits)
            if any(skill in trait_text for skill in required_skills):
                passed.append(agent)
                continue

            # Check cached skill profiles for any non-zero relevance
            self._ensure_skill_profiles_loaded(agent.agent_id)
            with self._lock:
                profiles = self._skill_profiles.get(agent.agent_id, [])
            if profiles:
                has_relevant = any(
                    p.tag in required_skills and p.success_rate > 0
                    for p in profiles
                )
                if has_relevant:
                    passed.append(agent)
                    continue

            logger.debug(
                "Keyword prefilter: agent %s (role=%s) skipped for task: %s",
                agent.agent_id, role_text[:30], text[:50],
            )

        return passed

    def _score_agents(self, text: str, agents: list[AgentIdentity]) -> list[tuple[AgentIdentity, float]]:
        """Score agents by relevance, success, and availability."""
        required_skills = self.extract_skill_keywords(text)
        scored: list[tuple[AgentIdentity, float]] = []

        for agent in agents:
            status = self.get_agent_status(agent.agent_id)
            if status != AgentStatus.IDLE:
                availability = 0.3
            else:
                # Soft availability score based on current task load (0.5-1.0)
                active_claims = self._task_claim.get_active_claim_count(agent.agent_id)
                availability = max(0.5, 1.0 - active_claims * 0.1)
            self._ensure_skill_profiles_loaded(agent.agent_id)

            with self._lock:
                profiles = self._skill_profiles.get(agent.agent_id, [])

            relevance = self._calculate_relevance(profiles, required_skills)
            avg_success = 0.5  # default
            if profiles:
                raw_success = sum(p.success_rate for p in profiles) / len(profiles) / 100.0
                # Apply confidence penalty: agents with few tasks get discounted
                total_tasks = sum(getattr(p, "total_tasks", 0) for p in profiles)
                confidence = min(1.0, total_tasks / _CONFIDENCE_MIN_TASKS)
                # Blend toward 0.5 (neutral) when confidence is low
                avg_success = 0.5 + (raw_success - 0.5) * confidence

            score = (
                avg_success * _WEIGHT_SUCCESS_RATE
                + relevance * _WEIGHT_SKILL_RELEVANCE
                + availability * _WEIGHT_AVAILABILITY
            )
            scored.append((agent, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored

    def extract_skill_keywords(self, text: str) -> list[str]:
        """Extract skill-related keywords from message text."""
        skill_keywords = {
            "code": ["code", "implement", "function", "class", "bug", "fix", "编码", "实现", "修复"],
            "review": ["review", "check", "audit", "审查", "检查"],
            "docs": ["document", "doc", "write", "readme", "文档", "撰写"],
            "test": ["test", "coverage", "assert", "测试", "用例"],
            "design": ["design", "architecture", "pattern", "设计", "架构"],
            "deploy": ["deploy", "release", "ci", "cd", "部署", "发布"],
            "debug": ["debug", "troubleshoot", "root cause", "timeout", "排查", "故障", "定位", "根因"],
        }

        text_lower = text.lower()
        matched: list[str] = []
        for skill, keywords in skill_keywords.items():
            if any(kw in text_lower for kw in keywords):
                matched.append(skill)

        return matched if matched else ["general"]  # default to general (no skill inference)

    def _extract_skill_keywords(self, text: str) -> list[str]:
        """Backward-compatible alias for existing direct unit tests."""
        return self.extract_skill_keywords(text)

    def _calculate_relevance(self, profiles: list[SkillProfile], required_skills: list[str]) -> float:
        """Calculate skill relevance score (0.0 - 1.0) with time decay.

        Skill profiles that haven't been exercised recently are decayed toward
        the neutral value (0.5), reflecting that expertise fades or context
        shifts over time.  Half-life is 7 days.
        """
        if not required_skills:
            return 0.5

        now = time.time()
        _HALF_LIFE_SECONDS = 7 * 24 * 3600  # 7 days

        total = 0.0
        for skill in required_skills:
            match = next((p for p in profiles if p.tag == skill), None)
            if match:
                raw_score = match.success_rate / 100.0
                # Apply time decay: blend toward 0.5 as time since last_active grows
                if match.last_active > 0:
                    elapsed = now - match.last_active
                    # Exponential decay factor: 1.0 at t=0, 0.5 at t=half_life
                    decay = 0.5 ** (elapsed / _HALF_LIFE_SECONDS)
                    # Decayed score blends toward neutral (0.5)
                    decayed_score = 0.5 + (raw_score - 0.5) * decay
                    total += decayed_score
                else:
                    total += raw_score
            else:
                total += 0.3  # partial credit for uncharted skills

        return total / len(required_skills)
