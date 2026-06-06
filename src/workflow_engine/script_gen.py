"""Script generation for workflow orchestration scripts."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subagent encouragement — appended to every agent() prompt template
# ---------------------------------------------------------------------------

SUBAGENT_ENCOURAGEMENT: str = (
    "**Subagent Usage Encouragement**: When a task can be decomposed, always "
    "delegate to subagents rather than doing everything yourself. Each subagent "
    "can further spawn its own subagents or sub-workflows. Subagents work in "
    "parallel and can independently handle research, implementation, verification, and "
    "testing tasks, significantly improving efficiency and convergence speed. Don't "
    "hesitate to spawn multiple subagents for different parts of the task — "
    "they are designed to work concurrently and will report back their results."
)


def _subagent_hint_enabled() -> bool:
    """Return True if the subagent / workflow encouragement paragraph should be appended.

    Reads the ``workflow_subagent_hint_enabled`` setting at call time so that
    runtime configuration changes are honoured.  Falls back to ``True`` if the
    settings module is unavailable (e.g. during unit tests that do not import
    the full application stack).
    """
    try:
        from src.config import get_settings

        return bool(getattr(get_settings(), "workflow_subagent_hint_enabled", True))
    except Exception:
        return True


def get_subagent_encouragement() -> str:
    """Return the subagent encouragement paragraph, or "" if disabled via settings."""
    return SUBAGENT_ENCOURAGEMENT if _subagent_hint_enabled() else ""

# ---------------------------------------------------------------------------
# Dangerous patterns that must not appear in generated scripts
# ---------------------------------------------------------------------------

_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"""require\s*\(\s*['"]fs['"]\s*\)""", "filesystem access via require('fs')"),
    (r"""require\s*\(\s*['"]child_process['"]\s*\)""", "shell access via require('child_process')"),
    (r"""require\s*\(\s*['"]net['"]\s*\)""", "network access via require('net')"),
    (r"""require\s*\(\s*['"]dgram['"]\s*\)""", "UDP access via require('dgram')"),
    (r"""require\s*\(\s*['"]http['"]\s*\)""", "HTTP access via require('http')"),
    (r"""require\s*\(\s*['"]https['"]\s*\)""", "HTTPS access via require('https')"),
    (r"""process\.exit""", "process.exit() call"),
    (r"""process\.env""", "process.env access"),
    (r"""import\s+.*from\s+['"]fs['"]""", "filesystem access via import 'fs'"),
    (r"""import\s+.*from\s+['"]child_process['"]""", "shell access via import 'child_process'"),
    (r"""import\s+.*from\s+['"]node:fs['"]""", "filesystem access via import 'node:fs'"),
    (r"""import\s+.*from\s+['"]node:child_process['"]""", "shell access via import 'node:child_process'"),
    (r"""import\s+.*from\s+['"]node:net['"]""", "network access via import 'node:net'"),
    (r"""import\s+.*from\s+['"]node:dgram['"]""", "UDP access via import 'node:dgram'"),
    (r"""import\s+.*from\s+['"]node:http['"]""", "HTTP access via import 'node:http'"),
    (r"""import\s+.*from\s+['"]node:https['"]""", "HTTPS access via import 'node:https'"),
    (r"""eval\s*\(""", "eval() usage"),
    (r"""Function\s*\(""", "dynamic Function constructor"),
    (r"""new\s+Worker\s*\(""", "Worker thread creation"),
    (r"""globalThis\[""", "globalThis bracket access"),
    (r"""Deno\.""", "Deno runtime API"),
    (r"""Bun\.""", "Bun runtime API"),
]

# ---------------------------------------------------------------------------
# Prompt template for script generation
# ---------------------------------------------------------------------------

# Injected section sentinels. These markers are removed after the budget /
# agent capability sections are spliced in. If a marker is missing for any
# reason (template edits, tests that strip them), insertion falls back to
# appending the sections at the end of the prompt instead of raising.
_USER_REQUIREMENT_INSERT_POINT = (
    "### SENTINEL: USER_REQUIREMENT_INSERT_POINT ###"
)

