# Subtask Card Visual Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make programming and Deep subtask cards readable on mobile by hiding internal identifiers, collapsing global progress, and removing repeated metadata.

**Architecture:** Keep `CardSession -> render -> delivery` unchanged. Normalize agent-facing labels at the shared tool-display boundary, then specialize only the pure header/task-list/footer render projection when `CardMetadata.is_subagent` is true. Parent cards and non-subtask engine cards retain their existing layout.

**Tech Stack:** Python 3.13, frozen card state dataclasses, Feishu Card JSON 2.0, pytest, Ruff, static HTML preview.

## Global Constraints

- Use `uv` only.
- Preserve the dirty worktree and do not rewrite unrelated current changes.
- Keep the card dependency direction `handler -> session -> render` and `session -> delivery`.
- Keep one independent Feishu card per real subagent.
- Never expose opaque `call_*` IDs, escaped control text, stdout, or raw JSON in titles.
- Keep every page under the internal 180-node / 27-KB budget.
- Do not commit or push without an explicit user request.

---

### Task 1: Mobile Subtask Preview

**Files:**
- Modify: `ux/card_preview.html`

**Interfaces:**
- Consumes: Existing `.feishu-card`, header, panel, footer, and mobile CSS preview primitives.
- Produces: Reviewed running/completed/failed subtask examples that production rendering must match.

- [x] **Step 1: Replace the existing subagent example**

Use a concise orange running header:

```html
<div class="card-header header-orange">
  <div class="title">🧬 子任务 · 完成独立最终代码审查</div>
  <div class="subtitle">执行中 · 子卡 #1.e · 来自主卡 #1</div>
</div>
```

Show a neutral collapsed progress summary:

```html
<div class="panel panel-grey">
  <div class="panel-header collapsed">
    <span class="arrow">▼</span>
    <span class="panel-title"><strong>整体 4/5 · 当前 5/5 最终代码审查</strong></span>
  </div>
  <div class="panel-content hidden"></div>
</div>
```

Keep the footer to status plus tool/model/time; omit working directory and a second subagent badge.

- [x] **Step 2: Add completed and failed variants**

Use green and red headers respectively, with the same title hierarchy and no internal IDs.

- [x] **Step 3: Inspect the preview source**

Run:

```bash
rg -n "call_|\\\\n|sub ·|from #" ux/card_preview.html
```

Expected: no production subtask example contains an opaque call ID, escaped newline, English `sub`, or English `from`.

### Task 2: Red Tests for Display-Safe Metadata

**Files:**
- Create: `tests/test_tool_display.py`
- Modify: `tests/test_programming_card_session.py`
- Modify: `tests/test_card_orchestrator.py`

**Interfaces:**
- Consumes: `ToolCallInfo`.
- Produces: `extract_agent_tool_name(tool_call, fallback="子代理", max_chars=24) -> str`.

- [x] **Step 1: Add shared display-helper tests**

```python
def test_agent_tool_name_rejects_escaped_source_fragment():
    call = ToolCallInfo(
        id="call_internal",
        title="agent",
        kind="other",
        content='子代理：\\" not in ordinary_output\\",\\n',
    )
    assert extract_agent_tool_name(call) == "agent"


def test_task_label_rejects_opaque_call_identifier():
    call = ToolCallInfo(
        id="call_internal",
        title="task",
        kind="other",
        content="call_usOANvwWFgpuBkmHB",
    )
    assert extract_tool_call_label(call, generic_labels={"task"}) == "子任务"
```

- [x] **Step 2: Add adapter integration assertions**

Create a real child session from the escaped marker input and assert its `tool_name`, `unit_label`, rendered header, and footer contain neither `ordinary_output`, literal `\\n`, nor `call_internal`.

- [x] **Step 3: Run RED**

Run:

```bash
uv run python -m pytest tests/test_tool_display.py tests/test_programming_card_session.py -q
```

Expected: FAIL because `extract_agent_tool_name` does not exist and the current child header exposes `unit_id`.

### Task 3: Red Tests for Compact Child Rendering

**Files:**
- Modify: `tests/test_render_task_list.py`
- Modify: `tests/test_card_renderer.py`
- Modify: `tests/test_footer_v2.py`

**Interfaces:**
- Consumes: `CardState(metadata.is_subagent=True)` and `TaskListBlock`.
- Produces: compact subtask header, collapsed task progress, and deduplicated footer.

- [x] **Step 1: Lock the compact task-list contract**

```python
def test_compact_mode_is_collapsed_neutral_summary():
    result = render_task_list_panel(block, compact=True)
    assert result["expanded"] is False
    assert result["border"]["color"] == "grey"
    assert "整体 4/5" in result["header"]["title"]["content"]
    assert "当前 5/5" in result["header"]["title"]["content"]
    assert "✅" not in result["header"]["title"]["content"]
```

