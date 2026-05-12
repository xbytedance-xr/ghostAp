# Topic-Scoped Engine Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Deep, Spec, WT, and future engines continue by Feishu topic, and make WT a topic-scoped single-task engine that can run multiple independent sessions for the same project.

**Architecture:** Extend the existing `ThreadContextManager` into the shared topic-engine registry, remove Deep/Spec/WT engine continuation fallback by chat, and move WT runtime state from `ProjectContext.worktree_state` into a session store keyed by `project_id + chat_id + thread_root_id`. WT starts through `/wt` or `/wt <goal>` by binding a topic, selecting tools/models, then auto-starting when both selection and goal exist.

**Tech Stack:** Python dataclasses, existing Feishu dispatcher/handler architecture, existing `ThreadContextManager`, Worktree engine, CardSession renderer, pytest.

---

## File Structure

- `src/thread/models.py`: add topic-engine status fields to `ThreadContext` only if existing fields are insufficient.
- `src/thread/manager.py`: become the single owner for strict topic engine lookup and binding.
- `src/feishu/ws_client.py`: remove engine continuation chat fallback; force WT topic mode before dispatching WT commands.
- `src/feishu/dispatcher.py`: enforce topic-bound routing precedence and single-engine-per-topic behavior.
- `src/feishu/handlers/worktree.py`: route all WT operations through topic-scoped session state and remove the separate start button flow.
- `src/worktree_engine/session_store.py`: new WT session store and state key helpers.
- `src/worktree_engine/manager.py`: delegate selection/execution state to topic-scoped sessions instead of `ProjectContext.worktree_state`.
- `src/worktree_engine/git_service.py`: include WT session id in generated branch/path names.
- `src/worktree_engine/review_adapter.py`: WT-specific adapter around shared Spec review components.
- `src/card/state/reducers/worktree.py` and `src/card/render/worktree.py`: render `AWAITING_GOAL` and auto-start semantics.
- Tests under `tests/test_thread_manager.py`, `tests/test_ws_client_routing.py`, `tests/test_worktree_selection_flow.py`, `tests/test_worktree_command_routing.py`, `tests/test_worktree_e2e.py`, and new WT session/review tests.

---

### Task 1: Strict Topic Engine Registry

**Files:**
- Modify: `src/thread/models.py`
- Modify: `src/thread/manager.py`
- Test: `tests/test_thread_manager.py`

- [ ] **Step 1: Add failing tests for strict topic lookup**

Add tests proving lookup only works by exact topic root and that chat fallback is not part of engine continuation.

```python
def test_engine_context_requires_exact_thread_root():
    mgr = ThreadContextManager(ttl=3600, cleanup_interval=999)
    mgr.register(
        "thread-wt-1",
        "chat-1",
        "proj-1",
        mode="worktree",
        tool_name="coco",
        model_name="m1",
    )

    assert mgr.get("thread-wt-1").mode == "worktree"
    assert mgr.get("missing-thread") is None
    assert [ctx.thread_root_id for ctx in mgr.get_by_chat("chat-1")] == ["thread-wt-1"]
```

Add a single-engine-per-topic test:

```python
def test_registering_different_engine_for_same_topic_updates_only_when_allowed():
    mgr = ThreadContextManager(ttl=3600, cleanup_interval=999)
    mgr.register("thread-1", "chat-1", "proj-1", mode="worktree")

    assert mgr.has_active_engine("thread-1") is True
    assert mgr.get("thread-1").mode == "worktree"
```

- [ ] **Step 2: Run the thread tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_thread_manager.py::TestThreadContextManager -q
```

Expected: failure for missing `has_active_engine()` or equivalent strict helper.

- [ ] **Step 3: Implement strict topic helpers**

Add helpers to `ThreadContextManager`:

```python
def get_engine_context(self, thread_root_id: str) -> Optional[ThreadContext]:
    ctx = self.get(thread_root_id)
    if ctx and ctx.mode and ctx.mode != "smart":
        return ctx
    return None

def has_active_engine(self, thread_root_id: str) -> bool:
    return self.get_engine_context(thread_root_id) is not None

def bind_engine(
    self,
    *,
    thread_root_id: str,
    chat_id: str,
    project_id: str,
    mode: str,
    tool_name: Optional[str] = None,
    model_name: Optional[str] = None,
    alias_keys: Optional[list[str]] = None,
) -> ThreadContext:
    return self.register(
        thread_root_id,
        chat_id,
        project_id,
        mode=mode,
        tool_name=tool_name,
        model_name=model_name,
        alias_keys=alias_keys,
    )
