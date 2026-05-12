# Topic-Scoped Engine Sessions Design

## Background

GhostAP already uses Feishu/Lark topics for programming conversations in parts of
the system. ACP sessions can be keyed by `chat_id + project_id + thread_id`, and
incoming messages try to recover a thread context before routing. That direction
is correct, but the behavior is uneven across engines.

Worktree is the clearest gap. Current WT state is effectively project-scoped via
`ProjectContext.worktree_state`. That means two Feishu topics working on the same
project can overwrite each other's selection, goal, units, merge notes, and
journey state. The current `/wt` flow also lets users reach a confirmation card
without a clear target-input step, so it is possible to click "start" without
understanding that the real goal still needs to be supplied later.

The product direction is:

- keep WT as a single-task engine;
- let Feishu topics provide natural parallelism;
- allow the same project to have multiple independent WT tasks in different
  topics;
- make WT, Deep, Spec, and future engines share one topic-bound continuation
  model.

## Goals

- Bind engine sessions to Feishu topics, not only to projects.
- Make ordinary replies inside a bound topic continue the topic's engine mode.
- Support multiple concurrent WT sessions for the same project when they live in
  different topics.
- Keep WT single-task: one topic, one WT goal, one iterative WT lifecycle.
- Require a clear WT goal before execution starts.
- Reuse Spec's adaptive multi-role review approach inside each WT task.
- Keep global coordination intentionally light: no automatic global task
  splitting or scheduler in the first version.
- Preserve project-level merge serialization so concurrent WT sessions do not
  merge into the base branch at the same time.

## Non-Goals

- Do not build a global task planner that decomposes one large goal into many WT
  tasks across topics.
- Do not make WT automatically decide which independent product problems should
  run in parallel.
- Do not block WT execution by default based on predicted file overlap.
- Do not require users to manually manage branches or worktree paths.
- Do not redesign ACP/TTADK provider selection beyond what WT needs for tool and
  model pools.

## Recommended Approach

Introduce a shared topic-scoped engine session model:

```text
EngineSessionKey = project_id + chat_id + thread_root_id + engine_type
```

For engines that are naturally one active instance per topic, `engine_type` is
part of the key but the topic should normally have only one active engine at a
time. If a user starts `/spec` in a topic that already has active `/wt`, the
system should show an explicit switch/stop confirmation instead of silently
rerouting.

WT should move from project-scoped state to topic-scoped state:

```text
WorktreeSessionState
  session_id
  project_id
  chat_id
  thread_root_id
  goal
  selected_tool_models
  plan
  estimated_change_scope
  worktree_units
  review_state
  merge_state
  status
```

The project keeps only aggregate indexes:

- active WT sessions for status cards;
- project-level merge lock state;
- optional overlap warnings;
- lookup from `thread_root_id` to WT session.

## Product Interaction

### Start WT

Primary command:

```text
/wt <需求目标>
```

This starts or resumes a WT task in the current Feishu topic.

If the message is not already inside a topic, GhostAP should create/reply with a
topic card and bind that topic as the WT session root. If it is already inside a
topic, the current topic is the session root.

### Empty `/wt`

If a user sends plain `/wt`:

- if the current topic has an active WT session, show that WT status;
- if the current topic has no WT session, show a goal-input card;
- if there are other WT sessions in the same project, show them as secondary
  context but do not enter them automatically.

The goal-input card must make the next required step explicit. It should not show
"start execution" until a non-empty goal exists.

### Continue WT

After a WT session is bound to a topic, ordinary messages in that topic route to
the current WT session when WT is the active topic engine.

Examples:

- user replies "这个方案里不要改配置层" -> WT treats it as guidance;
- user replies "继续" -> WT continues current cycle when paused/awaiting input;
- user replies "合并" or clicks merge -> WT enters merge gate.

Messages outside the topic must not mutate this WT session.

## Shared Topic Continuation For Deep, Spec, WT

Deep, Spec, WT, and future engines should share one routing rule:

```text
If incoming message has thread_root_id and that thread is bound to an engine
session, route the message to that engine unless the message is an explicit
system/slash control command.
```

Precedence:

1. safety/system commands (`/exit`, `/stop_*`, lock commands, status commands);
2. explicit engine command in the message (`/spec`, `/deep`, `/wt`, etc.);
3. topic-bound engine session;
4. project active mode fallback;
5. smart routing/intent recognition.

This makes Deep/Spec/WT behavior consistent and gives future engines the same
continuation mechanism.

## WT Lifecycle

Each WT session is one task:

```text
NEW
  -> GOAL_READY
  -> TOOL_SELECTION
  -> PLANNING
  -> EXECUTING
  -> REVIEWING
  -> ITERATING
  -> READY_TO_MERGE
  -> MERGING
  -> COMPLETED
```

Failure and cancellation states:

```text
FAILED
CANCELLED
MERGE_CONFLICT
```

### Goal

The goal is required before execution. It can arrive from:

- `/wt <goal>`;
- goal-input card;
- ordinary topic reply when WT is waiting for a goal.

The goal becomes part of the WT session and is shown on every major card.

### Tool And Model Pool

The selected tools/models are the resource pool for the WT task. They should not
mean "global developers across all project tasks"; they only belong to this
topic's WT session.

The first version can keep the existing selection UI, but the card must say that
these tools will be used for this WT task's implementation/review loop.

### Planning

After goal and tool selection, WT creates a task plan:

- short goal restatement;
- acceptance criteria;
- estimated change scope;
- implementation strategy;
- risks;
- review role plan.

Estimated scope is advisory in v1. It can power warnings, but should not block
execution by default.

### Execution

