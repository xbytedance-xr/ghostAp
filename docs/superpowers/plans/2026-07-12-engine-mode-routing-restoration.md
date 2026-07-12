# Engine Mode Routing Restoration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore explicit and topic-scoped engine requests to Deep, Spec,
Worktree, and Workflow without changing their autonomous execution logic.

**Architecture:** Fix the flat/locale Lark post compatibility boundary, then
lock the existing engine-priority routing contract with end-to-end regressions.
No engine implementation or prompt behavior is changed.

**Tech Stack:** Python 3.11+, pytest, Lark event payloads, GhostAP Feishu
dispatcher and engine handlers.

## Global Constraints

- Use `uv` only.
- Preserve Deep/Spec/Worktree/Workflow lifecycle and topic semantics.
- Explicit engine commands have priority over persistent programming modes.
- All behavior fixes require a regression test that fails before production
  code changes.
- Update `.Memory/2026-07-12.md` and `.Memory/Abstract.md` after verification.

---

### Task 1: Parse production flat post payloads

**Files:**
- Modify: `src/feishu/image_handler.py`
- Test: `tests/test_image_handler.py`

**Interfaces:**
- Consumes: `FeishuImageHandler.parse_message(message_type, content_str)`.
- Produces: unchanged `ImageParseResult(text: str, image_keys: list[str])`.

- [ ] **Step 1: Write the failing production-payload test**

Add a test whose JSON has top-level `title`, `content`, and `content_v2`, with
`/deep` in the first text row and an image in the second row. Assert exact text
and image key extraction.

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run python -m pytest tests/test_image_handler.py::TestParsePostMessage::test_parse_flat_production_post_with_engine_command_and_image -q
```

Expected: FAIL because the current parser returns empty text and image keys.

- [ ] **Step 3: Implement flat and localized post selection**

Select top-level `content`/`content_v2` when present; otherwise select the
localized `zh_cn`, `en_us`, or first mapping containing post rows. Extract rows
through one helper so both shapes retain identical ordering and filtering.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run:

```bash
uv run python -m pytest tests/test_image_handler.py -q
```

Expected: all parser and download tests pass.

### Task 2: Lock explicit engine command priority

**Files:**
- Test: `tests/test_ws_client_routing.py`
- Modify only if the new test exposes a second routing defect:
  `src/feishu/ws_client.py` or `src/feishu/dispatcher.py`

**Interfaces:**
- Consumes: `_dispatch_message_logic(..., auto_enter_mode, command_match=...)`.
- Produces: calls the existing engine handler for explicit engine commands.

- [ ] **Step 1: Add a parameterized routing regression**

For `/deep`, `/spec`, `/wt`, and `/wf`, provide a parsed `CommandMatch` while
`auto_enter_mode="traex"`. Assert the request reaches `_process_with_intent`
and never reaches `TraexModeHandler.handle_message`. The downstream dispatcher
tests must assert the corresponding engine handler is selected.

- [ ] **Step 2: Run the new test and interpret RED/GREEN honestly**

Run the new node directly. If it passes immediately, retain it as a contract
test and do not change routing production code: the production failure was
fully caused by post parsing. If it fails, make the smallest priority-order fix
and rerun until green.

- [ ] **Step 3: Run the shared routing tests**

Run:

```bash
uv run python -m pytest tests/test_ws_client_routing.py tests/test_feishu_dispatcher.py tests/test_workflow_topic_engine.py -q
```

Expected: all pass.

### Task 3: Verify historical autonomous engine contracts

**Files:**
- Test only; no production engine edits expected.

**Interfaces:**
- Deep/Spec/Worktree/Workflow handlers and current topic manager contracts.

- [ ] **Step 1: Run engine/topic regression suites**

Run the focused Deep, Spec, Worktree, Workflow topic and prompt tests selected
by `rg` for topic persistence, clarification-without-waiting, and engine start.

- [ ] **Step 2: Compare production files with the pre-regression reference**

Confirm no restoration edit changes `src/deep_engine/`, `src/spec_engine/`,
`src/worktree_engine/`, or `src/workflow_engine/` behavior. Record any unrelated
recent audit finding in `.Memory/Backlog.md`; fix only high correctness/security
findings required by the goal.

### Task 4: Verify, document, and converge

**Files:**
- Create: `.Memory/2026-07-12.md`
- Modify: `.Memory/Abstract.md`
- Modify if needed: `.Memory/Backlog.md`

**Interfaces:**
- Produces auditable evidence for the restored behavior and remaining risks.

- [ ] **Step 1: Run expanded verification**

```bash
uv run python -m pytest tests/ -q
uv run ruff check src/feishu tests/test_image_handler.py tests/test_ws_client_routing.py
uv run python -m src.main --validate
git diff --check
```

- [ ] **Step 2: Update project memory**

Record the production evidence, root cause, exact behavior restored, tests,
and the incomplete Employee card communication finding. Add a concise dated
entry to `.Memory/Abstract.md`.

- [ ] **Step 3: Run two stateless review rounds**

Review only the Goal Snapshot and current worktree from product, architecture,
engineering, QA, and user perspectives. Any material finding resets the clean
round count; stop only after two consecutive clean rounds.
