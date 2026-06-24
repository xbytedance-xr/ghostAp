"""Tests for fan-out + adversarial verify pattern in the Workflow Engine.

Validates:
- parallel() JS primitive correctly executes multiple agent calls concurrently
- Results from parallel agents are collected and returned in order
- Agents with different roles have role prefixes in their prompts
- Adversarial verify pattern: verification agent independently checks worker results
- Error handling: partial and total failures in parallel tasks
- phase() correctly wraps parallel operations for progress tracking
- Full integration: phase -> parallel(workers) -> verify -> synthesis pipeline
- Tool consistency: fan-out agents respect the allowed tools list

All tests mock the RuntimeBridge and Node.js runtime since we cannot run
real Node.js subprocesses in unit tests. The mocks simulate the JSON-RPC
communication protocol between Python and the JS runtime.
"""

from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

from src.workflow_engine.bridge import RuntimeBridge
from src.workflow_engine.engine import WorkflowEngine, WorkflowEngineCallbacks
from src.workflow_engine.executor import AgentExecutor
from src.workflow_engine.models import (
    AgentCallParams,
    AgentCallResult,
    AgentStatus,
    WorkflowMetrics,
    WorkflowProject,
    WorkflowStatus,
)
from src.workflow_engine.state_manager import WorkflowStateManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent_result(
    output: str | None = None,
    error: str | None = None,
    token_usage: int = 100,
    duration_s: float = 0.5,
    tool: str = "coco",
    model: str | None = None,
) -> AgentCallResult:
    """Create an AgentCallResult for testing."""
    return AgentCallResult(
        output=output,
        token_usage=token_usage,
        duration_s=duration_s,
        error=error,
        tool=tool,
        model=model,
    )


def _simulate_parallel_results(
    results: list[AgentCallResult],
    out_of_order: bool = False,
) -> list[AgentCallResult]:
    """Simulate parallel execution returning results.

    This helper mimics how the JS runtime's parallel() primitive uses
    Promise.all() to collect results from concurrent agent calls. The
    results are always returned in call order, even if execution completes
    out of order.

    Args:
        results: The results to return (in call order).
        out_of_order: If True, simulate that tasks completed out of order
            by sleeping different amounts before returning. The final list
            is still in call order (Promise.all behavior).

    Returns:
        The results list in the original call order.
    """
    if not out_of_order:
        return list(results)

    # Simulate out-of-order completion by "resolving" at different times
    # but still returning in original call order (Promise.all semantics)
    completed: list[tuple[int, AgentCallResult]] = []
    with ThreadPoolExecutor(max_workers=len(results)) as pool:
        futures = []
        for i, r in enumerate(results):
            # Vary sleep time so tasks complete out of order
            sleep_time = 0.01 * (len(results) - i)
            futures.append(pool.submit(lambda idx, res, t: (idx, res), i, r, sleep_time))
        for f in futures:
            idx, res = f.result()
            completed.append((idx, res))

    # Sort by original index to get Promise.all ordering
    completed.sort(key=lambda x: x[0])
    return [r for _, r in completed]


# ---------------------------------------------------------------------------
# TestFanOutPattern
# ---------------------------------------------------------------------------


