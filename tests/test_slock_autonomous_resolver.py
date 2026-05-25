"""Tests for slock autonomous resolution (AC-5).

Covers:
- AutonomousResolver.has_ambiguity_markers detection
- attempt_resolve with RESOLVED / NEEDS_CLARIFICATION / TIMEOUT / SKIPPED outcomes
- format_structured_question output structure
- Per-task question budget enforcement (MAX_QUESTIONS_PER_TASK = 2)
- Timeout behavior (15s configurable)
"""

from __future__ import annotations

import asyncio

import pytest

from src.slock_engine.autonomous_resolver import (
    AMBIGUITY_MARKERS,
    MAX_QUESTIONS_PER_TASK,
    RESOLVE_TIMEOUT_SECONDS,
    AutonomousResolver,
    ResolveStatus,
)

# ============================================================
# Constants and configuration
# ============================================================


class TestResolverConstants:
    """Verify module-level constants."""

    def test_timeout_is_15s(self):
        assert RESOLVE_TIMEOUT_SECONDS == 15.0

    def test_max_questions_is_2(self):
        assert MAX_QUESTIONS_PER_TASK == 2

    def test_ambiguity_markers_non_empty(self):
        assert len(AMBIGUITY_MARKERS) > 0
        assert "不确定" in AMBIGUITY_MARKERS
        assert "need clarification" in AMBIGUITY_MARKERS


# ============================================================
# has_ambiguity_markers
# ============================================================


class TestAmbiguityDetection:
    """Test ambiguity marker detection in agent output."""

    @pytest.fixture
    def resolver(self):
        return AutonomousResolver()

    @pytest.mark.parametrize("text", [
        "我不确定你要的是哪种排序",
        "需要确认具体的接口格式",
        "需要更多信息才能继续",
        "I need clarification on the API endpoint",
        "The requirements are ambiguous",
        "Cannot determine the correct approach",
    ])
    def test_detects_ambiguity(self, resolver, text):
        assert resolver.has_ambiguity_markers(text) is True

    @pytest.mark.parametrize("text", [
        "已完成排序函数实现",
        "代码审查通过，无问题",
        "Implementation complete. All tests pass.",
        "部署成功",
        "",
    ])
    def test_no_false_positives(self, resolver, text):
        assert resolver.has_ambiguity_markers(text) is False

    def test_case_insensitive(self, resolver):
        assert resolver.has_ambiguity_markers("NEED CLARIFICATION on this") is True
        assert resolver.has_ambiguity_markers("AMBIGUOUS requirement") is True


# ============================================================
# attempt_resolve
# ============================================================


class TestAttemptResolve:
    """Test the async resolution flow."""

    @pytest.fixture
    def resolver_no_llm(self):
        return AutonomousResolver(llm_callback=None)

    @pytest.fixture
    def resolver_with_llm(self):
        async def mock_llm(prompt):
            return "RESOLVED: 实现一个快速排序函数，使用Python，输入为整数列表。\n假设: 输入为整数列表"
        return AutonomousResolver(llm_callback=mock_llm)

    @pytest.fixture
    def resolver_needs_clarification(self):
        async def mock_llm(prompt):
            return "NEEDS_CLARIFICATION: 请问是要实现哪种排序算法？"
        return AutonomousResolver(llm_callback=mock_llm)

    def test_no_llm_returns_needs_clarification(self, resolver_no_llm):
        result = asyncio.run(
            resolver_no_llm.attempt_resolve("帮我写排序", task_id="t1")
        )
        assert result.status == ResolveStatus.NEEDS_CLARIFICATION
        assert "No LLM callback" in result.reasoning_trace

    def test_resolved_returns_resolved_status(self, resolver_with_llm):
        result = asyncio.run(
            resolver_with_llm.attempt_resolve("帮我写排序", task_id="t2")
        )
        assert result.status == ResolveStatus.RESOLVED
        assert "快速排序" in result.resolved_text
        assert len(result.assumptions) >= 1
        assert result.duration_s >= 0

    def test_clarification_returns_needs_clarification(self, resolver_needs_clarification):
        result = asyncio.run(
            resolver_needs_clarification.attempt_resolve("帮我写排序", task_id="t3")
        )
        assert result.status == ResolveStatus.NEEDS_CLARIFICATION

    def test_timeout_returns_timeout_status(self):
        """Resolution that exceeds timeout returns TIMEOUT."""
        async def slow_llm(prompt):
            await asyncio.sleep(10)
            return "RESOLVED: done"

        resolver = AutonomousResolver(llm_callback=slow_llm, resolve_timeout=0.1)
        result = asyncio.run(
            resolver.attempt_resolve("帮我写排序", task_id="t4")
        )
        assert result.status == ResolveStatus.TIMEOUT
        assert result.duration_s >= 0.1

    def test_question_budget_exhausted_returns_skipped(self):
        """After MAX_QUESTIONS_PER_TASK, status is SKIPPED."""
        resolver = AutonomousResolver(llm_callback=None, max_questions=2)
        # Simulate 2 questions already asked
        resolver.record_question_asked("t5")
        resolver.record_question_asked("t5")

        assert resolver.can_ask_question("t5") is False

        result = asyncio.run(
            resolver.attempt_resolve("帮我写排序", task_id="t5")
        )
        assert result.status == ResolveStatus.SKIPPED
        assert "Question limit reached" in result.reasoning_trace