```

Do not add any method that resolves an engine context from chat alone.

- [ ] **Step 4: Run thread tests**

Run:

```bash
uv run python -m pytest tests/test_thread_manager.py -q
```

Expected: all thread manager tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/thread/models.py src/thread/manager.py tests/test_thread_manager.py
git commit -m "feat: add strict topic engine registry"
```

---

### Task 2: Remove Engine Continuation Chat Fallback

**Files:**
- Modify: `src/feishu/ws_client.py`
- Modify: `src/feishu/dispatcher.py`
- Test: `tests/test_ws_client_routing.py`
- Test: `tests/test_feishu_dispatcher.py`

- [ ] **Step 1: Add failing routing tests**

Add a test where a chat has a registered WT topic but an incoming non-topic message must not auto-route to WT.

```python
def test_non_topic_message_does_not_fallback_to_recent_worktree_topic(mock_ws_client):
    mock_ws_client.settings.thread_programming_enabled = True
    mock_ws_client._thread_manager.register(
        "thread-wt",
        "chat-1",
        "proj-1",
        mode="worktree",
    )
    msg = create_mock_message("继续", message_id="msg-plain", chat_id="chat-1")
    msg.event.message.root_id = None
    msg.event.message.parent_id = None

    mock_ws_client._handle_message(msg)

    spec, _ = mock_ws_client._scheduler.submit.call_args[0]
    assert spec.queue_key is None or ":t:thread-wt" not in str(spec.queue_key)
```

Add dispatcher tests for topic-bound routing:

```python
def test_topic_bound_worktree_message_routes_to_worktree(client):
    project = ProjectContext("proj-1", "P", "/tmp/p")
    client._thread_manager.register("thread-wt", "chat-1", "proj-1", mode="worktree")
    client._handle_worktree_execute = MagicMock()

    # Build a message-like object with root_id=thread-wt and plain text.
    # Dispatch through the same async path used by ws_client tests.
```

- [ ] **Step 2: Run routing tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_ws_client_routing.py tests/test_feishu_dispatcher.py -q
```

Expected: current chat fallback causes the new non-topic test to fail.

- [ ] **Step 3: Remove chat-level engine fallback**

In `src/feishu/ws_client.py`, remove or guard the blocks that call `get_by_chat(chat_id)` for engine continuation. Keep exact `root_id` lookup.

Replacement rule:

```python
if root_id and self.settings.thread_programming_enabled:
    thread_ctx = self._thread_manager.get_engine_context(root_id)
    if thread_ctx:
        project_id = thread_ctx.project_id
        thread_root_id = thread_ctx.thread_root_id
```

Do not infer `thread_root_id` from `get_by_chat()` for Deep/Spec/WT continuation.

- [ ] **Step 4: Enforce topic-bound precedence**

In `src/feishu/dispatcher.py`, keep topic-bound engine routing before project active mode fallback. Explicit slash/system commands still keep priority.

Expected behavior:

```text
root_id has engine context + plain text -> route to that engine
no root_id + plain text -> do not route to recent topic
```

- [ ] **Step 5: Run routing tests**

Run:

```bash
uv run python -m pytest tests/test_ws_client_routing.py tests/test_feishu_dispatcher.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/feishu/ws_client.py src/feishu/dispatcher.py tests/test_ws_client_routing.py tests/test_feishu_dispatcher.py
git commit -m "fix: make engine continuation strictly topic scoped"
```

---

### Task 3: Single Engine Per Topic Guard

**Files:**
- Modify: `src/feishu/dispatcher.py`
- Modify: `src/feishu/handlers/system.py`
- Modify: `src/card/ui_text.py`
- Test: `tests/test_feishu_dispatcher.py`
- Test: `tests/test_card_builders.py`

- [ ] **Step 1: Add failing tests for cross-engine commands in bound topics**

Add tests for `/spec` inside WT topic and `/deep` inside Spec topic.

```python
def test_spec_command_inside_worktree_topic_does_not_switch_engine(client):
    client._thread_manager.register("thread-wt", "chat-1", "proj-1", mode="worktree")
    client._handle_spec_command = MagicMock()
    client._reply_text = MagicMock()

    # Dispatch text "/spec build X" with root_id="thread-wt".

    client._handle_spec_command.assert_not_called()
    assert "当前话题已绑定" in client._reply_text.call_args[0][1]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_feishu_dispatcher.py -q