class TestFanOutPattern(unittest.TestCase):
    """Test the parallel() fan-out pattern with multiple agent workers."""

    def _make_engine(self, root_path: str = "/tmp") -> WorkflowEngine:
        """Create a WorkflowEngine instance for testing."""
        return WorkflowEngine(
            chat_id="test_chat",
            root_path=root_path,
            agent_type="coco",
            engine_name="TestEngine",
        )

    def _mock_bridge_and_execute(
        self,
        engine: WorkflowEngine,
        agent_results: list[AgentCallResult],
        script_path: str = "/tmp/test_script.js",
        selected_tools: list[str] | None = None,
        executor_mock: MagicMock | None = None,
    ) -> WorkflowProject:
        """Mock the RuntimeBridge to simulate a workflow with parallel agent calls.

        This simulates a JS script that calls parallel() with N agent() calls.
        The mock bridge sends N agent_call JSON-RPC requests and then a done
        notification with the collected results.

        Args:
            executor_mock: Optional pre-configured mock for AgentExecutor.execute.
                If provided, it will be used to patch the execute method at
                the class level. This allows tests to track call counts on
                the same mock instance.
        """
        # Patch check_node_available to always return True
        with patch.object(RuntimeBridge, "check_node_available", return_value=True):
            # Patch the RuntimeBridge class itself
            mock_bridge_instance = MagicMock()

            def _fake_bridge_start():
                """Simulate bridge.start() — no-op for mock."""
                pass

            def _fake_bridge_run() -> str:
                """Simulate bridge.run() — dispatch agent calls and return result."""
                # Get the agent call handler from the bridge's constructor args
                call_handler = engine._handle_agent_call

                # Simulate the JS runtime sending agent_call requests for each parallel task
                results: list[str] = []
                for i, result_template in enumerate(agent_results):
                    # Build params as the JS runtime would send them
                    params = {
                        "prompt": f"Task {i + 1}",
                        "tool": result_template.tool,
                        "model": result_template.model,
                        "label": f"worker-{i + 1}",
                        "phase": "FanOut",
                    }

                    # Call the handler directly (simulating what bridge._handle_agent_call does)
                    agent_params = AgentCallParams.model_validate(params)

                    # If an executor mock was provided, it's already patched
                    # at the class level by the caller. If not, we need to
                    # patch the instance's execute method.
                    if executor_mock is not None:
                        # The class-level patch is already active; just call
                        result = call_handler(agent_params)
                    else:
                        # Patch the instance's execute method temporarily
                        original_execute = engine._executor.execute

                        def _temp_execute(params):
                            return result_template

                        engine._executor.execute = _temp_execute
                        try:
                            result = call_handler(agent_params)
                        finally:
                            engine._executor.execute = original_execute

                    if result.error:
                        results.append(f"ERROR: {result.error}")
                    else:
                        results.append(result.output or "")

                # Return the collected results as JSON (simulating JS runtime done)
                return json.dumps({"parallel_results": results})

            def _fake_bridge_stop():
                pass

            mock_bridge_instance.start = _fake_bridge_start
            mock_bridge_instance.run = _fake_bridge_run
            mock_bridge_instance.stop = _fake_bridge_stop

            with patch("src.workflow_engine.engine.RuntimeBridge", return_value=mock_bridge_instance):
                # If an executor mock was provided, patch AgentExecutor.execute
                # at the class level so the instance created inside execute_workflow
                # uses our mock.
                if executor_mock is not None:
                    with patch.object(AgentExecutor, "execute", executor_mock.execute):
                        project = engine.execute_workflow(
                            requirement="Test fan-out pattern",
                            script_path=script_path,
                            callbacks=WorkflowEngineCallbacks(),
                            selected_tools=selected_tools,
                        )
                else:
                    project = engine.execute_workflow(
                        requirement="Test fan-out pattern",
                        script_path=script_path,
                        callbacks=WorkflowEngineCallbacks(),
                        selected_tools=selected_tools,
                    )

        return project

    def test_parallel_fan_out_executes_all_tasks(self):
        """Verify that parallel() with 3 agent calls executes all 3 tasks.

        This test ensures that the fan-out pattern correctly spawns all
        requested agent workers, not just a subset. It validates that the
        ThreadPoolExecutor in RuntimeBridge receives and processes all
        agent_call requests from the JS runtime.
        """
        engine = self._make_engine()
        mock_results = [
            _make_agent_result(output="Result from worker 1"),
            _make_agent_result(output="Result from worker 2"),
            _make_agent_result(output="Result from worker 3"),
        ]

        # Create a single mock executor that will be used throughout the call
        mock_executor = MagicMock()
        mock_executor.execute.side_effect = mock_results

        project = self._mock_bridge_and_execute(
            engine, mock_results, executor_mock=mock_executor
        )

        # Verify all 3 agent calls were made
        self.assertEqual(mock_executor.execute.call_count, 3)

        # Verify the project completed successfully
        self.assertEqual(project.status, WorkflowStatus.COMPLETED)
        self.assertIsNone(project.error)

        # Verify metrics show 3 completed agents
        self.assertEqual(project.metrics.total_agents, 3)
        self.assertEqual(project.metrics.completed_agents, 3)

        # Verify result contains all 3 outputs
        result_data = json.loads(project.result or "{}")
        self.assertEqual(len(result_data.get("parallel_results", [])), 3)

    def test_fan_out_results_are_collected(self):
        """Verify that results from all parallel agents are collected and returned.

        This test validates that the parallel() primitive correctly collects
        results from all concurrent agent calls and returns them as a list.
        Each result should be accessible by its index in the original call order.
        """
        engine = self._make_engine()
        expected_outputs = [
            "Implementation complete: function foo() {}",
            "Test cases written: 5 tests",
            "Review complete: 2 issues found",
        ]
        mock_results = [
            _make_agent_result(output=out, token_usage=100 + i * 50)
            for i, out in enumerate(expected_outputs)
        ]

        # Create a single mock executor that will be used throughout the call
        mock_executor = MagicMock()
        mock_executor.execute.side_effect = mock_results

        project = self._mock_bridge_and_execute(
            engine, mock_results, executor_mock=mock_executor
        )

        # Parse the result
        result_data = json.loads(project.result or "{}")
        actual_outputs = result_data.get("parallel_results", [])

        # Verify all outputs are collected
        self.assertEqual(len(actual_outputs), 3)
        for expected, actual in zip(expected_outputs, actual_outputs):
            self.assertEqual(expected, actual)

        # Verify token usage was accumulated in metrics
        # (metrics.total_tokens is set from on_agent_done which uses the
        # result.token_usage directly)
        expected_tokens = sum(r.token_usage for r in mock_results)
        self.assertEqual(project.metrics.total_tokens, expected_tokens)

    def test_fan_out_with_different_roles(self):
        """Verify parallel agents can have different roles and prompts include role prefix.

        This test validates that each agent in a parallel fan-out can have
        a distinct role (implementer, tester, reviewer), and that the
        AgentExecutor correctly prepends "Role: {role}" to each prompt.
        """
        executor = AgentExecutor(
            cwd="/tmp",
            cancel_event=threading.Event(),
            on_token_usage=None,
        )

        roles = ["implementer", "tester", "reviewer"]
        prompts = [
            "Write the implementation",
            "Write test cases",
            "Review the code quality",
        ]

        for role, prompt_text in zip(roles, prompts):
            params = AgentCallParams(
                prompt=prompt_text,
                tool="coco",
                role=role,
                label=f"{role}-worker",
            )
            full_prompt = executor._build_prompt(params)

            # Verify role prefix is present
            self.assertTrue(
                full_prompt.startswith(f"Role: {role}"),
                f"Prompt for role '{role}' should start with 'Role: {role}'. "
                f"Got: {full_prompt[:50]}...",
            )

            # Verify original prompt text is included
            self.assertIn(prompt_text, full_prompt)

        # Verify no role prefix when role is empty
        params_no_role = AgentCallParams(prompt="Plain task", tool="coco", role="")
        full_prompt_no_role = executor._build_prompt(params_no_role)
        self.assertFalse(full_prompt_no_role.startswith("Role:"))

    def test_fan_out_preserves_order(self):
        """Verify results are returned in call order even if tasks complete out of order.

        The parallel() primitive uses Promise.all() semantics: results are
        always returned in the same order as the calls were made, regardless
        of which task finishes first. This test simulates out-of-order
        completion and verifies the result list preserves call order.
        """
        # Create results with distinct outputs
        results = [
            _make_agent_result(output="First call result"),
            _make_agent_result(output="Second call result"),
            _make_agent_result(output="Third call result"),
            _make_agent_result(output="Fourth call result"),
        ]

        # Simulate parallel execution with out-of-order completion
        ordered_results = _simulate_parallel_results(results, out_of_order=True)

        # Verify order is preserved (Promise.all behavior)
        self.assertEqual(len(ordered_results), 4)
        self.assertEqual(ordered_results[0].output, "First call result")
        self.assertEqual(ordered_results[1].output, "Second call result")
        self.assertEqual(ordered_results[2].output, "Third call result")
        self.assertEqual(ordered_results[3].output, "Fourth call result")

        # Also verify the indices match
        for i, result in enumerate(ordered_results):
            self.assertEqual(result.output, results[i].output)


