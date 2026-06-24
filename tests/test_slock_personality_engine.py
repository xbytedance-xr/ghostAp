"""Tests for personality_engine.py — behavioral style differentiation."""
from src.slock_engine.personality_engine import _TRAIT_DIMENSION_MAP, PersonalityEngine, PersonalityProfile


class TestPersonalityProfile:
    def test_from_traits_coder_default(self):
        """Coder default traits produce high detail, low verbosity."""
        profile = PersonalityProfile.from_traits("a1", ["严谨", "注重细节", "高效"])
        assert profile.detail_orientation > 0.8
        assert profile.verbosity < 0.5
        assert profile.confidence > 0.6

    def test_from_traits_reviewer_default(self):
        """Reviewer traits produce high verbosity and detail."""
        profile = PersonalityProfile.from_traits("a2", ["批判性思维", "注重质量", "全面"])
        assert profile.verbosity > 0.6
        assert profile.detail_orientation > 0.7
        assert profile.confidence > 0.6

    def test_from_traits_writer_default(self):
        """Writer traits produce warmth and formality."""
        profile = PersonalityProfile.from_traits("a3", ["清晰表达", "结构化", "用户视角"])
        assert profile.social_warmth > 0.6
        assert profile.formality > 0.6

    def test_from_traits_empty_is_neutral(self):
        """Empty traits yield all-neutral profile."""
        profile = PersonalityProfile.from_traits("a4", [])
        assert profile.verbosity == 0.5
        assert profile.formality == 0.5
        assert profile.to_behavioral_prompt() == ""

    def test_dimensions_clamped_to_bounds(self):
        """Dimensions never exceed [0.0, 1.0] even with many boosting traits."""
        profile = PersonalityProfile.from_traits("a5", ["严谨"] * 10)
        assert profile.detail_orientation <= 1.0
        assert profile.confidence <= 1.0

    def test_dimensions_clamped_to_zero(self):
        """Dimensions never go below 0.0 even with many reducing traits."""
        profile = PersonalityProfile.from_traits("a6", ["简洁"] * 10)
        assert profile.verbosity >= 0.0
        assert profile.formality >= 0.0

    def test_behavioral_prompt_non_empty_for_extreme(self):
        """Extreme traits produce non-empty behavioral prompt."""
        profile = PersonalityProfile.from_traits("a7", ["前瞻性", "热心", "果断"])
        prompt = profile.to_behavioral_prompt()
        assert len(prompt) > 0
        assert "主动" in prompt  # proactiveness

    def test_different_roles_produce_different_prompts(self):
        """Different role archetypes yield distinct behavioral instructions."""
        coder = PersonalityProfile.from_traits("c", ["高效", "严谨"])
        reviewer = PersonalityProfile.from_traits("r", ["批判性思维", "全面"])
        writer = PersonalityProfile.from_traits("w", ["用户视角", "幽默"])
        prompts = {coder.to_behavioral_prompt(), reviewer.to_behavioral_prompt(), writer.to_behavioral_prompt()}
        assert len(prompts) == 3  # All different

    def test_unknown_traits_ignored(self):
        """Unknown traits are silently ignored (neutral profile)."""
        profile = PersonalityProfile.from_traits("a8", ["未知特质", "不存在的"])
        assert profile.to_behavioral_prompt() == ""

    def test_trait_map_coverage(self):
        """All default bootstrap traits have entries in the map."""
        from src.slock_engine.role_bootstrap import _DEFAULT_TRAITS
        for role_traits in _DEFAULT_TRAITS.values():
            for trait in role_traits:
                assert trait in _TRAIT_DIMENSION_MAP, f"Trait '{trait}' missing from map"


class TestPersonalityEngine:
    def test_caching(self):
        """Same agent_id returns cached profile instance."""
        engine = PersonalityEngine()
        p1 = engine.get_profile("a", ["严谨"])
        p2 = engine.get_profile("a", ["严谨"])
        assert p1 is p2

    def test_invalidate_clears_cache(self):
        """invalidate removes cached profile."""
        engine = PersonalityEngine()
        p1 = engine.get_profile("a", ["严谨"])
        engine.invalidate("a")
        p2 = engine.get_profile("a", ["热心"])
        assert p1 is not p2
        assert p2.social_warmth > p1.social_warmth

    def test_invalidate_all(self):
        """invalidate_all clears entire cache."""
        engine = PersonalityEngine()
        engine.get_profile("a", ["严谨"])
        engine.get_profile("b", ["热心"])
        engine.invalidate_all()
        assert engine._cache == {}
