"""Tests for _estimate_tokens weighted CJK formula (AC17)."""
import pytest

from src.slock_engine.discussion_manager import DiscussionManager


class TestEstimateTokensWeighted:
    """Verify weighted token estimation accuracy for mixed CJK/Latin content."""

    def _make_dm(self):
        """Create a minimal DiscussionManager for testing."""
        from unittest.mock import MagicMock
        engine = MagicMock()
        engine.chat_id = "test"
        dm = DiscussionManager.__new__(DiscussionManager)
        dm._engine = engine
        dm._config = MagicMock()
        return dm

    def test_empty_string(self):
        dm = self._make_dm()
        assert dm._estimate_tokens("") == 0

    def test_pure_latin(self):
        dm = self._make_dm()
        text = "hello world this is a test"  # 26 chars, 0 CJK
        result = dm._estimate_tokens(text)
        expected = int(0 * 1.5 + 26 * 0.25)  # = 6
        assert result == expected

    def test_pure_cjk(self):
        dm = self._make_dm()
        text = "这是一个完全中文的测试文本内容"  # 15 CJK chars
        result = dm._estimate_tokens(text)
        expected = int(15 * 1.5 + 0 * 0.25)  # = 22
        assert result == expected

    def test_cjk_ratio_029(self):
        """CJK ratio ~0.29 - boundary below old threshold."""
        dm = self._make_dm()
        # 29 CJK + 71 Latin = 100 chars, ratio = 0.29
        cjk_part = "中" * 29
        latin_part = "a" * 71
        text = cjk_part + latin_part
        result = dm._estimate_tokens(text)
        expected = int(29 * 1.5 + 71 * 0.25)  # = 43 + 17 = 61 (rounded)
        assert result == expected

    def test_cjk_ratio_030(self):
        """CJK ratio = 0.30 - exact old threshold boundary."""
        dm = self._make_dm()
        cjk_part = "中" * 30
        latin_part = "a" * 70
        text = cjk_part + latin_part
        result = dm._estimate_tokens(text)
        expected = int(30 * 1.5 + 70 * 0.25)  # = 45 + 17 = 62
        assert result == expected

    def test_cjk_ratio_031(self):
        """CJK ratio ~0.31 - boundary above old threshold."""
        dm = self._make_dm()
        cjk_part = "中" * 31
        latin_part = "a" * 69
        text = cjk_part + latin_part
        result = dm._estimate_tokens(text)
        expected = int(31 * 1.5 + 69 * 0.25)  # = 46 + 17 = 63
        assert result == expected

    def test_no_discontinuity_at_boundary(self):
        """No jump between 0.30 and 0.31 ratios (old code had 6x jump)."""
        dm = self._make_dm()
        text_30 = "中" * 30 + "a" * 70
        text_31 = "中" * 31 + "a" * 69
        result_30 = dm._estimate_tokens(text_30)
        result_31 = dm._estimate_tokens(text_31)
        # Difference should be smooth (~1.25 per additional CJK char replacing Latin)
        assert abs(result_31 - result_30) <= 3

    def test_mixed_content_accuracy(self):
        """30% CJK + 70% Latin should be within 30% of tiktoken-like estimate."""
        dm = self._make_dm()
        # Approximate tiktoken: CJK chars ≈ 1.5 tokens, Latin ≈ 0.25 tokens
        cjk_part = "测试内容" * 7  # 28 CJK chars
        latin_part = "hello world " * 6  # 72 chars
        text = cjk_part + latin_part  # 100 chars, 28% CJK
        
        result = dm._estimate_tokens(text)
        # Expected: 28 * 1.5 + 72 * 0.25 = 42 + 18 = 60
        expected_baseline = int(28 * 1.5 + 72 * 0.25)
        # Within 30% tolerance
        assert abs(result - expected_baseline) / expected_baseline < 0.3