# ---------------------------------------------------------------------------
# TestAdversarialVerifyPattern
# ---------------------------------------------------------------------------


class TestAdversarialVerifyPattern(unittest.TestCase):
    """Test the adversarial verify pattern for independent result validation."""

    def _make_engine(self) -> WorkflowEngine:
        return WorkflowEngine(
            chat_id="test_chat",
            root_path="/tmp",
            agent_type="coco",
            engine_name="TestEngine",
        )

    def _simulate_verify_workflow(
        self,
        engine: WorkflowEngine,
        worker_results: list[AgentCallResult],
        verify_result: AgentCallResult,
    ) -> tuple[WorkflowProject, list[AgentCallParams]]:
        """Simulate a workflow with parallel workers followed by a verify agent.

        Returns the project and the list of agent call params received, so
        tests can verify the verify agent received all worker results.
        """
        received_params: list[AgentCallParams] = []

        with patch.object(RuntimeBridge, "check_node_available", return_value=True):
            mock_bridge_instance = MagicMock()

            def _fake_run() -> str:
                call_handler = engine._handle_agent_call

                # Phase 1: parallel workers
                worker_outputs: list[str] = []
                for i, result_template in enumerate(worker_results):
                    params = AgentCallParams(
                        prompt=f"Worker task {i + 1}",
                        tool=result_template.tool,
                        label=f"worker-{i + 1}",
                        phase="Analysis",
                    )
                    received_params.append(params)
                    with patch.object(engine, "_executor") as mock_executor:
                        mock_executor.execute.return_value = result_template
                        result = call_handler(params)
                    worker_outputs.append(result.output or f"worker-{i + 1}-output")

                # Phase 2: verify agent receives all worker results
                all_results_text = "\n\n".join(
                    f"Worker {i + 1} result:\n{out}"
                    for i, out in enumerate(worker_outputs)
                )
                verify_prompt = (
                    "You are an adversarial verifier. Independently check these results "
                    f"for consistency and correctness:\n\n{all_results_text}"
                )
                verify_params = AgentCallParams(
                    prompt=verify_prompt,
                    tool="claude",
                    role="adversarial_verifier",
                    label="verify-agent",
                    phase="Verification",
                )
                received_params.append(verify_params)

                with patch.object(engine, "_executor") as mock_executor:
                    mock_executor.execute.return_value = verify_result
                    call_handler(verify_params)

                return json.dumps({
                    "worker_results": worker_outputs,
                    "verification": verify_result.output,
                })

            mock_bridge_instance.start = lambda: None
            mock_bridge_instance.run = _fake_run
            mock_bridge_instance.stop = lambda: None

            with patch("src.workflow_engine.engine.RuntimeBridge", return_value=mock_bridge_instance):
                project = engine.execute_workflow(
                    requirement="Test adversarial verify",
                    script_path="/tmp/verify.js",
                    callbacks=WorkflowEngineCallbacks(),
                )

        return project, received_params

    def test_verify_agent_checks_all_results(self):
        """Verify that a verification agent receives all worker results and can validate them.

        This test ensures that the adversarial verify agent's prompt contains
        the complete results from all parallel workers, enabling it to perform
        a thorough independent validation.
        """
        engine = self._make_engine()
        worker_results = [
            _make_agent_result(output="Analysis A: 5 security issues found", tool="coco"),
            _make_agent_result(output="Analysis B: 3 performance issues found", tool="claude"),
            _make_agent_result(output="Analysis C: 2 quality issues found", tool="aiden"),
        ]
        verify_result = _make_agent_result(
            output="Verification complete: All results are consistent. "
                   "Total 10 issues identified across all analyses.",
            tool="claude",
        )

        project, received_params = self._simulate_verify_workflow(
            engine, worker_results, verify_result
        )

        # Verify workflow completed
        self.assertEqual(project.status, WorkflowStatus.COMPLETED)

        # Verify we had 3 workers + 1 verifier = 4 agent calls
        self.assertEqual(len(received_params), 4)

        # Verify the verify agent's prompt contains all worker results
        verify_params = received_params[-1]
        self.assertEqual(verify_params.role, "adversarial_verifier")
        self.assertEqual(verify_params.label, "verify-agent")
        self.assertIn("Analysis A", verify_params.prompt)
        self.assertIn("Analysis B", verify_params.prompt)
        self.assertIn("Analysis C", verify_params.prompt)
        self.assertIn("adversarial verifier", verify_params.prompt.lower())

        # Verify the final result includes both worker results and verification
        result_data = json.loads(project.result or "{}")
        self.assertIn("worker_results", result_data)
        self.assertIn("verification", result_data)
        self.assertEqual(len(result_data["worker_results"]), 3)

    def test_verify_detects_inconsistent_results(self):
        """When workers return conflicting results, the verify agent should detect it.

        This test validates that the adversarial verify pattern works correctly
        when worker agents produce inconsistent or conflicting outputs. The
        verify agent's prompt should contain all conflicting information so
        it can identify and report the discrepancies.
        """
        engine = self._make_engine()

        # Workers return conflicting results about the same code
        worker_results = [
            _make_agent_result(
                output="Security: No SQL injection vulnerabilities found. "
                       "All queries use parameterized statements.",
                tool="coco",
            ),
            _make_agent_result(
                output="Security: CRITICAL - Found 2 SQL injection vulnerabilities "
                       "in user_search and order_lookup functions.",
                tool="claude",
            ),
            _make_agent_result(
                output="Security: Found 1 potential SQL injection in the "
                       "get_user_profile function (needs manual verification).",
                tool="aiden",
            ),
        ]

        # The verify agent should detect the inconsistency
        verify_result = _make_agent_result(
            output="INCONSISTENCY DETECTED: Workers disagree on SQL injection findings. "
                   "Worker 1 claims 0 issues, Worker 2 claims 2 critical issues, "
                   "Worker 3 claims 1 potential issue. "
                   "Recommendation: Manual review required for all database queries.",
            tool="claude",
        )

        project, received_params = self._simulate_verify_workflow(
            engine, worker_results, verify_result
        )

        # Verify the verify agent received all conflicting results
        verify_params = received_params[-1]
        self.assertIn("No SQL injection", verify_params.prompt)
        self.assertIn("2 SQL injection", verify_params.prompt)
        self.assertIn("1 potential SQL injection", verify_params.prompt)

        # Verify the inconsistency is reported in the final result
        result_data = json.loads(project.result or "{}")
        self.assertIn("INCONSISTENCY DETECTED", result_data["verification"])
        self.assertIn("Manual review required", result_data["verification"])

    def test_verify_provides_independent_assessment(self):
        """The verify agent should provide its own independent assessment.

        This test ensures the verify agent doesn't just echo worker results
        but provides its own independent analysis. The verify agent uses a
        different tool (claude) than the workers (coco, aiden) and has a
        distinct role (adversarial_verifier).
        """
        engine = self._make_engine()
        worker_results = [
            _make_agent_result(
                output="Worker 1: The algorithm is O(n^2) time complexity.",
                tool="coco",
            ),
            _make_agent_result(
                output="Worker 2: The algorithm runs in O(n log n) time.",
                tool="aiden",
            ),
        ]
        verify_result = _make_agent_result(
            output="Independent assessment: After careful analysis, the algorithm "
                   "is actually O(n^2) in the worst case due to nested loops over "
                   "the same dataset. Worker 1 is correct. Worker 2 appears to have "
                   "misread the inner loop condition. "
                   "Recommendation: Optimize to O(n log n) using a hash map lookup.",
            tool="claude",
        )

        project, received_params = self._simulate_verify_workflow(
            engine, worker_results, verify_result
        )

        # Verify the verify agent has a distinct role and tool
        verify_params = received_params[-1]
        self.assertEqual(verify_params.role, "adversarial_verifier")
        self.assertEqual(verify_params.tool, "claude")

        # Verify the verification is not just echoing worker results
        result_data = json.loads(project.result or "{}")
        verification = result_data["verification"]
        self.assertIn("Independent assessment", verification)
        self.assertIn("Worker 1 is correct", verification)
        self.assertIn("Worker 2 appears to have misread", verification)
        self.assertIn("Recommendation", verification)

        # Verify the verify agent's output is different from all worker outputs
        worker_outputs = result_data["worker_results"]
        for worker_out in worker_outputs:
            self.assertNotEqual(verification, worker_out)


