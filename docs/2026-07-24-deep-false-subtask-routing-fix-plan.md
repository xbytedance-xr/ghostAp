# Deep False Subtask Routing Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent ordinary Deep tool output that merely contains the source text `子代理：` from being registered and rendered as a failed subtask.

**Architecture:** Keep agent/subtask recognition at the shared `TaskOrchestrator` boundary used by both Deep's sticky task list and child-card routing. Trust semantic ACP identity (`kind == "agent"` or an explicit `agent`/`subagent`/`task` title); retain the text-marker compatibility fallback only for the provider's generic `kind == "other"` shape.

**Tech Stack:** Python 3, ACP event models, CardSession/TaskOrchestrator, pytest via `uv`.

## Global Constraints

- Use only `uv` for Python commands.
- Preserve all pre-existing uncommitted image-card and completion-lifecycle work.
- Do not change card layout or copy; removing the false task removes the screenshot's bogus JSON/jq titles, failed child card, and generic error summary at their source.
- Every production behavior change must first be demonstrated by a failing regression test.

---

### Task 1: Reproduce and repair the screenshot's false child task

**Files:**
- Modify: `tests/test_card_orchestrator.py`
- Modify: `tests/test_deep_renderer_orchestrator.py`
- Modify: `src/card/orchestrator.py`

**Interfaces:**
- Consumes: `TaskOrchestrator.route_or_fallback(acp_event, fallback_bridge) -> bool`
- Produces: Regression coverage proving `ToolCallInfo(kind="execute", title="exec")` remains in the ordinary tool flow even when its failed output contains `子代理：`.

- [x] **Step 1: Add the focused orchestrator regression**

Add a failed `TOOL_CALL_DONE` event matching the real `call_zXAT0JlJc0dqRewi…` shape:

```python
event = ACPEvent(
    event_type=ACPEventType.TOOL_CALL_DONE,
    tool_call=ToolCallInfo(
        id="call_zXAT0JlJc0dqRewiUJK8nHYL",
        title="exec",
        kind="execute",
        status="failed",
        content='assert "子代理：" not in ordinary_output',
    ),
)
```

Assert that routing returns `False`, the fallback bridge receives the event, the registry remains empty, and no child session is created.

- [x] **Step 2: Add a Deep integration assertion**

Feed an ordinary execute start/done pair into `DeepStreamCallbacks` and assert the child-card count remains unchanged and no task-list entry uses the execute call ID.

- [x] **Step 3: Verify RED**

Run:

```bash
uv run python -m pytest \
  tests/test_card_orchestrator.py::TestAgentTaskRouting::test_execute_failure_output_with_subagent_source_marker_stays_ordinary_tool \
  tests/test_deep_renderer_orchestrator.py::TestDeepRendererSingleCard::test_execute_failure_output_with_subagent_source_marker_does_not_create_child_card \
  -q
```

Expected: both tests fail because `TaskOrchestrator._is_agent_task()` currently accepts the marker for every tool kind.

- [x] **Step 4: Implement the minimal classifier change**

Use:

```python
title = str(getattr(tool_call, "title", "") or "").strip().lower()
kind = str(getattr(tool_call, "kind", "") or "").strip().lower()
content = str(getattr(tool_call, "content", "") or "").strip()
if kind == "agent" or title in _AGENT_TOOL_TITLES:
    return True
return kind == "other" and "子代理：" in content
```

This keeps explicit provider agent tools working while excluding `execute`, `read`, `edit`, and other concrete tool outputs.

- [x] **Step 5: Verify GREEN**

Re-run the two focused tests from Task 1. Expected: `2 passed`.

- [x] **Step 6: Verify adjacent real-subagent contracts**

Run:

```bash
uv run python -m pytest \
  tests/test_card_orchestrator.py \
  tests/test_deep_renderer_orchestrator.py \
  tests/test_programming_completion_guards.py \
  -q
```

Expected: all tests pass, including explicit `agent`, `subagent`, `task`, and generic-`other` compatibility paths.

### Task 2: Quality gates and project memory

**Files:**
- Modify: `.Memory/2026-07-24.md`
- Modify: `.Memory/Abstract.md`

**Interfaces:**
- Consumes: focused red/green evidence and final test results.
- Produces: durable root-cause and verification record.

- [x] **Step 1: Run proportional verification**

```bash
uv run ruff check src/card/orchestrator.py tests/test_card_orchestrator.py tests/test_deep_renderer_orchestrator.py
uv run python -m src.main --validate
git diff --check
```

- [x] **Step 2: Run the broader card/Deep regression set**

```bash
uv run python -m pytest \
  tests/test_card_orchestrator.py \
  tests/test_deep_renderer_orchestrator.py \
  tests/test_card_renderer.py \
  tests/test_render_task_list.py \
  tests/test_task_list_v2.py \
  tests/test_deep_engine.py \
  -q
```

- [x] **Step 3: Record the fix**

Append the exact ACP evidence, classifier correction, red/green commands, broader verification, and remaining external E2E risk to `.Memory/2026-07-24.md`; add one dated summary line to `.Memory/Abstract.md`.

The user subsequently requested that the reviewed worktree be committed and
pushed after all quality gates pass.