- [x] **Step 2: Lock the child header contract**

Render running, completed, and failed states and assert:

```python
assert header["title"]["content"] == "🧬 子任务 · 完成独立最终代码审查"
assert header["subtitle"]["content"] == "执行中 · 子卡 #1.e · 来自主卡 #1"
assert "call_" not in str(header)
assert [running_template, completed_template, failed_template] == ["orange", "green", "red"]
```

- [x] **Step 3: Lock footer deduplication**

Assert a subtask footer has no working-directory context line, no duplicate `sub/model/tool/from` badge, and only one tool/model/duration metadata line.

- [x] **Step 4: Run RED**

Run:

```bash
uv run python -m pytest tests/test_render_task_list.py tests/test_card_renderer.py tests/test_footer_v2.py -q
```

Expected: FAIL because compact mode is currently expanded, child headers use the generic project header, and the footer repeats subagent metadata.

### Task 4: Minimal Production Implementation

**Files:**
- Modify: `src/card/tool_display.py`
- Modify: `src/card/programming_adapter.py`
- Modify: `src/card/orchestrator.py`
- Modify: `src/card/render/header.py`
- Modify: `src/card/render/task_list.py`
- Modify: `src/card/render/sticky_head.py`
- Modify: `src/card/render/renderer.py`
- Modify: `src/card/render/footer.py`
- Modify: `tests/test_header_v2.py`

**Interfaces:**
- Consumes: the tests and preview from Tasks 1–3.
- Produces: display-safe subtask metadata and compact pure rendering.

- [x] **Step 1: Implement `extract_agent_tool_name`**

Normalize actual and escaped whitespace, reject opaque IDs and code-like escaped fragments, prefer a clean explicit agent marker, then fall back to the non-generic tool title or `子代理`.

- [x] **Step 2: Reuse the helper in both child-card producers**

Replace duplicated `_extract_agent_tool_name` parsing in `ProgrammingCardSession` and `TaskOrchestrator`.

- [x] **Step 3: Add a dedicated subtask header projection**

When `metadata.is_subagent` is true, render only the bounded `unit_label`, state, visible branch number, and parent card number. Do not include project, model, tool, or `unit_id`.

- [x] **Step 4: Implement true compact task progress**

For `compact=True`, render a collapsed grey panel. Keep current work in the header; inside, retain counts, active items, the two most recent completed items, the next two pending items, and explicit remaining counts.

- [x] **Step 5: Select compact mode from card state**

Pass `compact=True` only when `state.metadata.is_subagent` is true. Keep the
sticky-head task-list path state-aware as well; its pre-rendered atom must not
silently force compact mode onto parent cards.

- [x] **Step 6: Deduplicate the child footer**

Hide working-directory context, active-tool hint, and the extra subagent badge for child cards. Keep the ordinary status/progress line and one tool/model/duration line.

- [x] **Step 7: Run GREEN**

Run:

```bash
uv run python -m pytest tests/test_tool_display.py tests/test_programming_card_session.py tests/test_card_orchestrator.py tests/test_render_task_list.py tests/test_card_renderer.py tests/test_footer_v2.py tests/test_header_v2.py tests/test_sticky_head.py -q
```

Expected: all tests pass.

### Task 5: Validation and Memory

**Files:**
- Modify: `.Memory/2026-07-24.md`
- Modify: `.Memory/Abstract.md`

**Interfaces:**
- Consumes: final code and test evidence.
- Produces: durable project decision record.

- [x] **Step 1: Run the adjacent regression set**

```bash
uv run python -m pytest tests/test_programming_completion_guards.py tests/test_programming_card_session.py tests/test_card_orchestrator.py tests/test_deep_renderer_orchestrator.py tests/test_render_task_list.py tests/test_card_renderer.py tests/test_footer_v2.py tests/test_header_v2.py tests/test_sticky_head.py -q
```

- [x] **Step 2: Run quality gates**

```bash
uv run ruff check src/card/tool_display.py src/card/programming_adapter.py src/card/orchestrator.py src/card/render/header.py src/card/render/task_list.py src/card/render/sticky_head.py src/card/render/renderer.py src/card/render/footer.py tests/test_tool_display.py tests/test_programming_card_session.py tests/test_card_orchestrator.py tests/test_render_task_list.py tests/test_card_renderer.py tests/test_footer_v2.py tests/test_header_v2.py
uv run python -m src.main --validate
git diff --check
```

Expected: zero failures and no diff whitespace errors.

- [x] **Step 3: Record the result**

Append a detailed entry to `.Memory/2026-07-24.md` covering the screenshot symptoms, render-boundary changes, RED/GREEN evidence, and remaining real-tenant visual risk. Add a roughly 20-character summary link in `.Memory/Abstract.md`.
