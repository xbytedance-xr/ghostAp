"""Tests for Loop Engine review circuit breaker.

Covers:
  (a) 连续 3 次 TimeoutError 后熔断触发
  (b) 冷却期内 review 被跳过
  (c) 冷却期后 review 恢复
  (d) 成功 review 重置计数器
  (e) 开关关闭时不熔断
  (f) 结构化诊断: TimeoutError → diag 含 fail_reason='timeout'
  (g) 结构化诊断: 非 timeout 异常 → diag 含 fail_reason='exception'
  (h) 结构化诊断: last_review_failure_diag 正确赋值
  (i) 成功 review 后 last_review_failure_diag 被清空
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
        s.loop_review_failure_max_cooldown_iterations = 12
        s.loop_review_min_timeout = 30
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


class TestLoopReviewDiagnostics:
    """Tests for structured review exception diagnostics in LoopEngine."""

    def test_timeout_error_produces_diag_with_timeout_reason(self, engine, callbacks):
        """TimeoutError → diag dict 包含 fail_reason='timeout' 且 error_text 非空。"""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("prompt timeout")
        result, decision = engine._conduct_review(1, callbacks)

        diag = engine._review_circuit.last_review_failure_diag
        assert diag is not None
        assert diag["fail_reason"] == "timeout"
        assert diag["error_text"]  # non-empty
        assert "timeout" in diag["err_type"].lower() or diag["fail_reason"] == "timeout"
        assert diag["phase"] == "review"
        assert diag["cycle"] == 1

    def test_timeout_error_empty_message_friendly_text(self, engine, callbacks):
        """裸 TimeoutError() (无消息) → error_text 使用中文友好文案。"""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError()
        result, decision = engine._conduct_review(1, callbacks)

        diag = engine._review_circuit.last_review_failure_diag
        assert diag is not None
        assert diag["fail_reason"] == "timeout"
        # Should NOT contain "empty message"
        assert "(empty message)" not in diag.get("error_text", "")

    def test_non_timeout_error_produces_diag_with_exception_reason(self, engine, callbacks):
        """非 timeout 异常 → diag dict 包含 fail_reason='exception'。"""
        engine._session.send_prompt_with_retry.side_effect = RuntimeError("connection refused")
        result, decision = engine._conduct_review(1, callbacks)

        diag = engine._review_circuit.last_review_failure_diag
        assert diag is not None
        assert diag["fail_reason"] == "exception"
        assert "connection refused" in diag["error_text"]
        assert diag["err_type"] == "RuntimeError"

    def test_diag_stored_on_circuit_state(self, engine, callbacks):
        """异常后 circuit.last_review_failure_diag 被正确赋值。"""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("t")
        engine._conduct_review(1, callbacks)

        diag = engine._review_circuit.last_review_failure_diag
        assert isinstance(diag, dict)
        # Must contain all stable keys
        for key in ("phase", "role", "cycle", "decision", "fail_reason",
                     "err_type", "err_repr", "error_text", "traceback_snippet"):
            assert key in diag, f"Missing key: {key}"

    def test_diag_cleared_on_success(self, engine, callbacks):
        """成功 review 后 last_review_failure_diag 被置为 None。"""
        # First: failure
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("t")
        engine._conduct_review(1, callbacks)
        assert engine._review_circuit.last_review_failure_diag is not None

        # Then: success
        engine._session.send_prompt_with_retry.side_effect = None
        engine._session.send_prompt_with_retry.return_value = MagicMock()
        engine._conduct_review(2, callbacks)
        assert engine._review_circuit.last_review_failure_diag is None

    def test_circuit_open_diag_has_extra_fields(self, engine, callbacks):
        """熔断器打开时 diag 包含 review_circuit_open 和 open_until_iter。"""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("t")
        for i in range(1, 4):
            engine._conduct_review(i, callbacks)

        diag = engine._review_circuit.last_review_failure_diag
        assert diag is not None
        assert diag.get("decision") == "review_failed_open_circuit"
        assert diag.get("review_circuit_open") is True
        assert diag.get("open_until_iter") == 6  # 3 + 3
        assert diag.get("consecutive_failures") == 3


# ===========================================================================
# Loop Engine: Exponential backoff for circuit breaker cooldown
# ===========================================================================


class TestLoopCircuitExponentialBackoff:
    """Verify exponential backoff: cooldown grows 3→6→12 on repeated triggers."""

    def test_first_trigger_cooldown_is_base(self, engine, callbacks):
        """First circuit trigger: cooldown = base (3)."""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("t")
        for i in range(1, 4):
            engine._conduct_review(i, callbacks)

        assert engine._review_circuit.review_circuit_open_until_iter == 6  # 3 + 3
        assert engine._review_circuit.backoff_level == 1

    def test_second_trigger_cooldown_doubles(self, engine, callbacks):
        """Second circuit trigger: cooldown = 6."""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("t")
        # First trigger
        for i in range(1, 4):
            engine._conduct_review(i, callbacks)
        assert engine._review_circuit.backoff_level == 1

        # Reset consecutive, keep backoff
        engine._review_circuit.review_failure_consecutive = 0
        engine._review_circuit.recent_outcomes.clear()
        base = engine._review_circuit.review_circuit_open_until_iter + 1
        for i in range(base, base + 3):
            engine._conduct_review(i, callbacks)

        # cooldown = 3 * 2^1 = 6
        assert engine._review_circuit.review_circuit_open_until_iter == (base + 2) + 6
        assert engine._review_circuit.backoff_level == 2

    def test_third_trigger_cooldown_capped(self, engine, callbacks):
        """Third trigger: cooldown capped at max (12)."""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("t")

        # 3 triggers with proper gap between them
        for trigger in range(3):
            engine._review_circuit.review_failure_consecutive = 0
            engine._review_circuit.recent_outcomes.clear()
            base = engine._review_circuit.review_circuit_open_until_iter + 1
            for i in range(base, base + 3):
                engine._conduct_review(i, callbacks)

        assert engine._review_circuit.backoff_level == 3
        # Fourth trigger still capped
        engine._review_circuit.review_failure_consecutive = 0
        engine._review_circuit.recent_outcomes.clear()
        base = engine._review_circuit.review_circuit_open_until_iter + 1
        for i in range(base, base + 3):
            engine._conduct_review(i, callbacks)
        assert engine._review_circuit.review_circuit_open_until_iter == (base + 2) + 12

    def test_success_resets_backoff_level(self, engine, callbacks):
        """After success, backoff_level resets to 0."""
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("t")
        for i in range(1, 4):
            engine._conduct_review(i, callbacks)
        assert engine._review_circuit.backoff_level == 1

        # Success
        engine._session.send_prompt_with_retry.side_effect = None
        engine._session.send_prompt_with_retry.return_value = MagicMock()
        base = engine._review_circuit.review_circuit_open_until_iter + 1
        engine._conduct_review(base, callbacks)

        assert engine._review_circuit.backoff_level == 0
        assert engine._review_circuit.consecutive_timeouts == 0


# ===========================================================================
# Loop Engine: Adaptive (progressive) review timeout
# ===========================================================================


class TestLoopAdaptiveTimeout:
    """Verify review timeout decreases on consecutive timeouts."""

    def test_timeout_decreases_on_consecutive_timeouts(self, engine, callbacks):
        """Consecutive TimeoutErrors trigger progressively shorter timeouts."""
        engine.settings.loop_review_timeout = 120
        engine.settings.loop_review_min_timeout = 30
        engine.settings.loop_review_failure_max_consecutive = 100  # prevent circuit interference

        captured_timeouts = []
        original_send = engine._session.send_prompt_with_retry

        def capturing_send(*args, **kwargs):
            captured_timeouts.append(kwargs.get("timeout"))
            raise TimeoutError("t")

        engine._session.send_prompt_with_retry = capturing_send

        for i in range(1, 4):
            engine._conduct_review(i, callbacks)

        assert captured_timeouts == [120, 60, 30]

    def test_timeout_resets_after_success(self, engine, callbacks):
        """After success, timeout returns to base."""
        engine.settings.loop_review_timeout = 120
        engine.settings.loop_review_min_timeout = 30
        engine.settings.loop_review_failure_max_consecutive = 100

        # 2 timeouts
        engine._session.send_prompt_with_retry.side_effect = TimeoutError("t")
        for i in range(1, 3):
            engine._conduct_review(i, callbacks)
        assert engine._review_circuit.consecutive_timeouts == 2

        # 1 success
        engine._session.send_prompt_with_retry.side_effect = None
        engine._session.send_prompt_with_retry.return_value = MagicMock()
        engine._conduct_review(3, callbacks)
        assert engine._review_circuit.consecutive_timeouts == 0

        # Next timeout should use base (120)
        captured = []

        def capturing_send(*args, **kwargs):
            captured.append(kwargs.get("timeout"))
            raise TimeoutError("t")

        engine._session.send_prompt_with_retry = capturing_send
        engine._conduct_review(4, callbacks)
        assert captured == [120]

    def test_timeout_respects_min(self, engine, callbacks):
        """Timeout never goes below min_timeout."""
        engine.settings.loop_review_timeout = 120
        engine.settings.loop_review_min_timeout = 30
        engine._review_circuit.consecutive_timeouts = 10

        captured = []

        def capturing_send(*args, **kwargs):
            captured.append(kwargs.get("timeout"))
            raise TimeoutError("t")

        engine._session.send_prompt_with_retry = capturing_send
        engine._conduct_review(1, callbacks)
        assert captured == [30]


class TestLoopReviewCircuitStateSerialization:
    """LoopReviewCircuitState.to_dict/from_dict round-trip for last_review_elapsed_ms."""

    def test_round_trip_default(self):
        circuit = LoopReviewCircuitState()
        assert circuit.last_review_elapsed_ms == 0
        d = circuit.to_dict()
        assert "last_review_elapsed_ms" in d
        restored = LoopReviewCircuitState.from_dict(d)
        assert restored.last_review_elapsed_ms == 0

    def test_round_trip_nonzero(self):
        circuit = LoopReviewCircuitState(last_review_elapsed_ms=9999)
        d = circuit.to_dict()
        assert d["last_review_elapsed_ms"] == 9999
        restored = LoopReviewCircuitState.from_dict(d)
        assert restored.last_review_elapsed_ms == 9999

    def test_from_dict_missing_key_defaults_zero(self):
        """Backward compat: old persisted state without last_review_elapsed_ms."""
        restored = LoopReviewCircuitState.from_dict({"review_failure_consecutive": 2})
        assert restored.last_review_elapsed_ms == 0