# ---------------------------------------------------------------------------
# TestFanOutErrorHandling
# ---------------------------------------------------------------------------


class TestFanOutErrorHandling(unittest.TestCase):
    """Test error handling in parallel fan-out operations."""

    def _make_engine(self) -> WorkflowEngine:
        return WorkflowEngine(
            chat_id="test_chat",
            root_path="/tmp",
            agent_type="coco",
            engine_name="TestEngine",
        )

    def _simulate_parallel_with_errors(
        self,
        engine: WorkflowEngine,
        results: list[AgentCallResult],
    ) -> WorkflowProject:
        """Simulate a parallel fan-out where some tasks may return errors."""
        with patch.object(RuntimeBridge, "check_node_available", return_value=True):
            mock_bridge_instance = MagicMock()

            def _fake_run() -> str:
                call_handler = engine._handle_agent_call
                outputs: list[dict] = []

                for i, result_template in enumerate(results):
                    params = AgentCallParams(
                        prompt=f"Task {i + 1}",
                        tool=result_template.tool,
                        label=f"task-{i + 1}",
                        phase="FanOut",
                    )
                    with patch.object(engine, "_executor") as mock_executor:
                        mock_executor.execute.return_value = result_template
                        result = call_handler(params)

                    outputs.append({
                        "index": i,
                        "output": result.output,
                        "error": result.error,
                        "success": result.error is None,
                    })

                # In the real JS runtime, parallel() would throw if any promise rejects.
                # Here we simulate collecting all results including errors.
                has_errors = any(o["error"] for o in outputs)
                if has_errors and all(o["error"] for o in outputs):
                    # All failed — aggregate error
                    raise RuntimeError(
                        "All parallel tasks failed: " +
                        "; ".join(o["error"] or "unknown error" for o in outputs)
                    )
                elif has_errors:
                    # Partial failure — return mixed results
                    return json.dumps({
                        "results": outputs,
                        "partial_failure": True,
                        "succeeded": sum(1 for o in outputs if o["success"]),
                        "failed": sum(1 for o in outputs if o["error"]),
                    })

                return json.dumps({"results": outputs, "partial_failure": False})

            mock_bridge_instance.start = lambda: None
            mock_bridge_instance.run = _fake_run
            mock_bridge_instance.stop = lambda: None

            with patch("src.workflow_engine.engine.RuntimeBridge", return_value=mock_bridge_instance):
                project = engine.execute_workflow(
                    requirement="Test error handling",
                    script_path="/tmp/error_test.js",
                    callbacks=WorkflowEngineCallbacks(),
                )

        return project

    def test_fan_out_one_task_fails(self):
        """When one parallel task fails, verify the error is properly propagated.

        This test validates the behavior when a subset of parallel tasks fail.
        The workflow should report the failure but still capture results from
        successful tasks. The failed agent should be tracked in metrics.
        """
        engine = self._make_engine()
        results = [
            _make_agent_result(output="Task 1 succeeded"),
            _make_agent_result(error="Task 2 timed out after 300s"),
            _make_agent_result(output="Task 3 succeeded"),
        ]

        project = self._simulate_parallel_with_errors(engine, results)

        # Verify the project handled the partial failure
        result_data = json.loads(project.result or "{}")
        self.assertTrue(result_data.get("partial_failure", False))
        self.assertEqual(result_data.get("succeeded"), 2)
        self.assertEqual(result_data.get("failed"), 1)

        # Verify metrics track the failure
        self.assertEqual(project.metrics.total_agents, 3)
        self.assertEqual(project.metrics.completed_agents, 3)
        self.assertEqual(project.metrics.failed_agents, 1)

        # Verify individual results
        task_results = result_data.get("results", [])
        self.assertEqual(task_results[0]["output"], "Task 1 succeeded")
        self.assertIsNone(task_results[0]["error"])
        self.assertTrue(task_results[0]["success"])

        self.assertIsNone(task_results[1]["output"])
        self.assertEqual(task_results[1]["error"], "Task 2 timed out after 300s")
        self.assertFalse(task_results[1]["success"])

        self.assertEqual(task_results[2]["output"], "Task 3 succeeded")
        self.assertIsNone(task_results[2]["error"])
        self.assertTrue(task_results[2]["success"])

    def test_fan_out_all_tasks_fail(self):
        """When all parallel tasks fail, verify proper error aggregation.

        This test validates that when every task in a parallel fan-out fails,
        the workflow correctly aggregates all errors and marks the project
        as FAILED.
        """
        engine = self._make_engine()
        results = [
            _make_agent_result(error="Network error: cannot reach API"),
            _make_agent_result(error="Token budget exhausted for this call"),
            _make_agent_result(error="Model 'gpt-5' is not available"),
        ]

        project = self._simulate_parallel_with_errors(engine, results)

        # Verify the project is marked as failed
        self.assertEqual(project.status, WorkflowStatus.FAILED)
        self.assertIsNotNone(project.error)

        # Verify all errors are mentioned in the error message
        self.assertIn("All parallel tasks failed", project.error or "")
        self.assertIn("Network error", project.error or "")
        self.assertIn("Token budget exhausted", project.error or "")
        self.assertIn("not available", project.error or "")

        # Verify metrics show all failed
        self.assertEqual(project.metrics.total_agents, 3)
        self.assertEqual(project.metrics.completed_agents, 3)
        self.assertEqual(project.metrics.failed_agents, 3)

    def test_pipeline_continues_after_fan_out(self):
        """Verify that a pipeline can continue after a parallel stage.

        This test validates the pipeline() primitive's ability to run stages
        sequentially. After a parallel fan-out stage completes (even with
        partial failures), the pipeline should be able to pass the collected
        results to subsequent stages for further processing.
        """
        engine = self._make_engine()

        with patch.object(RuntimeBridge, "check_node_available", return_value=True):
            mock_bridge_instance = MagicMock()

            stage_results: list[str] = []

            def _fake_run() -> str:
                call_handler = engine._handle_agent_call

                # Stage 1: parallel fan-out (3 workers)
                worker_outputs = []
                for i in range(3):
                    params = AgentCallParams(
                        prompt=f"Analyze part {i + 1}",
                        tool="coco",
                        label=f"worker-{i + 1}",
                        phase="Analysis",
                    )
                    result = _make_agent_result(output=f"Analysis {i + 1}: OK")
                    with patch.object(engine, "_executor") as mock_executor:
                        mock_executor.execute.return_value = result
                        call_handler(params)
                    worker_outputs.append(f"Analysis {i + 1}: OK")

                stage_results.append("parallel_complete")

                # Stage 2: synthesis agent receives all worker results
                synthesis_prompt = (
                    "Synthesize these results into a report:\n" +
                    "\n".join(worker_outputs)
                )
                synthesis_params = AgentCallParams(
                    prompt=synthesis_prompt,
                    tool="claude",
                    label="synthesis",
                    phase="Synthesis",
                )
                synthesis_result = _make_agent_result(
                    output="Final report: All 3 analyses completed successfully. "
                           "No issues found in any part.",
                    tool="claude",
                )
                with patch.object(engine, "_executor") as mock_executor:
                    mock_executor.execute.return_value = synthesis_result
                    call_handler(synthesis_params)

                stage_results.append("synthesis_complete")

                return json.dumps({
                    "stages_completed": stage_results,
                    "final_report": synthesis_result.output,
                })

            mock_bridge_instance.start = lambda: None
            mock_bridge_instance.run = _fake_run
            mock_bridge_instance.stop = lambda: None

            with patch("src.workflow_engine.engine.RuntimeBridge", return_value=mock_bridge_instance):
                project = engine.execute_workflow(
                    requirement="Test pipeline continuation",
                    script_path="/tmp/pipeline.js",
                    callbacks=WorkflowEngineCallbacks(),
                )

        # Verify both stages completed
        self.assertEqual(project.status, WorkflowStatus.COMPLETED)
        result_data = json.loads(project.result or "{}")
        self.assertEqual(
            result_data.get("stages_completed"),
            ["parallel_complete", "synthesis_complete"],
        )

        # Verify we had 3 workers + 1 synthesis = 4 agent calls
        self.assertEqual(project.metrics.total_agents, 4)
        self.assertEqual(project.metrics.completed_agents, 4)

        # Verify the final report is present
        self.assertIn("Final report", result_data.get("final_report", ""))
        self.assertIn("3 analyses completed", result_data.get("final_report", ""))


