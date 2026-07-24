# Task Completion Lifecycle Reliability Plan

> Date: 2026-07-24
> Scope: ordinary programming cards, shell/repository locking, ACP stop reasons, and Deep terminal state.

## Evidence-backed failures

1. `ProgrammingCardSession` receives `subagent_session_factory` in production but no
   `session_factory`. Its current guard therefore returns the parent session for an
   `agent`/`task` tool call; `TOOL_CALL_DONE` dispatches `COMPLETED` to the parent,
   closes the main CardKit stream, and drops all later ACP events.
2. P2P messages deliberately bypass `RepoLockManager`. A second P2P chat therefore
   ran `restart.sh rr` while Codex held the repository lock, sent SIGTERM to
   GhostAP, and broke the active ACP connection.
3. Ordinary programming treats every normally returned `PromptResult` as success,
   including cancellation, token/turn limits, CLI failure, and results that still
   contain active plan/tool entries. It then adds an unconditional success reaction.
4. Deep marks `max_turn_requests` as completed even though it is an execution limit,
   not proof that the requested work is done.

## Implementation

### 1. Reproduce the parent-card terminal leak

**Files**

- Add `tests/test_programming_completion_guards.py`

Construct the adapter exactly as the production handler does: a parent
`CardSession` plus only `subagent_session_factory`. Send an agent tool start/done
and then a text event. Assert that:

- a child session receives the agent terminal event;
- the parent remains `running`;
- the later text is still accepted by the parent.

Also cover the no-factory degradation path: an agent-shaped tool is rendered as a
normal parent tool and must never terminalize the parent.

### 2. Repair child routing without weakening terminal fences

**Files**

- Modify `src/card/programming_adapter.py`
- Modify `src/feishu/handlers/programming.py`

Treat either child factory as sufficient. If neither exists, return control to the
normal tool-event path instead of reusing the parent as a child. Derive child
metadata from the parent and explicitly pass production metadata from the handler.
Keep the existing CardSession post-terminal fence unchanged.

### 3. Reproduce and close the P2P shell lock bypass

**Files**

- Add `tests/test_shell_repo_lock_strict.py`
- Modify `src/feishu/handlers/lock_helper.py`
- Modify `src/feishu/handlers/system.py`

Add a strict lock helper that always performs a real repository acquisition while
preserving the existing P2P bypass for callers that intentionally use it. Route
shell execution through the strict helper. The regression holds the repository
from chat A, sets chat B's context to P2P, and proves chat B's shell body is not
called.

### 4. Make ACP completion classification fail closed

**Files**

- Add `src/acp/outcome.py`
- Extend `tests/test_programming_completion_guards.py`
- Modify `src/card/programming_adapter.py`
- Modify `src/feishu/handlers/programming.py`
- Modify `src/card/ui_text.py`

Introduce one pure classifier:

```python
end_turn + no pending plan/tool -> completed
cancelled/canceled -> cancelled
everything else -> incomplete
```

Streaming and fallback paths will use the same result. Completed results keep the
green terminal state; cancellation uses the existing cancelled state; incomplete
results use the existing failed state with an explicit stop reason. Remove the
streaming handler's unconditional success reaction and only add fallback reactions
when no terminal card hook could do so.

### 5. Correct Deep limit semantics and strengthen the completion contract

**Files**

- Add `tests/test_deep_completion_guard.py`
- Modify `src/deep_engine/engine.py`
- Modify `AGENTS.md`

Deep completes only on a classified completed result. `max_turn_requests` and
pending plan/tool work become failed, while cancellation remains paused. Apply the
same rule on resume. Add a failure-derived repository rule: an agent may not claim
completion while its own final report lists an unimplemented requested path,
unverified core acceptance case, or relevant failing test.

### 6. Verification and review

Run, in order:

```bash
uv run python -m pytest tests/test_programming_completion_guards.py tests/test_shell_repo_lock_strict.py tests/test_deep_completion_guard.py -q
uv run python -m pytest tests/test_programming_card_session.py tests/test_repo_lock.py tests/test_handlers.py tests/test_deep_engine.py -q
uv run ruff check src/acp/outcome.py src/card/programming_adapter.py src/feishu/handlers/programming.py src/feishu/handlers/lock_helper.py src/feishu/handlers/system.py src/deep_engine/engine.py tests/test_programming_completion_guards.py tests/test_shell_repo_lock_strict.py tests/test_deep_completion_guard.py
uv run python -m src.main --validate
uv run python -m pytest tests/ -q -m "not slow"
git diff --check
```

An independent reviewer will inspect terminal-state races, P2P privilege
regressions, and overlap with the pre-existing uncommitted image-card work before
the Memory entry is updated.