# ============================================================
# Question budget tracking
# ============================================================


class TestQuestionBudget:
    """Per-task question count enforcement."""

    def test_initial_count_is_zero(self):
        resolver = AutonomousResolver()
        assert resolver.get_question_count("new_task") == 0

    def test_record_increments_count(self):
        resolver = AutonomousResolver()
        resolver.record_question_asked("t1")
        assert resolver.get_question_count("t1") == 1
        resolver.record_question_asked("t1")
        assert resolver.get_question_count("t1") == 2

    def test_can_ask_within_budget(self):
        resolver = AutonomousResolver(max_questions=2)
        assert resolver.can_ask_question("t1") is True
        resolver.record_question_asked("t1")
        assert resolver.can_ask_question("t1") is True
        resolver.record_question_asked("t1")
        assert resolver.can_ask_question("t1") is False

    def test_cleanup_resets_state(self):
        resolver = AutonomousResolver()
        resolver.record_question_asked("t1")
        resolver.record_question_asked("t1")
        resolver.cleanup_task("t1")
        assert resolver.get_question_count("t1") == 0
        assert resolver.can_ask_question("t1") is True


# ============================================================
# format_structured_question
# ============================================================


class TestStructuredQuestion:
    """Test structured question formatting for user cards."""

    def test_basic_format(self):
        resolver = AutonomousResolver()
        q = resolver.format_structured_question(
            attempts_summary="尝试了基于上下文推断排序类型",
            blocker="无法确定用户期望的排序算法和输入类型",
            candidates=["快速排序（通用）", "归并排序（稳定）", "堆排序（原地）"],
        )
        assert "需要您的输入" in q
        assert "已尝试:" in q
        assert "卡点:" in q
        assert "候选方案:" in q
        assert "1." in q
        assert "2." in q
        assert "3." in q
        assert "快速排序" in q
        assert "请回复方案编号" in q

    def test_max_three_candidates(self):
        """Only up to 3 candidates shown even if more provided."""
        resolver = AutonomousResolver()
        q = resolver.format_structured_question(
            attempts_summary="test",
            blocker="test",
            candidates=["A", "B", "C", "D", "E"],
        )
        assert "1. A" in q
        assert "2. B" in q
        assert "3. C" in q
        assert "4." not in q

    def test_empty_candidates(self):
        """Works with no candidates."""
        resolver = AutonomousResolver()
        q = resolver.format_structured_question(
            attempts_summary="test",
            blocker="无法推断",
            candidates=[],
        )
        assert "卡点:" in q
        assert "请回复方案编号" in q


# ============================================================
# Resolve result parsing
# ============================================================


class TestResolutionParsing:
    """Test internal _parse_resolution_response logic."""

    def test_resolved_with_assumptions(self):
        resolver = AutonomousResolver()
        response = "RESOLVED: 使用快速排序\n假设: 输入为整数列表\n假设：数据量小于10万"
        result = resolver._parse_resolution_response(response, "原始任务")
        assert result.status == ResolveStatus.RESOLVED
        assert len(result.assumptions) == 2
        assert "输入为整数列表" in result.assumptions[0]

    def test_resolved_case_insensitive_prefix(self):
        resolver = AutonomousResolver()
        response = "resolved: 完成实现"
        result = resolver._parse_resolution_response(response, "原始任务")
        assert result.status == ResolveStatus.RESOLVED

    def test_needs_clarification_parsed(self):
        resolver = AutonomousResolver()
        response = "NEEDS_CLARIFICATION: 需要知道使用什么语言"
        result = resolver._parse_resolution_response(response, "原始任务")
        assert result.status == ResolveStatus.NEEDS_CLARIFICATION

    def test_unrecognized_response_defaults_to_needs_clarification(self):
        resolver = AutonomousResolver()
        response = "I don't know what you want"
        result = resolver._parse_resolution_response(response, "原始任务")
        assert result.status == ResolveStatus.NEEDS_CLARIFICATION

    def test_english_assumptions(self):
        resolver = AutonomousResolver()
        response = "RESOLVED: implement quicksort\nAssumption: input is list of integers"
        result = resolver._parse_resolution_response(response, "task")
        assert result.status == ResolveStatus.RESOLVED
        assert len(result.assumptions) == 1
        assert "input is list of integers" in result.assumptions[0]
