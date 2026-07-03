"""Tests for subagent encouragement injection (Task 4/13).

Validates:
- SUBAGENT_ENCOURAGEMENT_PROMPT constant exists and is non-empty
- AgentExecutor._build_prompt includes the encouragement suffix
- Role prefix is injected when params.role is set
"""

import unittest
from unittest.mock import MagicMock

import pytest

from src.workflow_engine.roles import SUBAGENT_ENCOURAGEMENT_PROMPT
from src.workflow_engine.script_gen import (
    SUBAGENT_ENCOURAGEMENT,
    _get_agent_capability_note,
    build_script_gen_prompt,
    generate_simple_script,
    validate_generated_script,
)


class TestSubagentEncouragementConstant(unittest.TestCase):
    """Test the SUBAGENT_ENCOURAGEMENT_PROMPT constant."""

    def test_constant_is_non_empty_string(self):
        self.assertIsInstance(SUBAGENT_ENCOURAGEMENT_PROMPT, str)
        self.assertGreater(len(SUBAGENT_ENCOURAGEMENT_PROMPT), 50)

    def test_contains_subagent_keyword(self):
        self.assertIn("subagent", SUBAGENT_ENCOURAGEMENT_PROMPT.lower())

    def test_contains_parallel_keyword(self):
        self.assertIn("parallel", SUBAGENT_ENCOURAGEMENT_PROMPT.lower())


class TestBuildPromptInjection(unittest.TestCase):
    """Test AgentExecutor._build_prompt injects encouragement."""

    def setUp(self):
        self._executors = []

    def tearDown(self):
        for executor in self._executors:
            try:
                executor.shutdown(wait=False)
            except Exception:
                pass
        self._executors.clear()

    def _make_executor(self):
        import threading

        from src.workflow_engine.executor import AgentExecutor

        executor = AgentExecutor(
            cwd="/tmp",
            cancel_event=threading.Event(),
            on_token_usage=None,
        )
        self._executors.append(executor)
        return executor

    def _make_params(self, prompt="do something", role=""):
        from src.workflow_engine.models import AgentCallParams

        return AgentCallParams(
            prompt=prompt,
            tool="coco",
            role=role,
        )

    def test_prompt_ends_with_encouragement(self):
        executor = self._make_executor()
        params = self._make_params(prompt="analyze this code")
        result = executor._build_prompt(params)

        self.assertTrue(result.endswith(SUBAGENT_ENCOURAGEMENT_PROMPT))

    def test_prompt_contains_task(self):
        executor = self._make_executor()
        params = self._make_params(prompt="analyze this code")
        result = executor._build_prompt(params)

        self.assertIn("analyze this code", result)

    def test_role_prefix_injected(self):
        executor = self._make_executor()
        params = self._make_params(prompt="task", role="security_auditor")
        result = executor._build_prompt(params)

        self.assertTrue(result.startswith("Role: security_auditor"))

    def test_no_role_prefix_when_empty(self):
        executor = self._make_executor()
        params = self._make_params(prompt="task", role="")
        result = executor._build_prompt(params)

        self.assertFalse(result.startswith("Role:"))


class TestBridgeArgsPassthrough(unittest.TestCase):
    """Test RuntimeBridge args parameter and passthrough."""

    def setUp(self):
        self._bridges = []

    def tearDown(self):
        for bridge in self._bridges:
            try:
                bridge.stop()
            except Exception:
                pass
        self._bridges.clear()

    def _make_bridge(self, **kwargs):
        from src.workflow_engine.bridge import RuntimeBridge

        defaults = {"script_path": "/tmp/test.js", "cwd": "/tmp"}
        defaults.update(kwargs)
        bridge = RuntimeBridge(**defaults)
        self._bridges.append(bridge)
        return bridge

    def test_bridge_stores_args(self):
        bridge = self._make_bridge(args={"key": "value", "num": 42})
        self.assertEqual(bridge._args, {"key": "value", "num": 42})

    def test_bridge_default_args_empty_dict(self):
        bridge = self._make_bridge()
        self.assertEqual(bridge._args, {})

    def test_bridge_none_args_becomes_empty_dict(self):
        bridge = self._make_bridge(args=None)
        self.assertEqual(bridge._args, {})