_SCRIPT_GEN_PROMPT_TEMPLATE = """\
# Workflow Script Generation Task

""" + _USER_REQUIREMENT_INSERT_POINT + """
## User Requirement

{requirement}

## Available Resources

**Tools (AI agents you can dispatch):**
{tools_list}

**Roles (specialized perspectives for agents):**
{roles_list}

**Budget:** {budget_total} tokens total across all agent() calls combined.

## Output Format

Generate a complete ES Module (.js) workflow script. The script MUST:

1. Export a `meta` constant describing the workflow
2. Export a `default` async function that orchestrates the workflow

### Meta Schema (REQUIRED)

```javascript
export const meta = {{
  name: "workflow-name-kebab-case",       // string, required
  description: "One-line description",     // string, required
  phases: [                                // array, at least 1 phase
    {{ title: "Phase Title", detail: "What this phase accomplishes" }},
  ],
  maxConcurrent: 6,                        // number, optional (default 10)
  tools: ["coco", "claude"],               // array of tools used
  workflow_refs: ["sub-workflow-name"],     // array, optional: sub-workflows invoked
}};
```

### Available Primitives

All primitives are globally available (no import needed):

```javascript
// Send a prompt to an AI agent and get the response
const result = await agent(prompt, {{
  tool: "coco",           // which AI tool to use
  role: "architect",      // optional role/persona
  label: "task-label",    // optional label for tracking
  phase: "Phase Title",   // optional phase association
  schema: {{ key: "string" }},  // optional output schema validation
  timeout: 300,           // optional timeout in seconds (default 300)
}});

// Run multiple independent tasks in parallel
const [r1, r2, r3] = await parallel([
  () => agent("task 1", {{ tool: "coco" }}),
  () => agent("task 2", {{ tool: "claude" }}),
  () => agent("task 3", {{ tool: "aiden" }}),
]);

// Process items through sequential stages (each item flows through all stages)
const results = await pipeline(
  items,                      // array of input items
  async (item) => {{ ... }},   // stage 1: transform
  async (item) => {{ ... }},   // stage 2: validate
  async (item) => {{ ... }},   // stage 3: finalize
);

// Declare phase transitions (for progress tracking)
phase("Phase Title");

// Logging
log("Status message");

// Invoke a sub-workflow by name
const subResult = await workflow("sub-workflow-name", {{ arg1: "value" }});
```

## Best Practices (MUST FOLLOW)

1. **Use parallel() for independent tasks** - When tasks don't depend on each other,
   wrap them in parallel() for concurrent execution. This maximizes throughput.

2. **Use pipeline() when items flow through stages** - When you have a list of items
   that each need the same multi-step processing, pipeline() is cleaner than nested loops.

3. **Assign different tools to different roles/tasks** - Diversity of AI perspectives
   produces more robust results. Don't use the same tool for everything.

4. **Include adversarial verification for critical outputs** - For important conclusions,
   have a separate agent with a different tool challenge the findings. Example:
   ```javascript
   const findings = await agent("Analyze X", {{ tool: "claude", role: "security_auditor" }});
   const verified = await agent(`Challenge these findings: ${{findings}}`, {{
     tool: "coco", role: "adversarial_verifier"
   }});
   ```

5. **Keep each agent() prompt focused on one task** - Don't ask an agent to do
   everything at once. Break complex work into focused, composable agent() calls.

6. **Encourage subagent usage in prompts** - When writing agent prompts, tell the agent
   to spawn subagents for independent sub-problems. Include this guidance in prompts
   for complex tasks:
   "When a task can be decomposed, always delegate to subagents rather than doing
   everything yourself. Each subagent can further spawn its own subagents or
   sub-workflows."
   This is critical for maximizing parallelism — agents that support subagents (e.g. coco)
   can internally fan out, reducing wall-clock time significantly.

7. **Declare phases** - Call phase("Title") before each logical section to enable
   progress tracking in the UI.

8. **Use labels** - Give agent() calls descriptive labels for observability.

9. **Handle results gracefully** - Agent calls may return errors; check results before
   using them in subsequent steps.

10. **Stay within budget** - The total token budget is {budget_total}. Avoid unnecessary
    agent calls. Combine related questions into single prompts where appropriate.

## Constraints

- Do NOT use `require()` or `import` statements (primitives are global)
- Do NOT access the filesystem, network, or child processes
- Do NOT call `process.exit()`
- Do NOT use `eval()` or `new Function()`
- Keep the script self-contained — all logic in one file

## Example Workflow

```javascript
export const meta = {{
  name: "code-review-workflow",
  description: "Multi-perspective code review with adversarial verification",
  phases: [
    {{ title: "Analysis", detail: "Independent code analysis from multiple perspectives" }},
    {{ title: "Verification", detail: "Adversarial challenge of findings" }},
    {{ title: "Synthesis", detail: "Consolidate verified findings into report" }},
  ],
  maxConcurrent: 4,
  tools: ["coco", "claude", "aiden"],
}};

export default async function() {{
  const codeContext = "..."; // would come from workflow args in real usage

  // Phase 1: Parallel independent analysis
  phase("Analysis");
  log("Starting multi-perspective analysis...");

  const [security, quality, perf] = await parallel([
    () => agent(`Review this code for security issues: ${{codeContext}}`, {{
      tool: "claude", role: "security_auditor", label: "security-review",
      phase: "Analysis",
    }}),
    () => agent(`Review this code for quality and maintainability: ${{codeContext}}`, {{
      tool: "coco", role: "code_quality_reviewer", label: "quality-review",
      phase: "Analysis",
    }}),
    () => agent(`Review this code for performance issues: ${{codeContext}}`, {{
      tool: "aiden", role: "correctness_auditor", label: "perf-review",
      phase: "Analysis",
    }}),
  ]);

  // Phase 2: Adversarial verification
  phase("Verification");
  log("Challenging findings...");

  const allFindings = `Security: ${{security}}\\nQuality: ${{quality}}\\nPerf: ${{perf}}`;
  const verified = await agent(
    `You are an adversarial verifier. Challenge these findings and identify any ` +
    `false positives or overblown severity: ${{allFindings}}`,
    {{ tool: "claude", role: "adversarial_verifier", label: "adversarial-check", phase: "Verification" }}
  );

  // Phase 3: Final synthesis
  phase("Synthesis");
  log("Synthesizing final report...");

  const report = await agent(
    `Synthesize a final review report from these verified findings: ${{verified}}`,
    {{ tool: "coco", label: "synthesis", phase: "Synthesis" }}
  );

  return report;
}}
```

## Now Generate

Based on the user requirement above, generate a COMPLETE workflow script following all
the patterns, constraints, and best practices described. Output ONLY the JavaScript code,
no markdown fences, no explanatory text before or after.
"""