```

Expected: current explicit command path may switch modes or bypass the guard.

- [ ] **Step 3: Add guard helper**

Add a dispatcher helper:

```python
def _guard_topic_engine_switch(self, message_id: str, chat_id: str, root_id: str | None, target_mode: str) -> bool:
    if not root_id:
        return False
    ctx = self.client._thread_manager.get_engine_context(root_id)
    if not ctx or ctx.mode == target_mode:
        return False
    self.client._reply_text(
        message_id,
        UI_TEXT["topic_engine_switch_blocked"].format(
            current=ctx.mode,
            target=target_mode,
        ),
    )
    return True
```

Add `topic_engine_switch_blocked` to `UI_TEXT`:

```python
"topic_engine_switch_blocked": "当前话题已绑定 {current} 任务，不能直接切换到 {target}。请先停止当前任务，或在新话题中启动 {target}。",
```

- [ ] **Step 4: Apply guard to Deep/Spec/WT entry commands**

Before handling `/deep`, `/spec`, `/wt`, and future engine entry commands, call the guard when the message has `root_id`.

- [ ] **Step 5: Run tests**

Run:

```bash
uv run python -m pytest tests/test_feishu_dispatcher.py tests/test_card_builders.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/feishu/dispatcher.py src/feishu/handlers/system.py src/card/ui_text.py tests/test_feishu_dispatcher.py tests/test_card_builders.py
git commit -m "feat: prevent implicit engine switching in topics"
```

---

### Task 4: Worktree Topic Session Store

**Files:**
- Create: `src/worktree_engine/session_store.py`
- Modify: `src/worktree_engine/models.py`
- Modify: `src/worktree_engine/manager.py`
- Test: `tests/test_worktree_session_store.py`

- [ ] **Step 1: Add failing store tests**

Create `tests/test_worktree_session_store.py`:

```python
def test_two_topics_same_project_keep_independent_worktree_state():
    store = WorktreeSessionStore()
    key_a = WorktreeSessionKey(project_id="p1", chat_id="c1", thread_root_id="t1")
    key_b = WorktreeSessionKey(project_id="p1", chat_id="c1", thread_root_id="t2")

    state_a = store.create(key_a, goal="fix card")
    state_b = store.create(key_b, goal="fix router")

    state_a.selection.selected_items.append("coco")
    state_b.selection.selected_items.append("codex")

    assert store.get(key_a).goal == "fix card"
    assert store.get(key_b).goal == "fix router"
    assert store.get(key_a).selection.selected_items == ["coco"]
    assert store.get(key_b).selection.selected_items == ["codex"]
```

- [ ] **Step 2: Run store test and verify failure**

Run:

```bash
uv run python -m pytest tests/test_worktree_session_store.py -q
```

Expected: import failure for new store.

- [ ] **Step 3: Implement key and store**

Create `src/worktree_engine/session_store.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from .models import WorktreeRuntimeState


@dataclass(frozen=True)
class WorktreeSessionKey:
    project_id: str
    chat_id: str
    thread_root_id: str

    @property
    def session_id(self) -> str:
        return f"{self.project_id}:{self.chat_id}:{self.thread_root_id}"


class WorktreeSessionStore:
    def __init__(self) -> None:
        self._states: dict[WorktreeSessionKey, WorktreeRuntimeState] = {}
        self._lock = Lock()

    def get(self, key: WorktreeSessionKey) -> WorktreeRuntimeState | None:
        with self._lock:
            return self._states.get(key)

    def get_or_create(self, key: WorktreeSessionKey, goal: str = "") -> WorktreeRuntimeState:
        with self._lock:
            state = self._states.get(key)
            if state is None:
                state = WorktreeRuntimeState()
                state.journey.goal = goal
                self._states[key] = state
            return state

    def create(self, key: WorktreeSessionKey, goal: str = "") -> WorktreeRuntimeState:
        with self._lock:
            state = WorktreeRuntimeState()
            state.journey.goal = goal
            self._states[key] = state
            return state

    def list_project_sessions(self, project_id: str) -> list[WorktreeRuntimeState]:
        with self._lock:
            return [state for key, state in self._states.items() if key.project_id == project_id]