class TestBridgeBackpressure(unittest.TestCase):
    """Test queue backpressure in _handle_agent_call."""

    def setUp(self):
        self._bridges = []

    def tearDown(self):
        for bridge in self._bridges:
            try:
                bridge.stop()
            except Exception:
                pass
        self._bridges.clear()

    def _make_bridge(self):
        import threading

        from src.workflow_engine.bridge import RuntimeBridge

        bridge = RuntimeBridge(
            script_path="/tmp/test.js",
            cwd="/tmp",
        )
        self._bridges.append(bridge)
        # Manually set up internals normally created by start()
        bridge._executor = MagicMock()
        bridge._on_agent_call = MagicMock()
        bridge._write_lock = threading.Lock()
        bridge._process = MagicMock()
        bridge._process.stdin = MagicMock()
        return bridge

    def test_rejects_when_queue_full(self):
        from src.workflow_engine.constants import MAX_QUEUE_SIZE

        bridge = self._make_bridge()
        # Fill the queue to capacity
        for _ in range(MAX_QUEUE_SIZE):
            bridge._msg_queue.append({"test": True})

        # Track what gets sent
        bridge._send_error_response = MagicMock()

        bridge._handle_agent_call(
            {"prompt": "test", "tool": "coco"},
            request_id="req_1",
        )

        # Should have sent error response about backpressure
        bridge._send_error_response.assert_called_once()
        args = bridge._send_error_response.call_args
        self.assertEqual(args[0][0], "req_1")  # request_id
        self.assertIn("backpressure", args[1]["message"].lower())

    def test_accepts_when_queue_not_full(self):
        bridge = self._make_bridge()
        # Queue is empty — should proceed (submit to executor)
        bridge._handle_agent_call(
            {"prompt": "test", "tool": "coco"},
            request_id="req_2",
        )
        bridge._executor.submit.assert_called_once()


class TestScriptGenSubagentEncouragement(unittest.TestCase):
    """Test SUBAGENT_ENCOURAGEMENT constant in script_gen.py."""

    def test_constant_exists_and_is_non_empty(self):
        self.assertIsInstance(SUBAGENT_ENCOURAGEMENT, str)
        self.assertGreater(len(SUBAGENT_ENCOURAGEMENT), 50)

    def test_constant_contains_subagent_keyword(self):
        self.assertIn("subagent", SUBAGENT_ENCOURAGEMENT.lower())

    def test_constant_contains_parallel_keyword(self):
        self.assertIn("parallel", SUBAGENT_ENCOURAGEMENT.lower())

    def test_constant_contains_efficiency_keyword(self):
        self.assertIn("efficiency", SUBAGENT_ENCOURAGEMENT.lower())

    def test_constant_contains_encouragement_marker(self):
        self.assertIn("Subagent Usage Encouragement", SUBAGENT_ENCOURAGEMENT)


class TestBuildScriptGenPromptInjection(unittest.TestCase):
    """Test that build_script_gen_prompt injects SUBAGENT_ENCOURAGEMENT."""

    def test_prompt_ends_with_encouragement(self):
        prompt = build_script_gen_prompt(
            requirement="Test requirement",
            available_tools=["coco", "claude"],
        )
        self.assertTrue(prompt.endswith(SUBAGENT_ENCOURAGEMENT))

    def test_prompt_contains_requirement(self):
        prompt = build_script_gen_prompt(
            requirement="My unique test requirement xyz123",
            available_tools=["coco"],
        )
        self.assertIn("My unique test requirement xyz123", prompt)

    def test_prompt_contains_tools_list(self):
        prompt = build_script_gen_prompt(
            requirement="Test",
            available_tools=["coco", "claude", "aiden"],
        )
        self.assertIn("`coco`", prompt)
        self.assertIn("`claude`", prompt)
        self.assertIn("`aiden`", prompt)

    def test_prompt_contains_roles_list(self):
        prompt = build_script_gen_prompt(
            requirement="Test",
            available_tools=[],
        )
        self.assertIn("architect", prompt.lower())

    def test_prompt_contains_budget(self):
        prompt = build_script_gen_prompt(
            requirement="Test",
            available_tools=[],
        )
        self.assertNotIn("预算", prompt)

    def test_encouragement_appears_exactly_once(self):
        prompt = build_script_gen_prompt(
            requirement="Test",
            available_tools=["coco"],
        )
        count = prompt.count(SUBAGENT_ENCOURAGEMENT)
        self.assertEqual(count, 1, f"Expected encouragement once, found {count} times")


