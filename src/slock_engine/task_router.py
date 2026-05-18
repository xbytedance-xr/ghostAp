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
from typing import Optional

from .models import AgentIdentity, AgentStatus, SkillProfile, SlockTask, TaskStatus

logger = logging.getLogger(__name__)

# Weights for automatic task assignment scoring
_WEIGHT_SUCCESS_RATE = 0.4
_WEIGHT_SKILL_RELEVANCE = 0.35
_WEIGHT_AVAILABILITY = 0.25


class TaskClaim:
    """Exclusive lock for task claiming — process-internal, thread-safe."""

    def __init__(self, default_ttl: float = 3600.0):
        self._claims: dict[str, tuple[str, float]] = {}  # task_id -> (agent_id, claimed_at)
        self._lock = threading.Lock()  # leaf lock: never held while acquiring a LockLevel lock
        self._default_ttl = default_ttl

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
                    return True
                # Still held by another agent
                if holder_id != agent_id:
                    return False
                # Same agent re-claiming — idempotent
                return True
            # Not claimed — claim it
            self._claims[task_id] = (agent_id, now)
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
                return None
            return holder_id

    def is_claimed(self, task_id: str) -> bool:
        """Check if a task is currently claimed (not expired)."""
        return self.get_holder(task_id) is not None

    def force_assign(self, task_id: str, agent_id: str) -> None:
        """Admin override: forcefully assign task regardless of current holder."""
        with self._lock:
            self._claims[task_id] = (agent_id, time.time())


class TaskRouter:
    """Routes incoming messages to the appropriate agent.

    Routing priority:
    1. @mention → direct to named agent
    2. Task messages → broadcast to all for claim competition
    3. Normal messages → skill-based scoring to best-match agent
    """

    # Pattern to match @AgentName mentions
    _MENTION_PATTERN = re.compile(r"@([\w\-]+)", re.UNICODE)

    def __init__(self, task_claim_ttl: float = 3600.0):
        self._task_claim = TaskClaim(default_ttl=task_claim_ttl)
        self._agent_statuses: dict[str, AgentStatus] = {}
        self._skill_profiles: dict[str, list[SkillProfile]] = {}  # agent_id -> profiles
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

    def route_message(
        self,
        text: str,
        available_agents: list[AgentIdentity],
    ) -> Optional[AgentIdentity]:
        """Route a message to the most appropriate agent.

        Returns the target agent, or None if no suitable agent found.
        """
        if not available_agents:
            return None

        # Priority 1: @mention routing
        mentioned = self._extract_mention(text, available_agents)
        if mentioned:
            return mentioned

        # Priority 2: Skill-based scoring for normal messages
        return self._score_and_assign(text, available_agents)

    def _extract_mention(self, text: str, agents: list[AgentIdentity]) -> Optional[AgentIdentity]:
        """Extract @mention and match to an agent."""
        matches = self._MENTION_PATTERN.findall(text)
        if not matches:
            return None

        for mention in matches:
            mention_lower = mention.lower()
            for agent in agents:
                if agent.name.lower() == mention_lower:
                    return agent
        return None

    def _score_and_assign(
        self,
        text: str,
        agents: list[AgentIdentity],
    ) -> Optional[AgentIdentity]:
        """Score agents by skill relevance and availability, return best match."""
        # Extract skill keywords from message
        required_skills = self._extract_skill_keywords(text)

        best_agent: Optional[AgentIdentity] = None
        best_score = -1.0

        for agent in agents:
            status = self.get_agent_status(agent.agent_id)
            # Prefer idle agents
            availability = 1.0 if status == AgentStatus.IDLE else 0.3

            # Get skill profiles
            with self._lock:
                profiles = self._skill_profiles.get(agent.agent_id, [])

            # Calculate relevance
            relevance = self._calculate_relevance(profiles, required_skills)

            # Calculate average success rate
            avg_success = 0.5  # default
            if profiles:
                avg_success = sum(p.success_rate for p in profiles) / len(profiles) / 100.0

            score = (
                avg_success * _WEIGHT_SUCCESS_RATE
                + relevance * _WEIGHT_SKILL_RELEVANCE
                + availability * _WEIGHT_AVAILABILITY
            )

            if score > best_score:
                best_score = score
                best_agent = agent

        return best_agent

    def _extract_skill_keywords(self, text: str) -> list[str]:
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
