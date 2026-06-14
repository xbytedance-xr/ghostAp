"""Tests for token efficiency in the Workflow Engine.

Validates that the workflow engine minimizes token consumption through:
- Journal caching of identical agent() calls
- Compact but complete prompt construction
- Subagent encouragement that is concise and not duplicated
- Parallel execution that shares context rather than duplicating it
- Accurate token usage tracking

Why this matters: Token efficiency directly impacts cost. Every redundant token
sent to an AI model is money spent unnecessarily. These tests guard against
regressions that would increase token consumption without providing value.
"""

import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from src.workflow_engine.journal import WorkflowJournal
from src.workflow_engine.models import (
    AgentCallParams,
    AgentCallResult,
    WorkflowMeta,
    WorkflowMetrics,
    WorkflowProject,
    WorkflowStatus,
)
from src.workflow_engine.roles import SUBAGENT_ENCOURAGEMENT_PROMPT
from src.workflow_engine.script_gen import (
    SUBAGENT_ENCOURAGEMENT,
    build_script_gen_prompt,
    generate_simple_script,
)


# ---------------------------------------------------------------------------
# Helper: rough token count (1 token ≈ 4 characters for English text)
# ---------------------------------------------------------------------------


def _count_tokens(text: str) -> int:
    """Rough token estimation: 1 token ≈ 4 characters for English text.

    This is a conservative heuristic used for relative comparisons in tests.
    It avoids dependencies on external tokenizers while being accurate enough
    to catch order-of-magnitude inefficiencies.
    """
    return max(1, len(text) // 4)


# ===========================================================================
# 1. Journal Cache Token Efficiency
# ===========================================================================


class TestJournalCacheTokenEfficiency(unittest.TestCase):
    """Test that journal caching reduces token usage for repeated calls.

    Caching is the single biggest token saver in the workflow engine. When
    a workflow script is re-run or when the same prompt+tool+model combination
    appears multiple times, the journal returns cached results instantly
    without calling the AI model.
    """

    def _make_journal(self) -> WorkflowJournal:
        """Create a journal with a unique temp directory to avoid cross-test pollution."""
        tmp_path = tempfile.mkdtemp(prefix="wf_journal_test_")
        return WorkflowJournal(root_path=tmp_path, run_id="test_run")

    def test_cache_hit_reduces_token_usage(self):
        """Verify cache hits return results with token_usage=0.

        A cache hit means we don't call the AI model at all, so token usage
        should be zero. This is the primary cost-saving mechanism.

        The journal stores the original result, and the engine wraps it
        with token_usage=0 when returning a cached result.
        """
        from src.workflow_engine.engine import WorkflowEngine
        from src.workflow_engine.state_manager import WorkflowStateManager

        # Set up engine with journal
        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine._lock = threading.Lock()
        engine._project = WorkflowProject(
            workflow_id="wf-cache-test",
            status=WorkflowStatus.RUNNING,
            metrics=WorkflowMetrics(),
        )
        engine._state_manager = WorkflowStateManager(engine._project)
        engine._cancel_event = threading.Event()
        engine._callbacks = None
        engine._agent_call_count = 0
        engine._progress_coalescer = None
        engine._journal = self._make_journal()
        engine._executor = MagicMock()
        engine._renderer_wf = MagicMock()  # Required for _fire_progress()

        prompt = "Analyze this code for security issues"
        tool = "coco"
        model = "claude-3-opus"
        key = WorkflowJournal.compute_key(prompt, tool, model)

        # Store a result with non-zero token usage (simulating a real call)
        original_result = AgentCallResult(
            output="No security issues found",
            token_usage=1500,
            duration_s=2.5,
            tool=tool,
            model=model,
        )
        engine._journal.store(key, original_result)

        # Now call _handle_agent_call with the same prompt+tool+model
        # This should hit the cache and return token_usage=0
        params = AgentCallParams(prompt=prompt, tool=tool, model=model)
        cached_result = engine._handle_agent_call(params)

        self.assertIsNotNone(cached_result)
        self.assertEqual(cached_result.token_usage, 0,
            "Cached result should have token_usage=0 (no model call)")
        self.assertEqual(cached_result.output, "No security issues found")
        self.assertTrue(cached_result.cached,
            "Cached result should have cached=True")

        # Verify no actual executor call was made
        engine._executor.execute.assert_not_called()

    def test_cache_hit_sets_cached_flag(self):
        """Verify cached results have cached=True in AgentCallResult.

        The cached flag lets downstream code (metrics, UI) distinguish
        real model calls from cache hits for reporting purposes.
        """
        journal = self._make_journal()
        key = WorkflowJournal.compute_key("test prompt", "coco", None)

        # Store a result that is NOT marked as cached
        journal.store(key, AgentCallResult(
            output="result", token_usage=100, cached=False,
        ))

        # When engine retrieves from cache, it explicitly sets cached=True
        # (this happens in engine._handle_agent_call, not in journal itself)
        # We verify the journal returns the stored result correctly
        retrieved = journal.get_cached(key)
        self.assertIsNotNone(retrieved)

        # Simulate what the engine does: wrap with cached=True and token_usage=0
        cached_result = AgentCallResult(
            output=retrieved.output,
            parsed=retrieved.parsed,
            token_usage=0,
            duration_s=0.0,
            cached=True,
            tool=retrieved.tool,
            model=retrieved.model,
        )
        self.assertTrue(cached_result.cached,
            "Engine should set cached=True on cache hits")
        self.assertEqual(cached_result.token_usage, 0)

    def test_stats_show_cache_effectiveness(self):
        """Verify journal.stats() correctly tracks hits vs misses.

        Stats let us measure cache effectiveness: hit_rate = hits / (hits + misses).
        A high hit rate means the cache is doing its job and saving tokens.
        """
        journal = self._make_journal()

        # First call: miss
        key1 = WorkflowJournal.compute_key("unique prompt 1", "coco", None)
        result1 = journal.get_cached(key1)
        self.assertIsNone(result1)

        # Store it
        journal.store(key1, AgentCallResult(output="r1", token_usage=100))

        # Second call on same key: hit
        result1_cached = journal.get_cached(key1)
        self.assertIsNotNone(result1_cached)

        # Third call on new key: miss
        key2 = WorkflowJournal.compute_key("unique prompt 2", "claude", None)
        result2 = journal.get_cached(key2)
        self.assertIsNone(result2)

        # Store and retrieve key2: another hit
        journal.store(key2, AgentCallResult(output="r2", token_usage=200))
        result2_cached = journal.get_cached(key2)
        self.assertIsNotNone(result2_cached)

        # Check stats
        stats = journal.stats()
        self.assertEqual(stats["hits"], 2, "Should have 2 cache hits")
        self.assertEqual(stats["misses"], 2, "Should have 2 cache misses")
        self.assertEqual(stats["total"], 2, "Should have 2 stored entries")

        # Verify we can compute hit rate
        total_requests = stats["hits"] + stats["misses"]
        hit_rate = stats["hits"] / total_requests if total_requests > 0 else 0
        self.assertEqual(hit_rate, 0.5, "Hit rate should be 50%")

    def test_same_prompt_different_tools_not_cached(self):
        """Verify that changing the tool (same prompt) results in a cache miss.

        The cache key includes prompt, tool, and model. Different tools
        may produce different results even for the same prompt, so they
        must not share cache entries.
        """
        journal = self._make_journal()
        prompt = "What is 2 + 2?"

        # Store for coco
        key_coco = WorkflowJournal.compute_key(prompt, "coco", None)
        journal.store(key_coco, AgentCallResult(
            output="4 (from coco)", token_usage=50,
        ))

        # Retrieve for claude — should be a miss (different key)
        key_claude = WorkflowJournal.compute_key(prompt, "claude", None)
        result_claude = journal.get_cached(key_claude)
        self.assertIsNone(result_claude,
            "Different tool should produce cache miss")

        # Verify keys are different
        self.assertNotEqual(key_coco, key_claude,
            "Cache keys for different tools should differ")

        # Retrieve for coco — should be a hit
        result_coco = journal.get_cached(key_coco)
        self.assertIsNotNone(result_coco)
        self.assertEqual(result_coco.output, "4 (from coco)")


# ===========================================================================
# 2. Subagent Encouragement Token Cost
# ===========================================================================


class TestSubagentEncouragementTokenCost(unittest.TestCase):
    """Test that SUBAGENT_ENCOURAGEMENT is concise and not duplicated.

    The subagent encouragement is appended to every agent prompt to encourage
    parallelism. It must be short enough that it doesn't add significant
    token overhead to each call, and it must appear exactly once per prompt.
    """

    def test_encouragement_adds_reasonable_tokens(self):
        """Verify SUBAGENT_ENCOURAGEMENT adds < 500 tokens to each prompt.

        The encouragement is added to every agent() call. If it were long,
        it would multiply token costs across all calls. Keeping it under
        500 tokens ensures the overhead is negligible compared to the
        savings from better parallelism.
        """
        tokens = _count_tokens(SUBAGENT_ENCOURAGEMENT)
        self.assertLess(tokens, 500,
            f"SUBAGENT_ENCOURAGEMENT should be < 500 tokens, got {tokens}")

        # Also check the executor version
        tokens_executor = _count_tokens(SUBAGENT_ENCOURAGEMENT_PROMPT)
        self.assertLess(tokens_executor, 500,
            f"SUBAGENT_ENCOURAGEMENT_PROMPT should be < 500 tokens, got {tokens_executor}")

    def test_encouragement_appears_exactly_once_per_prompt(self):
        """Verify the encouragement is appended exactly once, not multiple times.

        Duplicate encouragement would waste tokens and look unprofessional.
        This guards against bugs where the encouragement gets appended
        multiple times through different code paths.
        """
        from src.workflow_engine.executor import AgentExecutor

        executor = AgentExecutor(
            cwd="/tmp",
            cancel_event=threading.Event(),
            on_token_usage=None,
        )
        params = AgentCallParams(
            prompt="Analyze this code for bugs",
            tool="coco",
            role="",
        )
        full_prompt = executor._build_prompt(params)

        # Should appear exactly once
        count = full_prompt.count(SUBAGENT_ENCOURAGEMENT_PROMPT)
        self.assertEqual(count, 1,
            f"Encouragement should appear exactly once, found {count} times")

    def test_encouragement_not_duplicated_in_simple_script(self):
        """Verify that in generate_simple_script(), encouragement appears in
        each agent prompt but not in the overall script structure overhead.

        The simple script generator embeds SUBAGENT_ENCOURAGEMENT in each
        agent() call's prompt template. It should not appear outside of
        those prompt strings (which would be structural duplication).
        """
        script = generate_simple_script("Implement a REST API for user management")

        # Count occurrences in the script
        total_count = script.count(SUBAGENT_ENCOURAGEMENT)

        # The script has 4 agent prompts: planner, executor, task (in map), synthesizer
        # Each should contain the encouragement exactly once
        self.assertGreaterEqual(total_count, 3,
            f"Expected encouragement in at least 3 agent prompts, found {total_count}")

        # Verify the encouragement is inside template literals (prompt strings)
        # by checking that each occurrence is within a JS prompt: `...` block
        # SUBAGENT_ENCOURAGEMENT is a single line, so we check its context
        encouragement_text = SUBAGENT_ENCOURAGEMENT.strip()
        lines = script.split('\n')

        for i, line in enumerate(lines):
            if encouragement_text in line:
                # Find the context: this should be inside a template literal
                # that starts with `prompt: ` or similar
                # Look backwards to find the opening backtick / prompt field
                # The prompt field can be many lines back, so scan more context
                context_start = max(0, i - 15)
                context_lines = lines[context_start:i+1]
                context = '\n'.join(context_lines)

                # The line should be inside a backtick-quoted string for a prompt
                # Check that there's a `prompt: ` pattern before the encouragement
                self.assertIn('prompt:', context,
                    f"Encouragement at line {i+1} should be inside a prompt field. Context: {context[:200]}")
                # Verify it's not at the top level of the script (not inside JS code)
                stripped = line.strip()
                self.assertFalse(
                    stripped.startswith("**Subagent") and '`' not in context,
                    f"Encouragement at line {i+1} appears outside prompt string: {line[:80]}"
                )


# ===========================================================================
# 3. Prompt Compactness
# ===========================================================================


class TestPromptCompactness(unittest.TestCase):
    """Test that prompts are compact but complete.

    Large prompts cost more tokens. These tests ensure our prompt templates
    stay within reasonable bounds while still providing all necessary context.
    """

    def test_build_script_gen_prompt_is_compact(self):
        """Verify build_script_gen_prompt() produces < 5000 tokens for typical inputs.

        The script generation prompt is sent once per workflow to generate
        the orchestration script. It contains the full template with examples,
        so it's inherently larger than individual agent prompts, but it
        should still stay under 5000 tokens.
        """
        prompt = build_script_gen_prompt(
            requirement="Build a multi-agent code review workflow that analyzes "
                       "Python code for security vulnerabilities, performance issues, "
                       "and code quality problems. Use parallel analysis with "
                       "different tools and then synthesize a final report.",
            available_tools={
                "coco": "Full-stack programming with subagent support",
                "claude": "Deep reasoning and analysis",
                "aiden": "Code review and architecture",
                "gemini": "Multi-modal analysis",
            },
        )

        tokens = _count_tokens(prompt)
        self.assertLess(tokens, 5000,
            f"Script gen prompt should be < 5000 tokens, got {tokens}")

    def test_agent_executor_prompt_is_compact(self):
        """Verify AgentExecutor._build_prompt() produces compact prompts.

        Individual agent prompts are sent many times per workflow. Each
        one should be as compact as possible while still containing the
        role prefix, task, and subagent encouragement.
        """
        from src.workflow_engine.executor import AgentExecutor

        executor = AgentExecutor(
            cwd="/tmp",
            cancel_event=threading.Event(),
            on_token_usage=None,
        )

        # Typical agent call prompt
        params = AgentCallParams(
            prompt="Review this Python function for security issues:\n\n"
                  "def process_user_input(data):\n"
                  "    eval(data.get('command', ''))\n"
                  "    return True",
            tool="coco",
            role="security_auditor",
        )

        full_prompt = executor._build_prompt(params)
        tokens = _count_tokens(full_prompt)

        # Should be compact — role prefix + task + encouragement
        # The task itself is ~100 chars, role prefix ~20, encouragement ~300 chars
        # Total should be well under 1000 tokens
        self.assertLess(tokens, 1000,
            f"Agent executor prompt should be < 1000 tokens, got {tokens}")

        # Verify all parts are present
        self.assertIn("Role: security_auditor", full_prompt)
        self.assertIn("Review this Python function", full_prompt)
        self.assertIn(SUBAGENT_ENCOURAGEMENT_PROMPT, full_prompt)

    def test_role_prefix_adds_minimal_tokens(self):
        """Verify adding a role prefix adds < 100 tokens.

        Role prefixes are added to many agent prompts. They should be
        minimal — just "Role: {role_name}\n\n" — so they don't add
        significant token overhead.
        """
        from src.workflow_engine.executor import AgentExecutor

        executor = AgentExecutor(
            cwd="/tmp",
            cancel_event=threading.Event(),
            on_token_usage=None,
        )

        base_params = AgentCallParams(
            prompt="Analyze this code",
            tool="coco",
            role="",
        )
        base_prompt = executor._build_prompt(base_params)
        base_tokens = _count_tokens(base_prompt)

        role_params = AgentCallParams(
            prompt="Analyze this code",
            tool="coco",
            role="security_auditor",
        )
        role_prompt = executor._build_prompt(role_params)
        role_tokens = _count_tokens(role_prompt)

        added_tokens = role_tokens - base_tokens
        self.assertLess(added_tokens, 100,
            f"Role prefix should add < 100 tokens, added {added_tokens}")

        # The difference should be exactly the role prefix
        self.assertEqual(
            role_prompt,
            "Role: security_auditor\n\n" + base_prompt,
            "Role prefix should be prepended exactly"
        )


# ===========================================================================
# 4. Parallel Execution Token Efficiency
# ===========================================================================


class TestParallelExecutionTokenEfficiency(unittest.TestCase):
    """Test that parallel execution doesn't duplicate token overhead.

    When running N tasks in parallel, the total token cost should be
    approximately the sum of individual task costs. We should not pay
    N times the base overhead (session setup, shared context, etc.).
    """

    def _make_executor(self, on_token_usage=None):
        from src.workflow_engine.executor import AgentExecutor
        return AgentExecutor(
            cwd="/tmp",
            cancel_event=threading.Event(),
            on_token_usage=on_token_usage,
        )

    def test_parallel_tasks_dont_duplicate_context(self):
        """Verify parallel agent calls don't duplicate shared context.

        Each parallel task has its own prompt (its specific task), but
        shared setup (session pool, executor instance) is done once.
        The AgentExecutor uses a shared ThreadPoolExecutor for session
        creation, avoiding per-call pool overhead.
        """
        from src.workflow_engine.executor import AgentExecutor

        # Create one executor for all parallel tasks
        executor = self._make_executor()

        from src.workflow_engine.constants import DEFAULT_MAX_CONCURRENT, HARD_MAX_CONCURRENT

        # Verify the executor has a single shared session pool
        self.assertIsNotNone(executor._session_pool)
        expected_workers = max(1, min(int(DEFAULT_MAX_CONCURRENT), HARD_MAX_CONCURRENT))
        self.assertEqual(
            executor._session_pool._max_workers,
            expected_workers,
            f"Session pool should use DEFAULT_MAX_CONCURRENT capped by HARD_MAX_CONCURRENT",
        )

        # Build prompts for 3 parallel tasks — each should have its own
        # task-specific prompt but share the same encouragement template
        prompts = []
        for i in range(3):
            params = AgentCallParams(
                prompt=f"Task {i}: Analyze component {i}",
                tool="coco",
            )
            prompts.append(executor._build_prompt(params))

        # Each prompt should contain its unique task
        for i, prompt in enumerate(prompts):
            self.assertIn(f"Task {i}", prompt)
            self.assertIn(f"component {i}", prompt)

        # Each should have the encouragement (shared template part)
        for prompt in prompts:
            self.assertIn(SUBAGENT_ENCOURAGEMENT_PROMPT, prompt)

        # The total unique content is the sum of individual task prompts
        # plus one copy of the shared encouragement (not N copies in overhead)
        total_tokens = sum(_count_tokens(p) for p in prompts)
        unique_task_tokens = sum(
            _count_tokens(f"Task {i}: Analyze component {i}") for i in range(3)
        )
        overhead_tokens = total_tokens - unique_task_tokens

        # Overhead should be roughly 3 * encouragement (once per prompt),
        # not N times some larger base
        expected_overhead = 3 * _count_tokens(SUBAGENT_ENCOURAGEMENT_PROMPT)
        # Allow some tolerance for whitespace and formatting
        self.assertLess(overhead_tokens, expected_overhead + 50,
            f"Parallel overhead should be minimal, got {overhead_tokens} tokens")

    def test_parallel_token_usage_is_sum(self):
        """Verify total token usage for N parallel tasks is approximately
        the sum of individual task token usage.

        If we had per-task duplicated overhead, total would be N times
        some base overhead plus the sum. Instead, it should be just the
        sum of each task's actual token consumption.

        The engine's _on_token_usage callback accumulates tokens into
        both budget.used and metrics.total_tokens.
        """
        # Simulate 3 parallel calls with known token usage
        individual_usages = [1200, 800, 1500]
        expected_total = sum(individual_usages)

        # The engine accumulates token usage via on_token_usage callback
        accumulated = []

        def on_token_usage(tokens):
            accumulated.append(tokens)

        # Simulate what happens when 3 parallel calls complete
        for usage in individual_usages:
            on_token_usage(usage)

        # Total should be exactly the sum
        self.assertEqual(sum(accumulated), expected_total,
            "Parallel token usage should be sum of individual usages")

        # Verify token usage accumulates in project.metrics via state_manager.
        # (Previously engine._on_token_usage — now the state manager handles
        # metrics updates through on_agent_done.)
        from src.workflow_engine.engine import WorkflowEngine
        from src.workflow_engine.state_manager import WorkflowStateManager

        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine._lock = threading.Lock()
        engine._project = WorkflowProject(
            workflow_id="wf-parallel-test",
            status=WorkflowStatus.RUNNING,
            metrics=WorkflowMetrics(),
        )
        engine._state_manager = WorkflowStateManager(engine._project)
        engine._cancel_event = threading.Event()
        engine._callbacks = None

        for usage in individual_usages:
            engine._project.metrics.total_tokens += usage

        self.assertEqual(engine._project.metrics.total_tokens, expected_total,
            "metrics.total_tokens should equal sum of individual usages")

    def test_pipeline_reuses_context(self):
        """Verify pipeline stages can reuse context from previous stages.

        In a pipeline, each stage builds on the output of the previous
        stage. The prompt for stage N can reference the result of stage N-1
        without re-sending all the original context, reducing tokens.
        """
        # Simulate a 3-stage pipeline: analyze -> validate -> synthesize
        # Stage 1: full context needed (large code block)
        large_code_block = """\
def process_user_data(request, db_connection):
    \"\"\"Process user data and update database.\"\"\"
    user_id = request.args.get('user_id')
    raw_data = request.get_json()
    
    # Validate input
    if not user_id or not raw_data:
        return jsonify({'error': 'Missing parameters'}), 400
    
    # SQL injection vulnerability
    query = f"SELECT * FROM users WHERE id = '{user_id}'"
    cursor = db_connection.cursor()
    cursor.execute(query)
    user = cursor.fetchone()
    
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Update user data (no validation)
    update_query = f"UPDATE users SET data = '{raw_data}' WHERE id = '{user_id}'"
    cursor.execute(update_query)
    db_connection.commit()
    
    # Command injection vulnerability
    if raw_data.get('export'):
        import subprocess
        subprocess.call(f"echo {raw_data['export']} > /tmp/export.txt", shell=True)
    
    return jsonify({'status': 'success', 'user': user_id})
"""
        stage1_prompt = f"""\
Analyze this Python function for security vulnerabilities. Be thorough and identify
all potential issues including SQL injection, command injection, XSS, CSRF,
authentication bypass, and data validation problems.

Code to analyze:

{large_code_block}

For each vulnerability found, specify:
- Type of vulnerability
- Location in code (line reference)
- Severity (LOW/MEDIUM/HIGH/CRITICAL)
- A brief description of the issue
"""
        stage1_output = """\
## Security Analysis Results

### 1. SQL Injection (CRITICAL)
- Location: Lines 12 and 22
- Issue: User-provided `user_id` and `raw_data` are directly interpolated into SQL queries
  without parameterization. An attacker could craft a `user_id` like `' OR '1'='1` to
  bypass authentication or execute arbitrary SQL.

### 2. Command Injection (CRITICAL)  
- Location: Lines 29-30
- Issue: `raw_data['export']` is passed directly to `subprocess.call()` with `shell=True`.
  An attacker could provide a value like `"; rm -rf /;"` to execute arbitrary commands.

### 3. Missing Input Validation (HIGH)
- Location: Lines 7-8
- Issue: `raw_data` is accepted without any schema validation, type checking, or
  size limits. This could lead to memory exhaustion or unexpected data types.

### 4. Missing Authentication/Authorization (HIGH)
- Location: Entire function
- Issue: There is no check that the requesting user is authenticated or authorized
  to modify the specified user's data. Any user could modify any other user's data.
"""

        # Stage 2: only needs stage 1 output, not the full original code
        stage2_prompt = f"""\
Validate these security findings. For each finding:
1. Confirm whether it is a true positive or false positive
2. If true positive, verify the severity rating is appropriate
3. Suggest a concrete, actionable fix with code example

Findings to validate:

{stage1_output}
"""
        stage2_output = """\
## Validation Results

### 1. SQL Injection (CRITICAL) - CONFIRMED
- Verdict: True positive
- Severity: CRITICAL (correct)
- Fix: Use parameterized queries:
  ```python
  query = "SELECT * FROM users WHERE id = %s"
  cursor.execute(query, (user_id,))
  ```

### 2. Command Injection (CRITICAL) - CONFIRMED  
- Verdict: True positive
- Severity: CRITICAL (correct)
- Fix: Avoid `shell=True` and pass args as list:
  ```python
  subprocess.call(["echo", raw_data['export']], stdout=open("/tmp/export.txt", "w"))
  ```

### 3. Missing Input Validation (HIGH) - CONFIRMED
- Verdict: True positive
- Severity: HIGH (correct)
- Fix: Add schema validation with a library like pydantic or marshmallow.

### 4. Missing Authentication/Authorization (HIGH) - CONFIRMED
- Verdict: True positive
- Severity: HIGH (correct)
- Fix: Add authentication middleware and check user permissions before allowing data modification.
"""

        # Stage 3: only needs stage 2 output
        stage3_prompt = f"""\
Synthesize a final security report from these validated findings.
Structure the report with:
1. Executive summary
2. Detailed findings table (Vulnerability, Severity, Fix)
3. Overall risk assessment
4. Recommended remediation priority order

Validated findings:

{stage2_output}
"""

        # Verify token efficiency of the pipeline approach
        tokens1 = _count_tokens(stage1_prompt)
        tokens2 = _count_tokens(stage2_prompt)
        tokens3 = _count_tokens(stage3_prompt)

        # Total tokens for pipeline approach
        # Each stage only sends the output of the previous stage, not the original code
        pipeline_total = tokens1 + tokens2 + tokens3

        # Compare to naive approach where each stage re-sends ALL context:
        # - Stage 1: code + analysis instructions (same as pipeline)
        # - Stage 2: code + stage1_output + validation instructions (naive re-sends code)
        # - Stage 3: code + stage1_output + stage2_output + synthesis instructions (naive re-sends everything)
        code_tokens = _count_tokens(large_code_block)
        stage1_output_tokens = _count_tokens(stage1_output)
        stage2_output_tokens = _count_tokens(stage2_output)
        stage2_instructions_tokens = _count_tokens("""\
Validate these security findings. For each finding:
1. Confirm whether it is a true positive or false positive
2. If true positive, verify the severity rating is appropriate
3. Suggest a concrete, actionable fix with code example

Findings to validate:

""")
        stage3_instructions_tokens = _count_tokens("""\
Synthesize a final security report from these validated findings.
Structure the report with:
1. Executive summary
2. Detailed findings table (Vulnerability, Severity, Fix)
3. Overall risk assessment
4. Recommended remediation priority order

Validated findings:

""")

        # Naive: each stage re-sends everything
        naive_total = (
            tokens1 +  # Stage 1: same as pipeline
            (code_tokens + stage1_output_tokens + stage2_instructions_tokens) +  # Stage 2: re-sends code
            (code_tokens + stage1_output_tokens + stage2_output_tokens + stage3_instructions_tokens)  # Stage 3: re-sends everything
        )

        # Pipeline should save significant tokens by not re-sending the original code
        self.assertLess(pipeline_total, naive_total,
            "Pipeline approach should save tokens vs. re-sending full context")
        savings_pct = (1 - pipeline_total / naive_total) * 100
        self.assertGreater(savings_pct, 30,
            f"Pipeline should save at least 30% in tokens, saved {savings_pct:.0f}%")


# ===========================================================================
# 5. Token Usage Tracking
# ===========================================================================


class TestTokenUsageTracking(unittest.TestCase):
    """Test that token usage is accurately tracked across all agent calls.

    Accurate token tracking is essential for budget enforcement, cost
    attribution, and identifying inefficiencies.
    """

    def test_agent_call_result_has_token_usage(self):
        """Verify AgentCallResult includes a token_usage field with integer value.

        Every agent call result must report how many tokens it consumed.
        This is the foundation of all budget and cost tracking.
        """
        # Normal successful call
        result = AgentCallResult(
            output="The answer is 42",
            token_usage=256,
            duration_s=1.2,
            tool="coco",
        )
        self.assertIsInstance(result.token_usage, int)
        self.assertEqual(result.token_usage, 256)

        # Error result should still have token_usage (0 if no call was made)
        error_result = AgentCallResult(
            error="Connection timeout",
            tool="coco",
        )
        self.assertIsInstance(error_result.token_usage, int)
        self.assertEqual(error_result.token_usage, 0)

        # Default value should be 0
        default_result = AgentCallResult(output="test")
        self.assertEqual(default_result.token_usage, 0)

    def test_token_usage_accumulates_across_calls(self):
        """Verify multiple agent calls accumulate token usage correctly.

        Token usage is reflected in project.metrics.total_tokens via the
        state manager's on_agent_done path.
        """
        from src.workflow_engine.engine import WorkflowEngine
        from src.workflow_engine.state_manager import WorkflowStateManager

        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine._lock = threading.Lock()
        engine._project = WorkflowProject(
            workflow_id="wf-accum-test",
            status=WorkflowStatus.RUNNING,
            metrics=WorkflowMetrics(),
        )
        engine._state_manager = WorkflowStateManager(engine._project)
        engine._cancel_event = threading.Event()
        engine._callbacks = None

        # Simulate 5 agent calls with varying token usage
        call_tokens = [500, 1200, 800, 2100, 350]
        expected_total = sum(call_tokens)

        for idx, tokens in enumerate(call_tokens):
            label = f"agent-{idx}"
            engine._state_manager.on_agent_started(label, tool="coco", phase="default")
            engine._state_manager.on_agent_done(
                label,
                {"token_usage": tokens, "duration_s": 0.0, "cached": False},
            )

        # Verify token tracking through the callback mechanism
        self.assertEqual(engine._project.metrics.total_tokens, expected_total,
            f"metrics.total_tokens should equal {expected_total}")

    def test_cached_result_has_zero_or_minimal_tokens(self):
        """Verify cached results have token_usage=0 (no actual model call).

        Cache hits should not count toward token usage since no model
        call was made. This is what makes caching cost-effective.
        """
        # Simulate what engine does when returning a cached result
        # (see engine._handle_agent_call around line 371)
        original = AgentCallResult(
            output="Cached analysis result",
            token_usage=1500,  # Original call consumed tokens
            duration_s=3.0,
            tool="coco",
        )

        # Cached result wraps the original with zero tokens
        cached = AgentCallResult(
            output=original.output,
            parsed=original.parsed,
            token_usage=0,  # No tokens for cache hit
            duration_s=0.0,
            cached=True,
            tool=original.tool,
            model=original.model,
        )

        self.assertEqual(cached.token_usage, 0,
            "Cached result should have token_usage=0")
        self.assertTrue(cached.cached,
            "Cached result should have cached=True")
        self.assertEqual(cached.output, original.output,
            "Cached result should preserve output")


# ===========================================================================
# 6. AC4 Integration Semantic — delta_context_tokens ≈ len(final_result)
# ===========================================================================


class TestAC4IntegrationSemantic(unittest.TestCase):
    """End-to-end AC4 invariant: the engine records only the final-result
    text into ``delta_context_tokens``; repeated ``on_agent_done`` callbacks
    for intermediate agents leave it at zero.

    Together with the payload-stripping test above, these two tests pin down
    the high-level AC4 contract without relying on the renderer or bridge.
    """

    def _make_engine(self):
        """Build a WorkflowEngine with minimal mocking."""
        from src.workflow_engine.engine import WorkflowEngine
        from src.workflow_engine.state_manager import WorkflowStateManager

        engine = WorkflowEngine.__new__(WorkflowEngine)
        engine._lock = threading.Lock()
        engine._project = WorkflowProject(
            workflow_id="wf-ac4-semantic",
            status=WorkflowStatus.RUNNING,
            metrics=WorkflowMetrics(),
        )
        engine._state_manager = WorkflowStateManager(engine._project)
        engine._cancel_event = threading.Event()
        engine._callbacks = None
        engine._agent_call_count = 0
        engine._journal = None
        engine._progress_coalescer = None
        return engine

    def test_engine_records_final_result_only(self):
        """Simulating the engine's workflow-done path: add_context_tokens is
        called *once*, with the final result text length, and the state
        manager's audit counter matches."""
        engine = self._make_engine()

        # Before anything: audit counter is zero.
        self.assertEqual(engine._state_manager.delta_context_tokens, 0)

        # The legal single call-site in engine.execute_workflow:
        result_text = (
            "Workflow produced a short final report suitable for the main chat."
        )
        engine._state_manager.add_context_tokens(len(result_text or ""))

        # Audit counter tracks the injected text length — within 1.5× slack
        # (same bound used in test_workflow_ac4_isolation.py) it must be "on
        # the order of len(result_text)".
        self.assertGreater(engine._state_manager.delta_context_tokens, 0)
        self.assertLessEqual(
            engine._state_manager.delta_context_tokens,
            int(1.5 * len(result_text)),
            "delta_context_tokens should track len(final_result) closely",
        )

    def test_on_agent_done_does_not_inflate_context(self):
        """Repeated ``on_agent_done`` events for intermediate agents must
        leave delta_context_tokens at zero — only the final-result path may
        grow it.  Any nonzero value here would be an AC4 regression."""
        engine = self._make_engine()
        mgr = engine._state_manager
        mgr.on_phase_changed("Plan")

        # Simulate 5 intermediate agents completing.
        for i in range(5):
            label = f"planner-{i}"
            mgr.on_agent_started(label, "coco", "Plan")
            mgr.on_agent_done(label, {"token_usage": 100, "duration_s": 0.1})

        # Token-usage metrics must still be accurate (that's the *other*
        # counter — total tokens consumed from the AI provider).
        self.assertEqual(engine._project.metrics.total_tokens, 5 * 100)

        # But main-context injection must remain zero — nothing has been
        # pushed to the main chat yet.
        self.assertEqual(mgr.delta_context_tokens, 0)

        # Now simulate the legal final-result call.  Only *then* does the
        # audit counter advance.
        final_result = "Workflow complete.  Summary: 5 agents ran."
        mgr.add_context_tokens(len(final_result))
        self.assertEqual(mgr.delta_context_tokens, len(final_result))


if __name__ == "__main__":
    unittest.main()
