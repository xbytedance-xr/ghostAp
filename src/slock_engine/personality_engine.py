"""Personality Engine — transforms declared traits into behavioral directives.

Converts the cosmetic personality_traits list on AgentIdentity into concrete
LLM-injectable behavioral instructions that shape communication style,
proactiveness, and decision confidence.
"""

from __future__ import annotations

from dataclasses import dataclass

# Trait → behavioral dimension mapping.
# Keys are Chinese trait keywords from role_bootstrap._DEFAULT_TRAITS and user-defined values.
# Values are tuples of (dimension_name, delta) applied to the profile.
_TRAIT_DIMENSION_MAP: dict[str, list[tuple[str, float]]] = {
    # Coder traits
    "严谨": [("detail_orientation", 0.3), ("confidence", 0.1)],
    "注重细节": [("detail_orientation", 0.3), ("verbosity", 0.1)],
    "高效": [("verbosity", -0.2), ("confidence", 0.2)],
    # Reviewer traits
    "批判性思维": [("confidence", 0.2), ("formality", 0.1)],
    "注重质量": [("detail_orientation", 0.2), ("proactiveness", 0.1)],
    "全面": [("verbosity", 0.2), ("detail_orientation", 0.1)],
    # Writer traits
    "清晰表达": [("verbosity", 0.1), ("formality", 0.1)],
    "结构化": [("formality", 0.2), ("detail_orientation", 0.1)],
    "用户视角": [("social_warmth", 0.2), ("proactiveness", 0.1)],
    # Tester traits
    "边界思维": [("detail_orientation", 0.2), ("confidence", 0.1)],
    "全覆盖": [("detail_orientation", 0.3), ("verbosity", 0.1)],
    # Planner traits
    "全局观": [("confidence", 0.2), ("proactiveness", 0.2)],
    "结构化思维": [("formality", 0.2), ("verbosity", 0.1)],
    "优先级敏感": [("confidence", 0.1), ("proactiveness", 0.1)],
    # Architect traits
    "系统思维": [("confidence", 0.2), ("verbosity", 0.2)],
    "前瞻性": [("proactiveness", 0.3), ("confidence", 0.1)],
    "权衡利弊": [("verbosity", 0.2), ("formality", 0.1)],
    # General traits (user might define)
    "热心": [("social_warmth", 0.3), ("proactiveness", 0.2)],
    "简洁": [("verbosity", -0.3), ("formality", -0.1)],
    "幽默": [("social_warmth", 0.2), ("formality", -0.2)],
    "谨慎": [("confidence", -0.2), ("detail_orientation", 0.2)],
    "果断": [("confidence", 0.3), ("verbosity", -0.1)],
}


@dataclass
class PersonalityProfile:
    """Behavioral personality profile derived from personality_traits."""

    agent_id: str = ""
    # Core behavioral dimensions (0.0 - 1.0 scale, 0.5 = neutral)
    verbosity: float = 0.5
    formality: float = 0.5
    proactiveness: float = 0.5
    confidence: float = 0.5
    detail_orientation: float = 0.5
    social_warmth: float = 0.5

    def to_behavioral_prompt(self) -> str:
        """Generate a behavioral instruction block for injection into agent prompt.

        Returns a concise directive string that shapes the agent's communication
        style. Returns "" if all dimensions are neutral.
        """
        lines: list[str] = []

        if self.verbosity >= 0.7:
            lines.append("你倾向于详细解释推理过程和背景。")
        elif self.verbosity <= 0.3:
            lines.append("你偏好简洁回复，避免冗余。用要点列表代替长段落。")

        if self.formality >= 0.7:
            lines.append("你的表达正式、精确，使用专业术语。")
        elif self.formality <= 0.3:
            lines.append("你的表达随意自然，像聊天一样沟通。")

        if self.proactiveness >= 0.7:
            lines.append("你会主动提出观察和建议，即使没人问你。")
        elif self.proactiveness <= 0.3:
            lines.append("你只在被明确要求时才发表意见。")

        if self.confidence >= 0.7:
            lines.append("你直接表达观点，不加过多保留语气词。")
        elif self.confidence <= 0.3:
            lines.append("你常邀请他人反馈，用「可能」「也许」等表达不确定性。")

        if self.detail_orientation >= 0.7:
            lines.append("你注重边界条件和细节完整性，不遗漏异常情况。")

        if self.social_warmth >= 0.7:
            lines.append("你会肯定同事的贡献，对好的想法表示认可。")

        if not lines:
            return ""
        return "\n".join(lines)

    @classmethod
    def from_traits(cls, agent_id: str, traits: list[str]) -> "PersonalityProfile":
        """Build a profile from declared personality_traits keywords."""
        profile = cls(agent_id=agent_id)
        for trait in traits:
            trait_key = trait.strip()
            deltas = _TRAIT_DIMENSION_MAP.get(trait_key, [])
            for dim_name, delta in deltas:
                current = getattr(profile, dim_name, 0.5)
                # Clamp to [0.0, 1.0]
                setattr(profile, dim_name, max(0.0, min(1.0, current + delta)))
        return profile


class PersonalityEngine:
    """Manages personality profiles for all agents in a channel.

    Lazily computes and caches profiles. Cache is invalidated when
    agent identity changes (handled externally by clearing the engine).
    """

    def __init__(self) -> None:
        self._cache: dict[str, PersonalityProfile] = {}

    def get_profile(self, agent_id: str, traits: list[str]) -> PersonalityProfile:
        """Get or compute the personality profile for an agent."""
        if agent_id not in self._cache:
            self._cache[agent_id] = PersonalityProfile.from_traits(agent_id, traits)
        return self._cache[agent_id]

    def invalidate(self, agent_id: str) -> None:
        """Clear cached profile (call when agent traits are updated)."""
        self._cache.pop(agent_id, None)

    def invalidate_all(self) -> None:
        """Clear all cached profiles."""
        self._cache.clear()
