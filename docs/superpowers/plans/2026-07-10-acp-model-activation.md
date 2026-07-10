# ACP Model Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make final ACP model confirmation return immediately, initialize the selected programming session through the existing scheduler, and render truthful initializing/ready/failed states.

**Architecture:** `SystemHandler` persists the selection and PATCHes an initializing card synchronously, then submits one project-serialized activation task. Programming handlers return an explicit success boolean; the background task rejects stale selections, PATCHes the terminal card, and forwards a pending prompt only after success.

**Tech Stack:** Python 3.12, Pydantic `TaskSpec`, `TaskScheduler`, Feishu CardKit JSON, pytest/unittest, ruff, static HTML UX preview.

## Global Constraints

- Reuse `TaskScheduler` and `ACPSessionManager`; add no executor and bypass no session-key lock.
- Apply the shared path to Coco, Claude, Aiden, Codex, Gemini, and Traex.
- Do not change model probing, Workflow/Worktree/Spec selection, startup timeout, or retry counts.
- Create an HTML preview under `ux/` before production card changes.
- Keep raw startup diagnostics in logs; cards use fixed safe text or `safe_error_message()`.
- Start every production behavior change with a failing regression test.

---

### Task 1: ACP activation card states

**Files:**
- Create: `ux/acp_model_activation_preview.html`
- Modify: `src/card/ui_text.py`
- Modify: `src/card/builders/system.py`
- Modify: `src/card/builder.py`
- Test: `tests/test_card_builders.py`

**Interfaces:**
- Consumes: `CoreBuilder._wrap_card()`, `build_responsive_layout()`, `refresh_acp_models`, `select_acp_model`.
- Produces: `CardBuilder.build_acp_programming_initializing_card(...)` and `CardBuilder.build_acp_programming_failed_card(...)`.

- [ ] **Step 1: Create the three-state HTML preview**

Create a standalone dark preview with three mobile-width cards: 初始化中、已就绪、初始化失败. Use identical tool/model rows. The failed card has 重试初始化 and 返回模型选择 buttons.

- [ ] **Step 2: Write failing builder tests**

```python
_, initializing_json = CardBuilder.build_acp_programming_initializing_card(
    "codex", "gpt-5.6-sol", project_id="p1", thread_root_id="t1"
)
assert "正在初始化" in json.loads(initializing_json)["header"]["title"]["content"]

_, failed_json = CardBuilder.build_acp_programming_failed_card(
    "codex", "gpt-5.6-sol", "执行超时", project_id="p1", thread_root_id="t1"
)
values = _collect_button_values(json.loads(failed_json))
assert {v["action"] for v in values} == {"select_acp_model", "refresh_acp_models"}
```

- [ ] **Step 3: Verify RED**

Run: `uv run python -m pytest tests/test_card_builders.py -k "acp_programming" -q`

Expected: FAIL because the builder methods do not exist.

- [ ] **Step 4: Implement minimal text and builders**

Add initializing/failed title, body, retry, and back labels. Retry payload contains action, tool, model, project, and thread. Back payload uses `refresh_acp_models` with tool, project, and thread. Add matching `CardBuilder` forwarding methods.

- [ ] **Step 5: Verify GREEN**

Run: `uv run python -m pytest tests/test_card_builders.py -k "acp_programming" -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ux/acp_model_activation_preview.html src/card/ui_text.py src/card/builders/system.py src/card/builder.py tests/test_card_builders.py
git commit -m "feat(card): add ACP activation states"
```

---

### Task 2: Truthful programming activation result

**Files:**
- Modify: `src/feishu/handlers/programming.py`
- Modify: `src/feishu/handlers/system.py`
- Test: `tests/test_handlers.py`
- Test: `tests/test_switch_model.py`
- Test: `tests/test_model_command.py`

**Interfaces:**
- Consumes: `ProgrammingModeHandler.enter_mode()` and `switch_model()`.
- Produces: both methods return `bool`; `_enter_mode_with_acp_model(..., thread_id=None) -> bool` passes an explicit thread ID and returns the real handler outcome.

- [ ] **Step 1: Write failing result tests**

```python
ctx.coco_manager.ensure_session.side_effect = RuntimeError("startup failed")
assert h.enter_mode("m1", "c1", project=project, silent=True) is False

ctx.coco_manager.ensure_session.return_value = mock_session
assert h.enter_mode("m1", "c1", project=project, silent=True) is True
```

Add switch tests for protocol success, restart success, and restart failure. Add a system-handler test that the explicit `thread_id` reaches `enter_mode()` and a handler `False` remains `False`.

- [ ] **Step 2: Verify RED**

Run: `uv run python -m pytest tests/test_handlers.py tests/test_switch_model.py tests/test_model_command.py -k "activation_result or returns_success or returns_failure or passes_thread" -q`

Expected: FAIL because methods return `None` or the system helper returns `True` unconditionally.

- [ ] **Step 3: Implement boolean outcomes**