class TestGenerateSimpleScriptEncouragement(unittest.TestCase):
    """Test that generate_simple_script includes SUBAGENT_ENCOURAGEMENT in agent prompts."""

    def test_script_contains_encouragement_in_agent_prompts(self):
        script = generate_simple_script("Test requirement for simple script")
        # The encouragement should appear in each agent() call prompt
        # There are 4 agent calls: planner, executor, task (in map), synthesizer
        count = script.count(SUBAGENT_ENCOURAGEMENT)
        self.assertGreaterEqual(count, 3, f"Expected encouragement in at least 3 agent prompts, found {count}")

    def test_script_has_valid_structure(self):
        script = generate_simple_script("Test requirement")
        self.assertIn("export const meta", script)
        self.assertIn("export default async function", script)
        self.assertIn("agent(", script)

    def test_script_avoids_slow_static_analysis_agent(self):
        script = generate_simple_script("Fix workflow state mismatch")
        self.assertNotIn('label: "task-analysis"', script)
        self.assertNotIn("Analyze this task and determine the best execution strategy", script)

    def test_script_bounds_agent_calls_and_handles_errors(self):
        script = generate_simple_script("Fix workflow state mismatch", selected_tools=["coco", "codex"])
        self.assertIn("timeout:", script)
        self.assertIn(".error", script)
        self.assertIn("fallback", script.lower())

        is_valid, messages = validate_generated_script(script)
        self.assertTrue(is_valid, f"Expected valid fallback script, got: {messages}")

    def test_script_uses_race_instead_of_llm_classify_for_fallback_routing(self):
        script = generate_simple_script(
            "分析 spec 模式目标完成度如何把控，先不要动手改代码",
            selected_tools=["traex", "codex", "coco"],
        )

        self.assertIn("race(", script)
        self.assertIn("candidateTools", script)
        self.assertNotIn("await classify(", script)
        self.assertNotIn('label: "route"', script)
        self.assertNotIn("route-classify", script)

        is_valid, messages = validate_generated_script(script)
        self.assertTrue(is_valid, f"Expected valid fallback script, got: {messages}")

    def test_script_prompt_preserves_analysis_only_requests(self):
        script = generate_simple_script(
            "分析 spec 模式目标完成度如何把控，先不要动手改代码",
            selected_tools=["traex", "codex"],
        )

        self.assertIn("If the user asks for analysis only", script)
        self.assertIn("do not change code", script)