```

Use real existing fields from `WorktreeRuntimeState`. If `goal` is not a direct property, keep the goal on `state.journey.goal`.

- [ ] **Step 4: Add manager accessors**

In `WorktreeManager`, add a store and helpers:

```python
def get_session_state(self, key: WorktreeSessionKey) -> WorktreeRuntimeState:
    return self._session_store.get_or_create(key)
```

Do not remove existing `get_state(project)` yet; keep compatibility until WT handlers are migrated.

- [ ] **Step 5: Run tests**

Run:

```bash
uv run python -m pytest tests/test_worktree_session_store.py tests/test_worktree_selection_controller.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/worktree_engine/session_store.py src/worktree_engine/models.py src/worktree_engine/manager.py tests/test_worktree_session_store.py
git commit -m "feat: add topic-scoped worktree session store"
```

---

### Task 5: WT Command Flow Without Start Button

**Files:**
- Modify: `src/feishu/handlers/worktree.py`
- Modify: `src/card/state/reducers/worktree.py`
- Modify: `src/card/render/worktree.py`
- Modify: `src/card/ui_text.py`
- Test: `tests/test_worktree_selection_flow.py`
- Test: `tests/test_worktree_command_routing.py`
- Test: `tests/test_worktree_e2e.py`

- [ ] **Step 1: Add failing tests for `/wt <goal>` auto-start after selection**

Add a test:

```python
def test_wt_goal_selects_tools_then_auto_starts_without_start_button():
    handler = _make_system_handler()
    project = ProjectContext(project_id="p1", project_name="P", root_path="/tmp/p")
    handler.ctx.project_manager.get_project_for_chat.return_value = project

    handler.handle_worktree_command("msg", "chat1", project, goal="修复卡片")
    handler.handle_worktree_select_tool(
        "msg-tool",
        "chat1",
        project_id="p1",
        value={"tool_name": "coco", "display_name": "Coco", "supports_model": False},
    )
    handler.handle_finish_worktree_selection("msg-finish", "chat1", project_id="p1")

    # Assert execution path is called automatically and no worktree_confirm_start button is required.
```

- [ ] **Step 2: Add failing tests for plain `/wt` awaiting first goal message**

Add a test:

```python
def test_plain_wt_waits_for_first_topic_message_as_goal():
    handler = _make_system_handler()
    project = ProjectContext(project_id="p1", project_name="P", root_path="/tmp/p")
    handler.ctx.project_manager.get_project_for_chat.return_value = project

    handler.handle_worktree_command("msg", "chat1", project, goal="")
    handler.handle_worktree_select_tool(
        "msg-tool",
        "chat1",
        project_id="p1",
        value={"tool_name": "coco", "display_name": "Coco", "supports_model": False},
    )
    handler.handle_finish_worktree_selection("msg-finish", "chat1", project_id="p1")

    state = handler._worktree_manager().get_state(project)
    assert state.journey.status.value == "awaiting_goal"
```

Use the actual enum value name in the assertion after checking `WorktreeJourneyStatus`.

- [ ] **Step 3: Run WT flow tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_worktree_selection_flow.py tests/test_worktree_command_routing.py tests/test_worktree_e2e.py -q
```

Expected: current confirm/start button flow fails the new tests.

- [ ] **Step 4: Add `AWAITING_GOAL` state**

In `src/worktree_engine/models.py`, add a WT journey status for awaiting goal if no equivalent exists:

```python
AWAITING_GOAL = "awaiting_goal"
```

Update `WorktreeManager.is_awaiting_goal()` truth table to return `True` for `AWAITING_GOAL` when selected units are ready or selection is complete.

- [ ] **Step 5: Change finish selection behavior**

In `handle_finish_worktree_selection()`:

```python
if goal:
    self._auto_execute_worktree(message_id, chat_id, goal, project=project)
    return

mgr.apply_journey_event(state, event="awaiting_goal")
session.dispatch(worktree_confirm(
    selected_items=selected_dicts,
    project_id=pid,
    message=UI_TEXT["worktree_awaiting_goal_message"],
))
```

Do not render `WORKTREE_CONFIRM_START` for this state.

- [ ] **Step 6: Route first topic message as goal**

In the dispatcher worktree path, when topic-bound WT is awaiting goal, route the first ordinary message to `handle_worktree_execute()`.