`enter_mode()` returns `False` for missing active session, invalid path, timeout, startup exception, and unusable degraded branch; it returns `True` after successful mode/session registration. `switch_model()` returns `True` after protocol or restart success and `False` after its existing error path. `_enter_mode_with_acp_model()` forwards `thread_id` and wraps the chosen method result with `bool()`.

- [ ] **Step 4: Verify GREEN**

Run: `uv run python -m pytest tests/test_handlers.py tests/test_switch_model.py tests/test_model_command.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/feishu/handlers/programming.py src/feishu/handlers/system.py tests/test_handlers.py tests/test_switch_model.py tests/test_model_command.py
git commit -m "fix(acp): report session activation outcome"
```

---

### Task 3: Background activation and stale-selection guard

**Files:**
- Modify: `src/feishu/handlers/system.py`
- Test: `tests/test_model_command.py`

**Interfaces:**
- Consumes: Task 1 card methods, Task 2 boolean activation result, `TaskSpec`, `self.scheduler.submit()`.
- Produces: `_is_current_acp_selection(...)`, `_submit_acp_model_activation(...)`, and a non-blocking `handle_select_acp_model()`.

- [ ] **Step 1: Write the failing non-blocking test**

```python
submitted = []
self.handler.scheduler.submit = lambda spec, fn: submitted.append((spec, fn)) or SimpleNamespace(run_id="run-1")
self.handler.handle_select_acp_model("card1", "chat1", "codex", "gpt", project)
self.handler.get_handler("codex").enter_mode.assert_not_called()
assert submitted[0][0].task_type == "acp_model_activation"
assert "正在初始化" in json.loads(self.handler.update_card.call_args.args[1])["header"]["title"]["content"]
```

- [ ] **Step 2: Write failing terminal and race tests**

Execute the captured callback manually. Assert success PATCHes ready and forwards a pending prompt once; `False` PATCHes failed without forwarding; an exception uses `safe_error_message()`; changed project tool/model makes the task stale and skips activation/terminal PATCH; captured thread ID is forwarded; scheduler submission failure PATCHes failed synchronously.

- [ ] **Step 3: Verify RED**

Run: `uv run python -m pytest tests/test_model_command.py -k "activation or pending_prompt or stale" -q`

Expected: FAIL because selection still activates synchronously.

- [ ] **Step 4: Implement scheduler handoff**

The synchronous handler persists selection, clears the snapshot, pops the pending prompt, captures `thread_root_id`, PATCHes initializing, creates a `TaskSpec(name="activate_acp_model", task_type="acp_model_activation", priority=TaskPriority.HIGH)` with chat/project/message metadata, submits it, and returns. Do not set `is_system_command=True`; default project serialization orders activation before the next project message.

The task checks captured selection before and after activation, passes the explicit thread ID, PATCHes ready only on `True`, forwards pending only after ready, PATCHes a fixed safe failure on `False`, and logs full exceptions while displaying `safe_error_message(exc)`.

- [ ] **Step 5: Verify GREEN**

Run: `uv run python -m pytest tests/test_model_command.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/feishu/handlers/system.py tests/test_model_command.py
git commit -m "perf(acp): initialize selected models asynchronously"
```

---

### Task 4: Expanded verification and project memory

**Files:**
- Modify: `.Memory/2026-07-10.md`
- Modify: `.Memory/Abstract.md`

**Interfaces:**
- Consumes: completed implementation and test evidence.
- Produces: durable root-cause, fix, validation, and residual-risk record.

- [ ] **Step 1: Run shared-boundary tests**

Run: `uv run python -m pytest tests/test_model_command.py tests/test_handlers.py tests/test_switch_model.py tests/test_card_builders.py tests/test_model_cascade.py tests/test_action_dispatch_mapping.py tests/test_ws_client_routing.py tests/test_acp_manager_consistency.py tests/test_acp_startup_utils.py -q`

Expected: PASS.

- [ ] **Step 2: Run static checks**

Run: `uv run ruff check src/feishu/handlers/system.py src/feishu/handlers/programming.py src/card/builders/system.py src/card/builder.py tests/test_model_command.py tests/test_handlers.py tests/test_switch_model.py tests/test_card_builders.py`

Run: `uv run python -m src.main --validate`

Run: `git diff --check`

Expected: all exit 0.

- [ ] **Step 3: Update memory**

Append the 20s+30s synchronous timeout, false-ready bug, staged scheduler solution, test counts, and asynchronous backend-failure residual risk to `.Memory/2026-07-10.md`. Add one dated summary line to `.Memory/Abstract.md`.

- [ ] **Step 4: Commit memory**

```bash
git add .Memory/2026-07-10.md .Memory/Abstract.md
git commit -m "docs(acp): record async model activation fix"
```

- [ ] **Step 5: Check clean state**

Run: `git status --short && git log -6 --oneline`

Expected: clean worktree and commits for plan, cards, activation result, async scheduling, and memory.