@pytest.mark.skip(reason="Budget/roles selection removed; build_script_gen_prompt no longer accepts budget tokens or static roles.")
class TestBudgetConstraintAndAgentCapability(unittest.TestCase):
    """Test budget hard constraint and orchestrator agent capability adaptation.

    SKIPPED — budget/roles selection has been removed; script generation now
    uses dynamic roles and no budget hard constraints.
    """

    def test_budget_section_included_when_budget_tokens_provided(self):
        """Budget constraint section should appear when budget_tokens is provided."""
        prompt = build_script_gen_prompt(
            requirement="Test requirement",
            available_tools=["coco"],
        )
        self.assertIn("## 预算硬约束", prompt)
        self.assertIn("Token 预算硬约束", prompt)
        self.assertIn("2,000,000", prompt)

    def test_budget_section_not_included_when_budget_tokens_none(self):
        """Budget constraint section should NOT appear when budget_tokens is None."""
        prompt = build_script_gen_prompt(
            requirement="Test requirement",
            available_tools=["coco"],
            available_roles=["architect"],
            budget_total=2_000_000,
            budget_tokens=None,
        )
        self.assertNotIn("## 预算硬约束", prompt)
        self.assertNotIn("Token 预算硬约束", prompt)

    def test_budget_section_contains_tiered_guidance(self):
        """Budget section should contain tiered guidance for different budget sizes."""
        prompt = build_script_gen_prompt(
            requirement="Test requirement",
            available_tools=["coco"],
            available_roles=["architect"],
            budget_total=1_500_000,
            budget_tokens=1_500_000,
        )
        self.assertIn("预算紧张时", prompt)
        self.assertIn("预算适中时", prompt)
        self.assertIn("预算充足时", prompt)
        self.assertIn("50K-200K", prompt)
        self.assertIn("严禁超出预算", prompt)

    def test_agent_capability_section_included(self):
        """Agent capability section should always be included."""
        prompt = build_script_gen_prompt(
            requirement="Test requirement",
            available_tools=["coco"],
            available_roles=["architect"],
            budget_total=2_000_000,
            orchestrator_agent="coco",
        )
        self.assertIn("## 主编排 Agent 能力", prompt)
        self.assertIn("coco", prompt)

    def test_prompt_contains_both_sections_in_correct_order(self):
        """Both sections should appear in the prompt in the correct order."""
        prompt = build_script_gen_prompt(
            requirement="Test requirement",
            available_tools=["coco"],
            available_roles=["architect"],
            budget_total=2_000_000,
            budget_tokens=2_000_000,
            orchestrator_agent="claude",
        )
        # Budget section should come before agent capability section
        budget_pos = prompt.index("## 预算硬约束")
        agent_pos = prompt.index("## 主编排 Agent 能力")
        self.assertLess(budget_pos, agent_pos)
        # Both should come before user requirement
        req_pos = prompt.index("## User Requirement")
        self.assertLess(agent_pos, req_pos)

    def test_backward_compatibility_without_new_params(self):
        """Existing calls without new params should still work (defaults)."""
        prompt = build_script_gen_prompt(
            requirement="Test requirement",
            available_tools=["coco"],
            available_roles=["architect"],
            budget_total=2_000_000,
        )
        # Should still contain agent capability section with default "coco"
        self.assertIn("## 主编排 Agent 能力", prompt)
        self.assertIn("coco", prompt)
        # Should NOT contain budget section (budget_tokens defaults to None)
        self.assertNotIn("## 预算硬约束", prompt)


class TestAgentCapabilityNotes(unittest.TestCase):
    """Test _get_agent_capability_note returns correct notes for each agent type."""

    def test_agent_capability_coco(self):
        """Coco agent should have subagent and parallel orchestration notes."""
        note = _get_agent_capability_note("coco")
        self.assertIn("全栈编程", note)
        self.assertIn("subagent", note)
        self.assertIn("并行编排", note)

    def test_agent_capability_claude(self):
        """Claude agent should have deep reasoning notes."""
        note = _get_agent_capability_note("claude")
        self.assertIn("深度推理", note)
        self.assertIn("逻辑严谨性", note)

    def test_agent_capability_aiden(self):
        """Aiden agent should have code review notes."""
        note = _get_agent_capability_note("aiden")
        self.assertIn("代码审查", note)
        self.assertIn("架构设计", note)

    def test_agent_capability_codex(self):
        """Codex agent should have fast code generation notes."""
        note = _get_agent_capability_note("codex")
        self.assertIn("快速代码生成", note)
        self.assertIn("简洁直接", note)

    def test_agent_capability_gemini(self):
        """Gemini agent should have multi-modal notes."""
        note = _get_agent_capability_note("gemini")
        self.assertIn("多模态", note)
        self.assertIn("图像", note)

    def test_agent_capability_traex(self):
        """Traex agent should have high concurrency notes."""
        note = _get_agent_capability_note("traex")
        self.assertIn("高并发", note)
        self.assertIn("轻量任务", note)

    def test_unknown_agent_defaults_to_coco(self):
        """Unknown agent type should default to coco capability notes."""
        note_unknown = _get_agent_capability_note("unknown_agent")
        note_coco = _get_agent_capability_note("coco")
        self.assertEqual(note_unknown, note_coco)

    def test_different_agents_produce_different_notes(self):
        """Different agent types should produce different capability notes."""
        note_coco = _get_agent_capability_note("coco")
        note_claude = _get_agent_capability_note("claude")
        note_aiden = _get_agent_capability_note("aiden")
        self.assertNotEqual(note_coco, note_claude)
        self.assertNotEqual(note_coco, note_aiden)
        self.assertNotEqual(note_claude, note_aiden)


if __name__ == "__main__":
    unittest.main()