Expected behavior:

```python
if project and self.client._is_worktree_awaiting_goal(project):
    self.client._handle_worktree_execute(message_id, chat_id, text, project)
    return
```

After Task 4 migration, this must use the topic WT session key, not project singleton state.

- [ ] **Step 7: Remove start button from WT confirm/awaiting cards**

Update reducer/render logic so `WORKTREE_CONFIRM_START` is not shown for selection completion. Keep reselect/cancel where useful.

Add UI copy:

```python
"worktree_awaiting_goal_message": "已完成工具选择。请在当前话题发送任务目标，WT 将自动开始规划和执行。",
```

- [ ] **Step 8: Run WT flow tests**

Run:

```bash
uv run python -m pytest tests/test_worktree_selection_flow.py tests/test_worktree_command_routing.py tests/test_worktree_e2e.py -q
```

Expected: pass.

- [ ] **Step 9: Commit**

```bash
git add src/feishu/handlers/worktree.py src/card/state/reducers/worktree.py src/card/render/worktree.py src/card/ui_text.py tests/test_worktree_selection_flow.py tests/test_worktree_command_routing.py tests/test_worktree_e2e.py
git commit -m "feat: streamline worktree topic goal flow"
```

---

### Task 6: WT Topic Binding And Forced Topic Mode

**Files:**
- Modify: `src/feishu/handlers/worktree.py`
- Modify: `src/feishu/ws_client.py`
- Test: `tests/test_worktree_command_routing.py`
- Test: `tests/test_ws_client_routing.py`

- [ ] **Step 1: Add failing tests for WT forced topic mode**

Add tests proving `/wt` creates a topic binding even when general thread programming is disabled.

```python
def test_wt_forces_topic_binding_when_thread_programming_disabled():
    client = make_ws_client()
    client.settings.thread_programming_enabled = False
    client._thread_manager.register = MagicMock()

    # Send /wt goal outside a topic.

    client._thread_manager.register.assert_called()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_worktree_command_routing.py tests/test_ws_client_routing.py -q
```

Expected: current WT may respect `thread_programming_enabled` and not force binding.

- [ ] **Step 3: Add WT topic binding helper**

In `WorktreeHandler`, add a helper:

```python
def _ensure_worktree_topic(
    self,
    message_id: str,
    chat_id: str,
    project: ProjectContext,
    *,
    goal: str,
) -> str:
    current_thread_id = get_current_thread_id()
    if current_thread_id:
        return current_thread_id
    topic_root_id = self.reply_card(
        message_id,
        *self.card_builder.build_info_card(
            project,
            "Worktree",
            UI_TEXT["worktree_topic_created"],
        ),
    )
    self.ctx.thread_manager.bind_engine(
        thread_root_id=topic_root_id,
        chat_id=chat_id,
        project_id=project.project_id,
        mode="worktree",
    )
    return topic_root_id
```

Use existing handler/card APIs rather than introducing a new Feishu API path.

- [ ] **Step 4: Bind `/wt` and `/wt <goal>`**

At WT command entry, call `_ensure_worktree_topic()` before starting selection. Store the resulting `thread_root_id` in the WT session key.

- [ ] **Step 5: Run WT binding tests**

Run:

```bash
uv run python -m pytest tests/test_worktree_command_routing.py tests/test_ws_client_routing.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/feishu/handlers/worktree.py src/feishu/ws_client.py tests/test_worktree_command_routing.py tests/test_ws_client_routing.py
git commit -m "feat: force topic binding for worktree"
```

---

### Task 7: Session-Scoped Worktree Branches And Merge Serialization

**Files:**
- Modify: `src/worktree_engine/git_service.py`
- Modify: `src/worktree_engine/manager.py`
- Modify: `src/feishu/handlers/worktree.py`
- Test: `tests/test_worktree_git_service.py`
- Test: `tests/test_worktree_command_routing.py`
- Test: `tests/test_repo_lock.py`

- [ ] **Step 1: Add failing branch/path collision tests**

Add a test:

```python
def test_create_units_uses_session_id_in_branch_and_path(tmp_path):
    service = WorktreeGitService()
    repo = init_repo(tmp_path)

    state_a, units_a = service.create_units(str(repo), count=1, session_slug="t1")
    state_b, units_b = service.create_units(str(repo), count=1, session_slug="t2")

    assert units_a[0].branch_name != units_b[0].branch_name
    assert units_a[0].worktree_path != units_b[0].worktree_path
    assert "t1" in units_a[0].branch_name
    assert "t2" in units_b[0].branch_name
```

