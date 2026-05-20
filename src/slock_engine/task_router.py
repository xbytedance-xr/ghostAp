"""Task Router — message routing and Task Claim mechanism for Slock Engine.

Implements:
- @mention routing: direct message to specific agent
- Skill-based routing: score agents by skill profile and assign to best match
- Task Claim: exclusive lock mechanism (first-come-first-served, timeout release)
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Optional

from .models import AgentIdentity, AgentStatus, SkillProfile

if TYPE_CHECKING:
    from .memory_manager import MemoryManager

logger = logging.getLogger(__name__)

# Weights for automatic task assignment scoring
_WEIGHT_SUCCESS_RATE = 0.4
_WEIGHT_SKILL_RELEVANCE = 0.35
_WEIGHT_AVAILABILITY = 0.25


class TaskClaim:
    """Exclusive lock for task claiming — thread-safe with optional file persistence.

    When persist_path is set, claim state is saved to disk on every mutation
    and loaded on construction, surviving process restarts.
    """

    def __init__(self, default_ttl: float = 3600.0, persist_path: Optional[str] = None):
        self._claims: dict[str, tuple[str, float]] = {}  # task_id -> (agent_id, claimed_at)
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._default_ttl = default_ttl
        self._persist_path = persist_path
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

    def force_assign(self, task_id: str, agent_id: str) -> None:
        """Admin override: forcefully assign task regardless of current holder."""
        with self._lock:
            self._claims[task_id] = (agent_id, time.time())
            self._persist()

    def purge_expired(self) -> int:
        """Remove all expired claims. Returns the number purged."""
        now = time.time()
        purged = 0
        with self._lock:
            expired_keys = [
                tid for tid, (_, claimed_at) in self._claims.items()
                if now - claimed_at >= self._default_ttl
            ]
            for tid in expired_keys:
                del self._claims[tid]
                purged += 1
            if purged:
                self._persist()
        return purged

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
    ):
        self._task_claim = TaskClaim(default_ttl=task_claim_ttl, persist_path=persist_path)
        self._agent_statuses: dict[str, AgentStatus] = {}
        self._skill_profiles: dict[str, list[SkillProfile]] = {}  # agent_id -> profiles
        self._skill_profile_ts: dict[str, float] = {}  # agent_id -> last_load_time
        self._memory_backend = memory_backend
        self._round_robin_index = 0
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock

    @property
    def task_claim(self) -> TaskClaim:
        return self._task_claim

    def set_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        """Update an agent's status."""
        with self._lock:
            self._agent_statuses[agent_id] = status

    def get_agent_status(self, agent_id: str) -> AgentStatus:
        """Get an agent's current status."""
        with self._lock:
            return self._agent_statuses.get(agent_id, AgentStatus.IDLE)

    def set_skill_profiles(self, agent_id: str, profiles: list[SkillProfile]) -> None:
        """Set skill profiles for an agent."""
        with self._lock:
            self._skill_profiles[agent_id] = profiles
            self._skill_profile_ts[agent_id] = time.time()

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

    def route_message(
        self,
        text: str,
        available_agents: list[AgentIdentity],
    ) -> Optional[AgentIdentity]:
        """Route a message to the most appropriate agent.

        Returns the target agent, or None if no suitable agent found.
        Only IDLE agents are considered for routing; agents in MOVING,
        RUNNING, or any other non-IDLE state are excluded.
        """
        if not available_agents:
            return None

        # Hard filter: only consider IDLE agents
        idle_agents = [
            a for a in available_agents
            if self.get_agent_status(a.agent_id) == AgentStatus.IDLE
        ]
        if not idle_agents:
            return None

        # Priority 1: @mention routing
        mentioned = self._extract_mention(text, idle_agents)
        if mentioned:
            return mentioned

        # Priority 2: Skill-based scoring for normal messages
        return self._score_and_assign(text, idle_agents)

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

    def _extract_mention(self, text: str, agents: list[AgentIdentity]) -> Optional[AgentIdentity]:
        """Extract @mention and match to an agent."""
        matches = self._FEISHU_MENTION_PATTERN.findall(text)
        matches.extend(self._MENTION_PATTERN.findall(text))
        if not matches:
            return None

        for mention in matches:
            mention_lower = re.sub(r"\s+", " ", mention).strip().lower()
            for agent in agents:
                agent_name = re.sub(r"\s+", " ", agent.name).strip().lower()
                if agent_name == mention_lower:
                    return agent
        return None

    def _score_and_assign(
        self,
        text: str,
        agents: list[AgentIdentity],
    ) -> Optional[AgentIdentity]:
        """Score agents by skill relevance and availability, return best match."""
        scored = self._score_agents(text, agents)

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

    def _score_agents(self, text: str, agents: list[AgentIdentity]) -> list[tuple[AgentIdentity, float]]:
        """Score agents by relevance, success, and availability."""
        required_skills = self.extract_skill_keywords(text)
        scored: list[tuple[AgentIdentity, float]] = []

        for agent in agents:
            status = self.get_agent_status(agent.agent_id)
            availability = 1.0 if status == AgentStatus.IDLE else 0.3
            self._ensure_skill_profiles_loaded(agent.agent_id)

            with self._lock:
                profiles = self._skill_profiles.get(agent.agent_id, [])

            relevance = self._calculate_relevance(profiles, required_skills)
            avg_success = 0.5  # default
            if profiles:
                avg_success = sum(p.success_rate for p in profiles) / len(profiles) / 100.0

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
        }

        text_lower = text.lower()
        matched: list[str] = []
        for skill, keywords in skill_keywords.items():
            if any(kw in text_lower for kw in keywords):
                matched.append(skill)

        return matched if matched else ["code"]  # default to code

    def _extract_skill_keywords(self, text: str) -> list[str]:
        """Backward-compatible alias for existing direct unit tests."""
        return self.extract_skill_keywords(text)

    def _calculate_relevance(self, profiles: list[SkillProfile], required_skills: list[str]) -> float:
        """Calculate skill relevance score (0.0 - 1.0)."""
        if not required_skills:
            return 0.5

        total = 0.0
        for skill in required_skills:
            match = next((p for p in profiles if p.tag == skill), None)
            if match:
                total += match.success_rate / 100.0
            else:
                total += 0.3  # partial credit for uncharted skills

        return total / len(required_skills)