WT creates isolated worktree units for the current WT session. Paths and branches
must include the WT session id or topic id so concurrent sessions in the same
project cannot collide.

Example branch naming:

```text
ghostap/wt/<short-session-id>/<unit-index>-<slug>
```

Execution can remain parallel inside the WT task when multiple selected tools
are used. That is separate from topic-level parallelism.

### Review Loop

Each WT task borrows Spec's adaptive review idea, but stays scoped to the WT
session.

The review loop should:

- derive roles from goal, plan, touched files, and diff;
- include software roles by default: architect, product, tester, integration;
- add dynamic roles when useful: security, performance, docs, mobile UX;
- require blocker findings to include evidence from diff, tests, logs, or
  acceptance criteria;
- continue iteration until blocking findings are resolved or budget is reached.

Unlike Spec, WT review must focus on actual code diff and merge readiness.

### Merge

Merge is project-serialized. Multiple WT sessions may execute concurrently, but
only one session may merge to the base branch at a time.

Merge gate checks:

- session is `READY_TO_MERGE`;
- review blockers are resolved or explicitly waived;
- tests required by the plan have passed or are explicitly skipped with reason;
- project merge lock is available;
- git merge/rebase can proceed.

Conflicts are handled at merge time. The user can resolve or ask WT to attempt a
conflict-fix cycle in the same topic.

## Overlap Policy

Global conflict control should remain light. Add a configuration point, but keep
v1 behavior simple.

Proposed setting:

```yaml
worktree:
  overlap_policy: warn
```

Supported semantics:

- `warn`: default. If estimated scope overlaps active WT sessions, show a warning
  but allow execution.
- `queue`: design placeholder. Future behavior can wait until overlapping WT
  sessions complete or release scope.
- `ignore`: skip overlap checks and rely only on merge-time conflict handling.

V1 should implement `warn` only if the scope index is cheap to add. If scope
tracking threatens delivery size, defer overlap warnings and keep merge-time
conflict handling as the only enforcement.

## Data Model Changes

Add an engine session abstraction:

```python
class EngineSessionRecord:
    session_id: str
    engine_type: str
    project_id: str
    chat_id: str
    thread_root_id: str
    status: str
    created_at: float
    updated_at: float
```

WT-specific state should live behind a WT session store:

```python
class WorktreeSessionStore:
    def get(session_key) -> WorktreeSessionState | None: ...
    def create(session_key, goal: str) -> WorktreeSessionState: ...
    def update(session_key, mutation) -> WorktreeSessionState: ...
    def list_project_sessions(project_id: str) -> list[WorktreeSessionState]: ...
```

In the first implementation this can be in-memory and attached to
`WorktreeManager`, but the API should not expose `ProjectContext.worktree_state`
as the owner. That preserves a path to persistence later.

## Routing Changes

Create a shared topic-engine resolver:

```python
class TopicEngineResolver:
    def resolve(message) -> TopicEngineContext | None: ...
    def bind(thread_root_id, project_id, chat_id, engine_type, session_id) -> None: ...
    def unbind(thread_root_id, engine_type=None) -> None: ...
```

Existing `ThreadContextManager` may already own most of this. The design should
prefer extending it instead of adding a second registry if the ownership fits.

The dispatcher should route by topic before project active mode fallback. This
is what makes "reply in the current topic continues the current WT/Deep/Spec"
work consistently.

## Card UX Requirements

WT cards should show:

- project;
- topic/session id short label;
- goal;
- stage;
- selected tool/model pool;
- current plan or active review summary;
- merge status;
- next required user action.

Important copy rule:

- Do not show "开始执行" before the goal exists.
- If the system is waiting for a goal, show "输入任务目标".
- If goal exists and tools are selected, then show "开始规划/执行".

The `/wt` entry card should avoid implying that selection alone is enough to
run a task.

## Migration Plan

1. Introduce shared topic-engine routing contract and tests for Deep/Spec/WT.
2. Change WT session ownership from `ProjectContext.worktree_state` to
   topic-scoped session store.
3. Update `/wt` flow so goal is required before execution and plain `/wt` shows
   goal input or current topic status.
4. Make WT worktree branch/path names session-scoped.
5. Add WT review loop adapter that reuses Spec adaptive review concepts.
6. Add project-level merge serialization tests for multiple WT sessions.
7. Add optional overlap warning only after session scoping is stable.

## Testing Strategy

Targeted tests:

- `/wt <goal>` inside a topic creates a WT session keyed by
  `project_id + chat_id + thread_root_id`;
- plain `/wt` inside an existing WT topic shows that WT status;
- plain text in a WT topic routes to WT, not project active mode;
- plain text in a Spec topic routes to Spec;
- plain text in a Deep topic routes to Deep;
- two topics in the same project can create independent WT sessions;
- two WT sessions do not overwrite selected tools, goal, units, or merge notes;
- branch/path names do not collide across sessions;
- merge actions are serialized by project;
- empty WT goal cannot start execution;
- topic-bound slash/system commands keep their existing priority.

Regression tests:

- existing single WT flow still works;
- existing `/wt <goal>` fast path still works, but now binds to topic session;
- ACP session keys still use `thread_id` correctly;
- existing Deep/Spec topic behavior does not regress.

## First-Version Decisions

- When `/wt <goal>` is sent outside a topic, GhostAP should create a topic-root
  card reply and bind the new WT session to that root message. The user should
  continue the task in that topic.
- The first implementation should not block or queue WT sessions based on
  estimated overlap. It may show overlap warnings if the session index already
  has enough data, but merge-time conflict handling is the only required
  enforcement.
- WT review should use a smaller WT-specific review adapter around shared Spec
  review components. Do not couple WT directly to the full Spec engine lifecycle.