# ---------------------------------------------------------------------------
# Orchestrator agent capability notes
# ---------------------------------------------------------------------------


def _get_agent_capability_note(agent_type: str) -> str:
    """Get capability notes for the orchestrator agent to adapt prompt style.

    Returns guidance on how to tailor the workflow script generation prompt
    based on the strengths and characteristics of the orchestrator agent
    that will execute the generated script.
    """
    capability_notes = {
        "coco": "Coco 擅长全栈编程和 subagent 调度，支持复杂的并行编排。prompt 中可以使用高级编排模式，如深度 fan-out、多轮验证闭环、复杂 pipeline 组合。",
        "claude": "Claude 擅长深度推理和复杂任务分解，适合需要精细规划的场景。prompt 中应强调逻辑严谨性、逐步推理、边界条件分析。",
        "aiden": "Aiden 擅长代码审查和架构设计，适合代码质量相关任务。prompt 中应强调审查深度、架构合理性、技术债务识别。",
        "codex": "Codex 擅长快速代码生成，适合 straightforward 的实现任务。prompt 中应简洁直接，减少不必要的抽象层。",
        "gemini": "Gemini 擅长多模态推理，适合涉及图像或复杂数据的任务。prompt 中可以包含多模态相关指令，如图像分析、图表理解。",
        "traex": "Traex 擅长高并发轻量任务，适合大量简单并行任务。prompt 中应强调任务粒度控制、最小化单任务复杂度、最大化并行度。",
    }
    return capability_notes.get(agent_type, capability_notes["coco"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_script_gen_prompt(
    requirement: str,
    available_tools: list[str] | dict[str, str],
    available_roles: list[str],
    budget_total: int,
    budget_tokens: Optional[int] = None,
    orchestrator_agent: str = "coco",
) -> str:
    """Build the prompt that instructs an AI to generate a workflow script.

    Args:
        requirement: The user's task description / workflow requirement.
        available_tools: List of tool names, or dict of name->description.
        available_roles: List of available roles (e.g. ["architect", "tester"]).
        budget_total: Total token budget for the workflow execution.
        budget_tokens: Optional hard token budget constraint. If provided, adds
            a detailed budget constraint section with tiered guidance based on
            budget size. If None, no hard constraint section is added.
        orchestrator_agent: The type of agent that will execute the generated
            workflow script. Used to adapt prompt style and recommendations
            to the agent's capabilities. Defaults to "coco".

    Returns:
        A complete prompt string ready to send to a code-generation agent.
    """
    if isinstance(available_tools, dict):
        tools_list = "\n".join(
            f"- `{name}` — {desc}" for name, desc in available_tools.items()
        ) if available_tools else "- (none)"
    else:
        tools_list = "\n".join(f"- `{t}`" for t in available_tools) if available_tools else "- (none)"
    roles_list = "\n".join(f"- `{r}`" for r in available_roles) if available_roles else "- (none)"

    # Budget constraint section (hard constraint)
    budget_section = ""
    if budget_tokens is not None:
        budget_section = f"""## 预算硬约束

**Token 预算硬约束**：本 Workflow 的总 Token 预算为 {budget_tokens:,} tokens。

- 脚本生成时必须考虑预算限制，合理安排 agent() 调用数量和复杂度
- **预算紧张时** (< 1,000,000 tokens)：减少并行度，合并相似任务，避免过度 fan-out
- **预算适中时** (1,000,000 - 2,000,000 tokens)：平衡并行度和任务质量，使用适度的并行策略
- **预算充足时** (> 2,000,000 tokens)：可以使用更激进的并行策略和多轮验证
- 每个 agent() 调用预计消耗 50K-200K tokens，请据此规划任务数量
- 严禁超出预算，runtime 会在超预算时终止执行
- 建议预留 10-20% 的预算作为缓冲，应对意外情况
"""

    # Orchestrator agent capability adaptation section
    agent_capability_section = f"""## 主编排 Agent 能力

当前使用的主编排 Agent 是：**{orchestrator_agent}**

{_get_agent_capability_note(orchestrator_agent)}

请根据上述 Agent 的能力特点，生成最适合它执行的 workflow 脚本。
"""

    # Build the base prompt from template
    base_prompt = _SCRIPT_GEN_PROMPT_TEMPLATE.format(
        requirement=requirement.strip(),
        tools_list=tools_list,
        roles_list=roles_list,
        budget_total=budget_total,
    )

    # Inject budget and agent capability sections using an explicit sentinel.
    # Using str.find() instead of str.index() so we never raise ValueError.
    # If the sentinel is missing (template edits, test mutations, etc.), we
    # fall back to appending the sections at the end and log a debug note.
    insert_idx = base_prompt.find(_USER_REQUIREMENT_INSERT_POINT)
    injection = budget_section + agent_capability_section

    if insert_idx >= 0:
        # Splice the sections in and remove the sentinel line (plus its trailing
        # newline) so it never reaches the AI.
        sentinel_len = len(_USER_REQUIREMENT_INSERT_POINT)
        end_of_sentinel = insert_idx + sentinel_len
        # Absorb one trailing newline if present so we don't leave a blank line
        # in the rendered prompt.
        if end_of_sentinel < len(base_prompt) and base_prompt[end_of_sentinel] == "\n":
            end_of_sentinel += 1
        prompt = (
            base_prompt[:insert_idx]
            + injection
            + base_prompt[end_of_sentinel:]
        )
    else:
        logger.debug(
            "script_gen: USER_REQUIREMENT_INSERT_POINT not found in template; "
            "appending budget/agent sections at the end of the prompt."
        )
        prompt = base_prompt + "\n\n" + injection

    return prompt + ("\n\n" + get_subagent_encouragement() if get_subagent_encouragement() else "")


def validate_generated_script(script_content: str) -> tuple[bool, list[str]]:
    """Validate a generated workflow script without executing it.

    Performs basic structural and safety checks:
    - Presence of `export const meta =`
    - Meta has `name` and `description` fields
    - Balanced braces and brackets (rough syntax check)
    - At least one `agent(` call exists
    - Presence of `export default` entry function
    - Dangerous patterns are reported as BLOCKING errors (fail-closed — not warnings)

    The runtime sandbox provides defense-in-depth, but script-level rejection
    is the primary security boundary for user-generated workflows. Templates
    approved by an admin can be whitelisted via `WORKFLOW_ALLOWLIST_CAPABILITIES`
    to preserve their intended behavior.

    Args:
        script_content: The raw JavaScript source code of the workflow script.

    Returns:
        A tuple of (is_valid, list_of_messages). Dangerous patterns are treated
        as errors (prefix "[capability]"). `is_valid` is False if any structural
        error OR any dangerous pattern is detected.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not script_content or not script_content.strip():
        return False, ["Script content is empty"]

    # --- Check meta export presence ---
    if not re.search(r"export\s+const\s+meta\s*=", script_content):
        errors.append("Missing `export const meta =` declaration")

    # --- Check meta has name field ---
    if not re.search(r"""name\s*:\s*["'`]""", script_content):
        errors.append("Meta object missing `name` field (expected name: \"...\")")

    # --- Check meta has description field ---
    if not re.search(r"""description\s*:\s*["'`]""", script_content):
        errors.append("Meta object missing `description` field (expected description: \"...\")")

    # --- Check for export default function ---
    if not re.search(r"export\s+default\s+(async\s+)?function", script_content):
        errors.append("Missing `export default function` — script must export a default entry function")

    # --- Check for at least one agent() call ---
    if not re.search(r"\bagent\s*\(", script_content):
        errors.append("No `agent()` call found - workflow must invoke at least one agent")

    # --- Balanced braces check (rough) ---
    brace_balance = 0
    bracket_balance = 0
    paren_balance = 0
    in_string = False
    string_char = ""
    escaped = False

    for ch in script_content:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_string:
            if ch == string_char:
                in_string = False
            continue
        if ch in ("'", '"', "`"):
            in_string = True
            string_char = ch
            continue

        if ch == "{":
            brace_balance += 1
        elif ch == "}":
            brace_balance -= 1
        elif ch == "[":
            bracket_balance += 1
        elif ch == "]":
            bracket_balance -= 1
        elif ch == "(":
            paren_balance += 1
        elif ch == ")":
            paren_balance -= 1

    if brace_balance != 0:
        errors.append(
            f"Unbalanced braces: {{}} balance is {brace_balance:+d} "
            f"({'excess opens' if brace_balance > 0 else 'excess closes'})"
        )
    if bracket_balance != 0:
        errors.append(
            f"Unbalanced brackets: [] balance is {bracket_balance:+d} "
            f"({'excess opens' if bracket_balance > 0 else 'excess closes'})"
        )
    if paren_balance != 0:
        errors.append(
            f"Unbalanced parentheses: () balance is {paren_balance:+d} "
            f"({'excess opens' if paren_balance > 0 else 'excess closes'})"
        )

    # --- Check for dangerous patterns (FAIL-CLOSED — security boundary) ---
    # Patterns in _DANGEROUS_PATTERNS are non-negotiable: filesystem, network,
    # child_process, eval, Function, Worker, globalThis, Deno/Bun APIs, etc.
    # They are reported as blocking errors, not warnings, because the runtime
    # sandbox is defense-in-depth, not the primary enforcement mechanism.
    for pattern, description in _DANGEROUS_PATTERNS:
        if re.search(pattern, script_content):
            errors.append(f"[capability] Forbidden pattern: {description}")

    is_valid = len(errors) == 0
    all_messages = errors + warnings

    if errors:
        logger.warning("Script validation failed with %d error(s): %s", len(errors), "; ".join(errors))
    if warnings:
        logger.info("Script validation warnings: %s", "; ".join(warnings))
    if is_valid and not warnings:
        logger.debug("Script validation passed")

    return is_valid, all_messages


def extract_meta_from_script(script_content: str) -> Optional[dict[str, Any]]:
    """Try to extract the meta object from a workflow script source.

    Uses regex-based extraction followed by JSON parsing. Handles common JS object
    literal patterns by converting them to valid JSON (unquoted keys, trailing commas,
    single quotes).

    Args:
        script_content: The raw JavaScript source code of the workflow script.

    Returns:
        The meta object as a Python dict, or None if extraction/parsing fails.
    """
    if not script_content:
        return None

    # Strategy 1: Find the meta block between `export const meta = {` and the matching `}`
    meta_match = re.search(
        r"export\s+const\s+meta\s*=\s*(\{)",
        script_content,
    )
    if not meta_match:
        logger.debug("No `export const meta =` found in script")
        return None

    # Find the matching closing brace
    start_idx = meta_match.start(1)
    brace_depth = 0
    end_idx = start_idx
    in_string = False
    string_char = ""
    escaped = False

    for i in range(start_idx, len(script_content)):
        ch = script_content[i]

        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_string:
            if ch == string_char:
                in_string = False
            continue
        if ch in ("'", '"', "`"):
            in_string = True
            string_char = ch
            continue

        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if brace_depth == 0:
                end_idx = i
                break
    else:
        # Never found matching brace
        logger.debug("Could not find matching closing brace for meta object")
        return None

    raw_meta = script_content[start_idx : end_idx + 1]

    # Convert JS object literal to JSON-parseable form
    json_str = _js_object_to_json(raw_meta)

    try:
        meta = json.loads(json_str)
        if isinstance(meta, dict):
            logger.debug("Successfully extracted meta: %s", meta.get("name", "?"))
            _enrich_workflow_refs(meta, script_content)
            return meta
        return None
    except json.JSONDecodeError as e:
        logger.debug("JSON parse failed for extracted meta: %s", e)
        # Strategy 2: Try a more aggressive cleanup
        json_str_v2 = _aggressive_json_cleanup(raw_meta)
        try:
            meta = json.loads(json_str_v2)
            if isinstance(meta, dict):
                logger.debug("Extracted meta via aggressive cleanup: %s", meta.get("name", "?"))
                _enrich_workflow_refs(meta, script_content)
                return meta
        except json.JSONDecodeError:
            pass

        logger.warning("Failed to parse meta object from script")
        return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _enrich_workflow_refs(meta: dict[str, Any], script_content: str) -> None:
    """If meta lacks workflow_refs, scan the script for workflow() calls and populate.

    Produces normalized refs in {name, path?, hash?} format. Existing string refs
    are converted to dicts for consistency.
    """
    existing = meta.get("workflow_refs", [])
    if existing:
        # Normalize existing refs: convert strings to {name} dicts
        normalized: list[dict[str, str]] = []
        for ref in existing:
            if isinstance(ref, str):
                normalized.append({"name": ref})
            elif isinstance(ref, dict):
                # Normalize legacy script_path to path
                item = dict(ref)
                if "script_path" in item and "path" not in item:
                    item["path"] = item.pop("script_path")
                normalized.append(item)
        meta["workflow_refs"] = normalized
        return

    # Find workflow("name", ...) or workflow({name: "..."}) calls
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in re.finditer(r"""\bworkflow\s*\(\s*["'`]([^"'`]+)["'`]""", script_content):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            refs.append({"name": name})
    if refs:
        meta["workflow_refs"] = refs


def _js_object_to_json(js_obj: str) -> str:
    """Convert a JavaScript object literal to a JSON string.

    Handles:
    - Unquoted property keys: { name: "x" } -> { "name": "x" }
    - Single-quoted strings: 'value' -> "value"
    - Trailing commas: [1, 2, ] -> [1, 2]
    - JS comments: // ... and /* ... */
    """
    result = js_obj

    # Remove single-line comments (but not inside strings — rough heuristic)
    result = re.sub(r"//[^\n]*", "", result)
    # Remove multi-line comments
    result = re.sub(r"/\*[\s\S]*?\*/", "", result)

    # Replace single quotes with double quotes (simple cases)
    # This is imperfect for strings containing quotes but handles most generated scripts
    result = _replace_single_quotes(result)

    # Quote unquoted keys: word: -> "word":
    result = re.sub(
        r'(?<=[{,\n])\s*([a-zA-Z_$][a-zA-Z0-9_$]*)\s*:',
        r' "\1":',
        result,
    )

    # Remove trailing commas before } or ]
    result = re.sub(r",\s*([}\]])", r"\1", result)

    return result


def _replace_single_quotes(s: str) -> str:
    """Replace single-quoted strings with double-quoted strings.

    Handles escaped single quotes within single-quoted strings.
    """
    result = []
    i = 0
    length = len(s)

    while i < length:
        ch = s[i]

        # Already in a double-quoted string — pass through
        if ch == '"':
            result.append(ch)
            i += 1
            while i < length:
                if s[i] == "\\" and i + 1 < length:
                    result.append(s[i])
                    result.append(s[i + 1])
                    i += 2
                elif s[i] == '"':
                    result.append(s[i])
                    i += 1
                    break
                else:
                    result.append(s[i])
                    i += 1
            continue

        # Template literal — pass through as-is (cannot easily convert)
        if ch == "`":
            result.append('"')
            i += 1
            while i < length:
                if s[i] == "\\" and i + 1 < length:
                    result.append(s[i])
                    result.append(s[i + 1])
                    i += 2
                elif s[i] == "`":
                    result.append('"')
                    i += 1
                    break
                else:
                    # Escape any double quotes inside
                    if s[i] == '"':
                        result.append('\\"')
                    else:
                        result.append(s[i])
                    i += 1
            continue

        # Single-quoted string — convert to double-quoted
        if ch == "'":
            result.append('"')
            i += 1
            while i < length:
                if s[i] == "\\" and i + 1 < length:
                    if s[i + 1] == "'":
                        # Escaped single quote -> just the quote in double-quoted string
                        result.append("'")
                        i += 2
                    else:
                        result.append(s[i])
                        result.append(s[i + 1])
                        i += 2
                elif s[i] == '"':
                    # Unescaped double quote inside single-quoted string -> escape it
                    result.append('\\"')
                    i += 1
                elif s[i] == "'":
                    result.append('"')
                    i += 1
                    break
                else:
                    result.append(s[i])
                    i += 1
            continue

        result.append(ch)
        i += 1

    return "".join(result)


def _aggressive_json_cleanup(raw: str) -> str:
    """More aggressive attempt to extract a JSON object from JS source.

    Strips everything that isn't likely part of the JSON structure.
    """
    result = raw

    # Remove comments
    result = re.sub(r"//[^\n]*", "", result)
    result = re.sub(r"/\*[\s\S]*?\*/", "", result)

    # Replace single quotes
    result = _replace_single_quotes(result)

    # Quote unquoted keys
    result = re.sub(
        r'(?<=[{,\n])\s*([a-zA-Z_$][a-zA-Z0-9_$]*)\s*:',
        r' "\1":',
        result,
    )

    # Remove trailing commas
    result = re.sub(r",\s*([}\]])", r"\1", result)

    # Remove template literal expressions ${...} — replace with empty string
    result = re.sub(r"\$\{[^}]*\}", "", result)

    return result


# ---------------------------------------------------------------------------
# Simple script generation (no AI call — wraps requirement in a single agent)
# ---------------------------------------------------------------------------


def generate_simple_script(requirement: str) -> str:
    """Generate a minimal workflow script that wraps a requirement in a single agent call.

    Used as a fallback when no template matches. Creates a simple two-phase
    workflow: plan → execute.
    """
    # Resolve subagent encouragement at call time so the runtime setting can
    # suppress it without reimporting the module.
    _enc = get_subagent_encouragement()

    # Escape the requirement for embedding in JS template literal
    escaped = requirement.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")

    return f'''/**
 * Auto-generated workflow script.
 * Requirement: {requirement[:80]}
 */

export const meta = {{
  name: "generated-workflow",
  description: "Auto-generated from user requirement",
  phases: [
    {{ title: "Planning", detail: "Analyze requirement and create plan" }},
    {{ title: "Execution", detail: "Execute the plan" }}
  ],
  maxConcurrent: 4,
  tools: ["coco"]
}};

export default async function main() {{
  const requirement = `{escaped}`;

  // Phase 1: Planning
  phase("Planning");
  log("Analyzing requirement and creating execution plan");

  const plan = await agent({{
    prompt: `You are a senior engineer. Analyze this requirement and create a concrete implementation plan.

Requirement: ${{requirement}}

Output a JSON object with:
- "tasks": array of {{ "description": "", "priority": "high|medium|low" }}
- "approach": brief description of the overall approach
- "estimated_agents": how many agent calls you expect to need

{_enc}`,
    schema: {{ tasks: [], approach: "", estimated_agents: 0 }},
    label: "planner",
  }});

  const tasks = plan.tasks || [];
  log(`Plan created: ${{tasks.length}} tasks identified`);

  // Phase 2: Execution
  phase("Execution");

  if (tasks.length === 0) {{
    // Single-shot execution
    const result = await agent({{
      prompt: `Complete this task fully:\\n\\n${{requirement}}\\n\\n{_enc}`,
      label: "executor",
    }});
    return result;
  }}

  // Execute tasks (parallel where possible)
  const results = await parallel(
    tasks.map((task, i) => ({{
      prompt: `Complete this specific task as part of a larger requirement.

Overall requirement: ${{requirement}}
Overall approach: ${{plan.approach}}

Your specific task: ${{task.description}}

Complete this task fully and provide the result.

{_enc}`,
      label: `task-${{i + 1}}`,
    }}))
  );

  // Final synthesis
  const synthesis = await agent({{
    prompt: `Synthesize the results of all completed tasks into a final deliverable.

Original requirement: ${{requirement}}
Approach: ${{plan.approach}}

Task results:
${{results.map((r, i) => `Task ${{i+1}}: ${{typeof r === "string" ? r.slice(0, 500) : JSON.stringify(r).slice(0, 500)}}`).join("\\n\\n")}}

Provide a concise final summary and any integration notes.

{_enc}`,
    label: "synthesizer",
  }});

  return synthesis;
}}
'''