- [ ] **Step 2: Run git service tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_worktree_git_service.py -q
```

Expected: current deterministic `wt-01` naming collides across sessions.

- [ ] **Step 3: Add session slug to unit creation**

Change `create_units()` signature:

```python
def create_units(
    self,
    root_path: str,
    count: int,
    base_branch: str | None = None,
    custom_base_dir: str | None = None,
    session_slug: str = "default",
) -> tuple[RepoState, list[WorktreeUnit]]:
```

Use names:

```python
branch_name = f"ghostap/wt/{session_slug}/{index:02d}-unit"
worktree_name = f"wt-{session_slug}-{index:02d}"
```

- [ ] **Step 4: Pass session slug from WT session key**

In `WorktreeManager.ensure_worktrees()`, pass a short safe slug derived from `thread_root_id` or session id.

- [ ] **Step 5: Keep merge project-serialized**

Verify merge path still goes through repo/project lock. Add a test that two WT sessions for the same project cannot merge concurrently.

- [ ] **Step 6: Run tests**

Run:

```bash
uv run python -m pytest tests/test_worktree_git_service.py tests/test_repo_lock.py tests/test_worktree_command_routing.py -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/worktree_engine/git_service.py src/worktree_engine/manager.py src/feishu/handlers/worktree.py tests/test_worktree_git_service.py tests/test_repo_lock.py tests/test_worktree_command_routing.py
git commit -m "feat: scope worktree branches by topic session"
```

---

### Task 8: WT Review Adapter

**Files:**
- Create: `src/worktree_engine/review_adapter.py`
- Modify: `src/worktree_engine/manager.py`
- Modify: `src/worktree_engine/models.py`
- Test: `tests/test_worktree_review_adapter.py`
- Test: `tests/test_worktree_execute_flow.py`

- [ ] **Step 1: Add failing review adapter tests**

Create `tests/test_worktree_review_adapter.py`:

```python
def test_review_adapter_derives_programming_roles_from_goal_and_diff():
    adapter = WorktreeReviewAdapter()
    plan = adapter.plan_roles(
        goal="修复 WT 路由",
        changed_files=["src/feishu/dispatcher.py", "tests/test_feishu_dispatcher.py"],
    )

    role_ids = {role.role_id for role in plan.roles}
    assert {"architect", "tester", "integration"} <= role_ids
```

Add evidence gate test:

```python
def test_review_adapter_downgrades_blocker_without_evidence():
    adapter = WorktreeReviewAdapter()
    outcome = adapter.aggregate([
        {"role_id": "tester", "severity": "blocker", "evidence": "", "message": "bad"}
    ])

    assert outcome.blockers == []
    assert outcome.observations
```

- [ ] **Step 2: Run adapter tests and verify failure**

Run:

```bash
uv run python -m pytest tests/test_worktree_review_adapter.py -q
```

Expected: import failure for new adapter.

- [ ] **Step 3: Implement lightweight adapter**

Create `src/worktree_engine/review_adapter.py`:

```python
from dataclasses import dataclass, field

@dataclass
class WorktreeReviewRole:
    role_id: str
    display_name: str
    blocking: bool = True

@dataclass
class WorktreeReviewPlan:
    roles: list[WorktreeReviewRole] = field(default_factory=list)

@dataclass
class WorktreeReviewOutcome:
    blockers: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)

class WorktreeReviewAdapter:
    def plan_roles(self, *, goal: str, changed_files: list[str]) -> WorktreeReviewPlan:
        roles = [
            WorktreeReviewRole("architect", "架构审查"),
            WorktreeReviewRole("tester", "测试审查"),
            WorktreeReviewRole("integration", "集成审查"),
            WorktreeReviewRole("product", "目标验收"),
        ]
        if any("security" in path or "auth" in path for path in changed_files):
            roles.append(WorktreeReviewRole("security", "安全审查"))
        return WorktreeReviewPlan(roles=roles)

    def aggregate(self, findings: list[dict]) -> WorktreeReviewOutcome:
        blockers: list[dict] = []
        observations: list[dict] = []
        for finding in findings:
            if finding.get("severity") == "blocker" and finding.get("evidence"):
                blockers.append(finding)
            else:
                observations.append(finding)
        return WorktreeReviewOutcome(blockers=blockers, observations=observations)