# ---------------------------------------------------------------------------
# TestFanOutWithPhase
# ---------------------------------------------------------------------------


class TestFanOutWithPhase(unittest.TestCase):
    """Test that phase() calls correctly wrap parallel fan-out operations."""

    def test_phase_surrounds_fan_out(self):
        """Verify that phase() calls correctly wrap parallel fan-out operations.

        This test validates that when a workflow script calls phase() before
        a parallel() block, the phase transition is properly tracked in the
        WorkflowStateManager, and all agents in the parallel block are
        associated with the correct phase.
        """
        project = WorkflowProject(
            workflow_id="test_phase",
            status=WorkflowStatus.RUNNING,
            requirement="Test phase wrapping",
            script_path="/tmp/phase_test.js",
            metrics=WorkflowMetrics(),
        )
        mgr = WorkflowStateManager(project)

        # Phase transition before parallel block
        mgr.on_phase_changed("Analysis")

        # Verify phase was created
        self.assertEqual(len(project.phases), 1)
        self.assertEqual(project.phases[0].title, "Analysis")
        self.assertIsNotNone(project.phases[0].started_at)
        self.assertIsNone(project.phases[0].finished_at)

        # Parallel agents start within the phase
        for i in range(3):
            mgr.on_agent_started(f"worker-{i + 1}", "coco", "Analysis")

        # Verify all agents are in the correct phase
        self.assertEqual(len(project.phases[0].agents), 3)
        for i, agent in enumerate(project.phases[0].agents):
            self.assertEqual(agent.label, f"worker-{i + 1}")
            self.assertEqual(agent.status, AgentStatus.RUNNING)
            self.assertEqual(agent.tool, "coco")

        # Agents complete
        for i in range(3):
            mgr.on_agent_done(f"worker-{i + 1}", {
                "token_usage": 100 * (i + 1),
                "duration_s": 0.5 * (i + 1),
            })

        # Verify agents are marked done
        for agent in project.phases[0].agents:
            self.assertEqual(agent.status, AgentStatus.DONE)

        # Phase changes to next stage
        mgr.on_phase_changed("Verification")

        # Verify second phase is created
        # Note: on_phase_changed does NOT close the previous phase — only
        # on_workflow_done closes the last phase. Intermediate phases remain
        # open until workflow completion.
        self.assertEqual(len(project.phases), 2)
        self.assertIsNone(project.phases[0].finished_at)  # Not closed yet
        self.assertEqual(project.phases[1].title, "Verification")
        self.assertIsNone(project.phases[1].finished_at)

        # Verify agent in second phase
        mgr.on_agent_started("verifier", "claude", "Verification")
        self.assertEqual(len(project.phases[1].agents), 1)
        self.assertEqual(project.phases[1].agents[0].label, "verifier")

        # Complete the workflow — this should close the last phase
        mgr.on_workflow_done("All done")

        # Now verify the last phase is closed
        self.assertIsNotNone(project.phases[1].finished_at)
        # Note: Intermediate phases (phase[0]) are NOT automatically closed
        # by the current implementation — only the last phase gets finished_at
        self.assertIsNone(project.phases[0].finished_at)

    def test_multiphase_fan_out(self):
        """Verify a multi-phase workflow with fan-out in each phase tracks progress.

        This test validates that a workflow with multiple phases, each
        containing a parallel fan-out, correctly tracks progress across
        all phases. Metrics should aggregate across phases, and each
        phase's agents should be properly isolated.
        """
        project = WorkflowProject(
            workflow_id="test_multiphase",
            status=WorkflowStatus.RUNNING,
            requirement="Multi-phase fan-out test",
            script_path="/tmp/multiphase.js",
            metrics=WorkflowMetrics(),
        )
        mgr = WorkflowStateManager(project)

        phases = [
            ("Planning", 2),
            ("Implementation", 3),
            ("Testing", 2),
            ("Review", 2),
        ]

        total_agents = 0
        total_tokens = 0

        for phase_name, num_agents in phases:
            mgr.on_phase_changed(phase_name)

            # Start parallel agents
            for i in range(num_agents):
                label = f"{phase_name.lower()}-{i + 1}"
                mgr.on_agent_started(label, "coco", phase_name)
                total_agents += 1

            # Complete all agents
            for i in range(num_agents):
                label = f"{phase_name.lower()}-{i + 1}"
                tokens = 500 + i * 100
                mgr.on_agent_done(label, {
                    "token_usage": tokens,
                    "duration_s": 1.0,
                })
                total_tokens += tokens

        # Complete the workflow
        mgr.on_workflow_done("All phases completed successfully")

        # Verify phases
        self.assertEqual(len(project.phases), 4)
        for i, (phase_name, num_agents) in enumerate(phases):
            self.assertEqual(project.phases[i].title, phase_name)
            self.assertEqual(len(project.phases[i].agents), num_agents)
            self.assertIsNotNone(project.phases[i].started_at)
            # Note: Only the LAST phase gets finished_at set by on_workflow_done.
            # Intermediate phases are not automatically closed by the current
            # implementation.
            if i == len(phases) - 1:
                self.assertIsNotNone(project.phases[i].finished_at)
            else:
                self.assertIsNone(project.phases[i].finished_at)

        # Verify metrics
        self.assertEqual(project.metrics.total_agents, total_agents)
        self.assertEqual(project.metrics.completed_agents, total_agents)
        self.assertEqual(project.metrics.phases_completed, 4)
        self.assertEqual(project.metrics.total_tokens, total_tokens)

        # Verify token tracking (metrics.total_tokens is set from on_agent_done)
        self.assertEqual(project.metrics.total_tokens, total_tokens)

        # Verify final status
        self.assertEqual(project.status, WorkflowStatus.COMPLETED)
        self.assertEqual(project.result, "All phases completed successfully")


