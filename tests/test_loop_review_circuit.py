"""Tests for Loop Engine review circuit breaker.

Covers:
  (a) 连续 3 次 TimeoutError 后熔断触发
  (b) 冷却期内 review 被跳过
  (c) 冷却期后 review 恢复
  (d) 成功 review 重置计数器
  (e) 开关关闭时不熔断
"""

from unittest.mock import MagicMock, patch

import pytest

from src.engine_base import PerspectiveReview, ReviewPerspective, ReviewResult
from src.loop_engine.engine import LoopEngine, LoopEngineCallbacks, LoopReviewCircuitState


@pytest.fixture
def engine():
    with patch("src.engine_base.get_settings") as mock_settings:
        s = MagicMock()
        s.loop_max_iterations = 100
        s.loop_convergence_window = 3
        s.loop_execution_timeout = 300
        s.loop_review_timeout = 5
        s.loop_review_enabled = True
        s.loop_review_failure_circuit_enabled = True
        s.loop_review_failure_max_consecutive = 3
        s.loop_review_failure_cooldown_iterations = 3
        mock_settings.return_value = s
        eng = LoopEngine(chat_id="test", root_path="/tmp/test")
        # Provide a mock session so review actually attempts the prompt
        eng._session = MagicMock()
        yield eng


@pytest.fixture
def callbacks():
    return LoopEngineCallbacks(on_review_done=MagicMock())


class TestLoopReviewCircuitBreaker:
    def test_circuit_state_initial(self, engine):
        """初始状态: 熔断器关闭, 计数器为 0."""
        assert engine._review_circuit.review_failure_consecutive == 0
        assert engine._review_circuit.review_circuit_open_until_iter == 0

    def test_consecutive_failures_open_circuit(self, engine, callbacks):
        """连续 3 次 TimeoutError 后熔断器打开."""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("timeout")

        for i in range(1, 4):
            result, decision = engine._conduct_review(i, callbacks)
            assert decision is not None
            assert decision.startswith("review_failed")

        # After 3rd failure, circuit should be open
        assert engine._review_circuit.review_failure_consecutive == 3
        assert engine._review_circuit.review_circuit_open_until_iter == 3 + 3  # iteration(3) + cooldown(3)
        # The 3rd call should have opened the circuit
        assert decision == "review_failed_open_circuit"

    def test_review_skipped_during_cooldown(self, engine, callbacks):
        """冷却期内 review 被跳过并返回 fallback."""
        # Manually set circuit to open state
        engine._review_circuit.review_failure_consecutive = 3
        engine._review_circuit.review_circuit_open_until_iter = 6

        for iteration in [4, 5, 6]:
            result, decision = engine._conduct_review(iteration, callbacks)
            assert decision == "review_circuit_open_skip"
            # Should not have called send_prompt_with_retry
            engine._session.send_prompt_with_retry.assert_not_called()
            # Fallback result should have all perspectives failed
            assert not result.all_passed
            assert any("熔断" in s for pr in result.reviews for s in pr.suggestions)

    def test_review_resumes_after_cooldown(self, engine, callbacks):
        """冷却期结束后 review 恢复执行."""
        engine._review_circuit.review_failure_consecutive = 3
        engine._review_circuit.review_circuit_open_until_iter = 6

        # Mock successful review response
        engine._session.send_prompt_with_retry.return_value = MagicMock()

        # Iteration 7 > open_until(6), should attempt real review
        result, decision = engine._conduct_review(7, callbacks)
        # send_prompt_with_retry was called (review executed)
        engine._session.send_prompt_with_retry.assert_called_once()
        # On success, circuit resets
        assert engine._review_circuit.review_failure_consecutive == 0
        assert engine._review_circuit.review_circuit_open_until_iter == 0
        assert decision is None  # success

    def test_success_resets_counter(self, engine, callbacks):
        """成功 review 后计数器归零."""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("timeout")
        # 2 failures (not yet tripped)
        for i in range(1, 3):
            engine._conduct_review(i, callbacks)
        assert engine._review_circuit.review_failure_consecutive == 2

        # Now succeed
        engine._session.send_prompt_with_retry.side_effect = None
        engine._session.send_prompt_with_retry.return_value = MagicMock()
        result, decision = engine._conduct_review(3, callbacks)
        assert engine._review_circuit.review_failure_consecutive == 0
        assert decision is None

    def test_circuit_disabled_no_trip(self, engine, callbacks):
        """开关关闭时，即使连续失败也不熔断."""
        engine.settings.loop_review_failure_circuit_enabled = False
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("timeout")

        for i in range(1, 6):
            result, decision = engine._conduct_review(i, callbacks)
            # Counter still accumulates
            assert engine._review_circuit.review_failure_consecutive == i
            # But circuit never opens
            assert engine._review_circuit.review_circuit_open_until_iter == 0
            assert decision == "review_failed_continue"

    def test_on_review_done_callback_called_on_skip(self, engine, callbacks):
        """熔断跳过时仍调用 on_review_done 回调."""
        engine._review_circuit.review_circuit_open_until_iter = 10

        result, decision = engine._conduct_review(5, callbacks)
        callbacks.on_review_done.assert_called_once_with(5, result)

    def test_mixed_scenario(self, engine, callbacks):
        """混合场景: 失败 → 熔断 → 冷却 → 恢复 → 失败 → 再次熔断."""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("timeout")

        # 3 failures → circuit opens at iter 3+3=6
        for i in range(1, 4):
            engine._conduct_review(i, callbacks)
        assert engine._review_circuit.review_circuit_open_until_iter == 6

        # iter 4-6: skipped
        for i in range(4, 7):
            _, decision = engine._conduct_review(i, callbacks)
            assert decision == "review_circuit_open_skip"

        # iter 7: resume, succeed
        engine._session.send_prompt_with_retry.side_effect = None
        engine._session.send_prompt_with_retry.return_value = MagicMock()
        _, decision = engine._conduct_review(7, callbacks)
        assert decision is None
        assert engine._review_circuit.review_failure_consecutive == 0

        # iter 8-10: fail again → re-open circuit at 10+3=13
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("timeout")
        for i in range(8, 11):
            _, decision = engine._conduct_review(i, callbacks)
        assert decision == "review_failed_open_circuit"
        assert engine._review_circuit.review_circuit_open_until_iter == 13