```

This first adapter establishes the WT review contract without coupling WT to the full Spec lifecycle.

- [ ] **Step 4: Integrate adapter after execution**

After WT execution completes, compute changed files per unit and run the adapter. Store review plan/outcome on WT runtime state.

- [ ] **Step 5: Run review and execution tests**

Run:

```bash
uv run python -m pytest tests/test_worktree_review_adapter.py tests/test_worktree_execute_flow.py -q
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/worktree_engine/review_adapter.py src/worktree_engine/manager.py src/worktree_engine/models.py tests/test_worktree_review_adapter.py tests/test_worktree_execute_flow.py
git commit -m "feat: add worktree review adapter"
```

---

### Task 9: Integration Validation And Documentation

**Files:**
- Modify: `.Memory/2026-05-12.md`
- Modify: `.Memory/Abstract.md`
- Modify: `docs/superpowers/specs/2026-05-12-topic-scoped-engine-sessions-design.md` only if implementation reveals a necessary correction.

- [ ] **Step 1: Run focused routing and WT suites**

Run:

```bash
uv run python -m pytest tests/test_thread_manager.py tests/test_ws_client_routing.py tests/test_feishu_dispatcher.py tests/test_worktree_selection_flow.py tests/test_worktree_command_routing.py tests/test_worktree_e2e.py tests/test_worktree_git_service.py tests/test_worktree_execute_flow.py -q
```

Expected: all pass.

- [ ] **Step 2: Run broader Worktree/card/action suites**

Run:

```bash
uv run python -m pytest tests/test_chat_lock.py tests/test_worktree_adapter_sequence.py tests/test_card_renderer.py tests/test_card_render_fallback.py tests/test_worktree_safe_delete.py tests/test_worktree_goal_persistence.py tests/test_action_dispatch_mapping.py tests/test_worktree_render_direct.py tests/test_card_render_atoms.py tests/test_worktree_card.py tests/test_card_pipeline_integration.py tests/test_worktree_selection_controller.py tests/test_worktree_dispatcher_timeout.py tests/test_worktree_tool_discovery.py tests/test_worktree_dispatcher.py tests/test_chat_lock_gate.py tests/test_card_render_panels.py tests/test_worktree_session_factory.py tests/test_worktree_renderer.py tests/test_worktree_auto_execute.py tests/test_worktree_reporter.py tests/test_worktree_remote_branch.py tests/test_card_render_components.py tests/test_worktree_custom_path.py tests/test_worktree_sync.py tests/test_worktree_card_flow.py tests/test_worktree_performance.py tests/test_worktree_list.py -q
```

Expected: all pass.

- [ ] **Step 3: Run full suite when focused suites pass**

Run:

```bash
uv run python -m pytest tests/ -q
```

Expected: all pass or only known unrelated environmental failures documented with exact failure names.

- [ ] **Step 4: Run config validation**

Run:

```bash
uv run python -m src.main --validate
```

Expected: `GhostAP v0.2.0 配置校验通过`.

- [ ] **Step 5: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 6: Update Memory**

Add a concrete entry to `.Memory/2026-05-12.md` and `.Memory/Abstract.md` including:

```text
- topic-scoped engine routing changed to strict root_id continuation;
- WT state moved to topic-scoped sessions;
- /wt and /wt <goal> selection/goal behavior;
- validation commands and results.
```

- [ ] **Step 7: Commit final documentation update**

```bash
git add .Memory/2026-05-12.md .Memory/Abstract.md
git commit -m "docs: record topic-scoped worktree implementation"
```

---

## Self-Review

- Spec coverage: Tasks 1-3 cover shared topic continuation and single-engine-per-topic behavior. Tasks 4-7 cover WT topic sessions, goal flow, forced topic mode, branch/path isolation, and merge serialization. Task 8 covers WT's Spec-like review adapter. Task 9 covers validation and memory updates.
- Placeholder scan: This plan intentionally avoids open-ended implementation notes; each task has tests, implementation shape, commands, and expected results.
- Type consistency: The plan consistently uses `ThreadContextManager`, `ThreadContext`, `WorktreeSessionKey`, `WorktreeSessionStore`, `WorktreeRuntimeState`, `WorktreeManager`, and `WorktreeReviewAdapter`.