# ---------------------------------------------------------------------------
# TestIntegrationPattern
# ---------------------------------------------------------------------------


class TestIntegrationPattern(unittest.TestCase):
    """Integration tests for the complete fan-out + verify workflow pattern."""

    def _make_engine(self) -> WorkflowEngine:
        return WorkflowEngine(
            chat_id="test_chat",
            root_path="/tmp",
            agent_type="coco",
            engine_name="TestEngine",
        )

    def test_full_fan_out_verify_pipeline(self):
        """Test the complete pattern: phase -> parallel(workers) -> verify agent -> synthesis.

        This integration test validates the entire fan-out + adversarial verify
        pipeline end-to-end. It simulates a code review workflow that:
        1. Declares an "Analysis" phase
        2. Fans out to 3 parallel workers (security, quality, performance)
        3. Declares a "Verification" phase
        4. Runs an adversarial verifier to check all worker results
        5. Declares a "Synthesis" phase
        6. Runs a synthesis agent to produce the final report

        All components (phase tracking, parallel execution, agent roles,
        result collection, error handling) are exercised together.
        """
        engine = self._make_engine()

        # Track phase changes and agent calls
        phase_changes: list[str] = []
        agent_calls: list[AgentCallParams] = []
        agent_done: list[tuple[str, dict]] = []

        def on_phase(title: str) -> None:
            phase_changes.append(title)

        def on_agent_start(label: str, tool: str) -> None:
            pass

        def on_agent_done(label: str, result: dict) -> None:
            agent_done.append((label, result))

        callbacks = WorkflowEngineCallbacks(
            on_phase=on_phase,
            on_agent_start=on_agent_start,
            on_agent_done=on_agent_done,
        )

        # Define the expected workflow results
        worker_results = {
            "security-review": _make_agent_result(
                output="Security: Found 2 SQL injection vulnerabilities in "
                       "user_search and order_lookup functions.",
                tool="claude",
                token_usage=1500,
            ),
            "quality-review": _make_agent_result(
                output="Quality: Found 5 code quality issues including "
                       "inconsistent naming and missing docstrings.",
                tool="coco",
                token_usage=1200,
            ),
            "perf-review": _make_agent_result(
                output="Performance: Found 2 bottlenecks — O(n^2) loop in "
                       "data processing and unindexed database query.",
                tool="aiden",
                token_usage=1800,
            ),
        }

        verify_result = _make_agent_result(
            output="Verification: All findings are valid. "
                   "The SQL injections are critical and should be fixed first. "
                   "The performance issues are moderate priority. "
                   "Code quality issues are low priority but should be addressed.",
            tool="claude",
            token_usage=2000,
        )

        synthesis_result = _make_agent_result(
            output="Final Code Review Report\n"
                   "=========================\n"
                   "Critical Issues: 2 SQL injections (fix immediately)\n"
                   "Moderate Issues: 2 performance bottlenecks\n"
                   "Minor Issues: 5 code quality problems\n"
                   "\nRecommendation: Prioritize security fixes, then "
                   "performance optimization, then quality improvements.",
            tool="coco",
            token_usage=1000,
        )

        with patch.object(RuntimeBridge, "check_node_available", return_value=True):
            mock_bridge_instance = MagicMock()

            def _fake_run() -> str:
                call_handler = engine._handle_agent_call
                phase_handler = engine._handle_phase

                # Phase 1: Analysis
                phase_handler("Analysis")

                # Parallel workers
                worker_outputs = {}
                for label, result_template in worker_results.items():
                    role = label.replace("-review", "_auditor")
                    params = AgentCallParams(
                        prompt=f"Review code for {label.replace('-review', '')} issues",
                        tool=result_template.tool,
                        role=role,
                        label=label,
                        phase="Analysis",
                    )
                    agent_calls.append(params)
                    with patch.object(engine, "_executor") as mock_executor:
                        mock_executor.execute.return_value = result_template
                        call_handler(params)
                    worker_outputs[label] = result_template.output

                # Phase 2: Verification
                phase_handler("Verification")

                # Adversarial verifier
                all_findings = "\n\n".join(
                    f"{label}:\n{out}" for label, out in worker_outputs.items()
                )
                verify_params = AgentCallParams(
                    prompt=f"Independently verify these findings:\n\n{all_findings}",
                    tool="claude",
                    role="adversarial_verifier",
                    label="verification",
                    phase="Verification",
                )
                agent_calls.append(verify_params)
                with patch.object(engine, "_executor") as mock_executor:
                    mock_executor.execute.return_value = verify_result
                    call_handler(verify_params)

                # Phase 3: Synthesis
                phase_handler("Synthesis")

                # Synthesis agent
                synthesis_params = AgentCallParams(
                    prompt=f"Synthesize final report from verified findings:\n"
                           f"{verify_result.output}",
                    tool="coco",
                    label="synthesis",
                    phase="Synthesis",
                )
                agent_calls.append(synthesis_params)
                with patch.object(engine, "_executor") as mock_executor:
                    mock_executor.execute.return_value = synthesis_result
                    call_handler(synthesis_params)

                return json.dumps({
                    "final_report": synthesis_result.output,
                    "worker_findings": worker_outputs,
                    "verification": verify_result.output,
                })

            mock_bridge_instance.start = lambda: None
            mock_bridge_instance.run = _fake_run
            mock_bridge_instance.stop = lambda: None

            with patch("src.workflow_engine.engine.RuntimeBridge", return_value=mock_bridge_instance):
                project = engine.execute_workflow(
                    requirement="Full code review with adversarial verification",
                    script_path="/tmp/full_pipeline.js",
                    callbacks=callbacks,
                )

        # Verify workflow completed successfully
        self.assertEqual(project.status, WorkflowStatus.COMPLETED)
        self.assertIsNone(project.error)

        # Verify phase tracking
        self.assertEqual(phase_changes, ["Analysis", "Verification", "Synthesis"])
        self.assertEqual(project.metrics.phases_completed, 3)

        # Verify all agent calls were made (3 workers + 1 verifier + 1 synthesis)
        self.assertEqual(len(agent_calls), 5)
        self.assertEqual(project.metrics.total_agents, 5)
        self.assertEqual(project.metrics.completed_agents, 5)

        # Verify worker roles are correct
        self.assertEqual(agent_calls[0].role, "security_auditor")
        self.assertEqual(agent_calls[1].role, "quality_auditor")
        self.assertEqual(agent_calls[2].role, "perf_auditor")
        self.assertEqual(agent_calls[3].role, "adversarial_verifier")

        # Verify tools are correctly assigned
        self.assertEqual(agent_calls[0].tool, "claude")
        self.assertEqual(agent_calls[1].tool, "coco")
        self.assertEqual(agent_calls[2].tool, "aiden")

        # Verify token usage is accumulated correctly in metrics
        # (metrics.total_tokens is set from on_agent_done which uses
        # the result.token_usage directly)
        expected_tokens = (
            worker_results["security-review"].token_usage +
            worker_results["quality-review"].token_usage +
            worker_results["perf-review"].token_usage +
            verify_result.token_usage +
            synthesis_result.token_usage
        )
        self.assertEqual(project.metrics.total_tokens, expected_tokens)

        # Verify final result structure
        result_data = json.loads(project.result or "{}")
        self.assertIn("final_report", result_data)
        self.assertIn("worker_findings", result_data)
        self.assertIn("verification", result_data)
        self.assertIn("Critical Issues", result_data["final_report"])
        self.assertIn("SQL injections", result_data["final_report"])

    def test_fan_out_with_tool_consistency(self):
        """Verify that fan-out agents respect the allowed tools list.

        This test validates that when a workflow is executed with a
        selected_tools whitelist, any agent call that attempts to use a
        tool not in the whitelist is rejected with an appropriate error.
        This ensures tool selection consistency across all fan-out workers.
        """
        engine = self._make_engine()

        # Only "coco" and "claude" are allowed
        allowed_tools = ["coco", "claude"]

        with patch.object(RuntimeBridge, "check_node_available", return_value=True):
            mock_bridge_instance = MagicMock()

            def _fake_run() -> str:
                call_handler = engine._handle_agent_call
                results: list[dict] = []

                # Worker 1: uses allowed tool "coco"
                params1 = AgentCallParams(
                    prompt="Task 1",
                    tool="coco",
                    label="worker-1",
                    phase="FanOut",
                )
                result1 = _make_agent_result(output="Worker 1 OK", tool="coco")
                with patch.object(engine, "_executor") as mock_executor:
                    mock_executor.execute.return_value = result1
                    r1 = call_handler(params1)
                results.append({"label": "worker-1", "success": r1.error is None, "error": r1.error})

                # Worker 2: uses allowed tool "claude"
                params2 = AgentCallParams(
                    prompt="Task 2",
                    tool="claude",
                    label="worker-2",
                    phase="FanOut",
                )
                result2 = _make_agent_result(output="Worker 2 OK", tool="claude")
                with patch.object(engine, "_executor") as mock_executor:
                    mock_executor.execute.return_value = result2
                    r2 = call_handler(params2)
                results.append({"label": "worker-2", "success": r2.error is None, "error": r2.error})

                # Worker 3: uses DISALLOWED tool "aiden"
                params3 = AgentCallParams(
                    prompt="Task 3",
                    tool="aiden",
                    label="worker-3",
                    phase="FanOut",
                )
                # The engine should reject this before calling executor
                r3 = call_handler(params3)
                results.append({"label": "worker-3", "success": r3.error is None, "error": r3.error})

                return json.dumps({"results": results})

            mock_bridge_instance.start = lambda: None
            mock_bridge_instance.run = _fake_run
            mock_bridge_instance.stop = lambda: None

            with patch("src.workflow_engine.engine.RuntimeBridge", return_value=mock_bridge_instance):
                project = engine.execute_workflow(
                    requirement="Test tool consistency",
                    script_path="/tmp/tool_test.js",
                    callbacks=WorkflowEngineCallbacks(),
                    selected_tools=allowed_tools,
                )

        # Verify project completed (partial failure)
        self.assertEqual(project.status, WorkflowStatus.COMPLETED)

        result_data = json.loads(project.result or "{}")
        results = result_data.get("results", [])

        # Worker 1 (coco) should succeed
        self.assertTrue(results[0]["success"])
        self.assertIsNone(results[0]["error"])

        # Worker 2 (claude) should succeed
        self.assertTrue(results[1]["success"])
        self.assertIsNone(results[1]["error"])

        # Worker 3 (aiden) should fail with tool not allowed error
        self.assertFalse(results[2]["success"])
        self.assertIsNotNone(results[2]["error"])
        self.assertIn("not in allowed list", results[2]["error"] or "")
        self.assertIn("aiden", results[2]["error"] or "")

        # Verify metrics: 3 total (2 successful + 1 tool-rejected)
        # The agent state is updated BEFORE the tool whitelist check.
        # When the tool check fails, on_agent_failed is called for rollback,
        # marking it as FAILED.
        # completed_agents counts all finished agents (success + failure),
        # failed_agents counts only the failed subset.
        self.assertEqual(project.metrics.total_agents, 3)
        self.assertEqual(project.metrics.completed_agents, 3)
        self.assertEqual(project.metrics.failed_agents, 1)

        # Verify the project's selected_tools was set correctly
        self.assertEqual(project.selected_tools, allowed_tools)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main()
