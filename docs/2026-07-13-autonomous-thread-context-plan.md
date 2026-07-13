# Autonomous Employee Thread Context Production Plan

> **Execution:** implement task-by-task with test-driven development and an
> independent spec/quality review before each task commit. The overall Agent
> Department goal remains active after this plan completes.

**Goal:** Replace the in-memory Thread Context scaffold with an
employee-scoped, official `lark-oapi` message source and a deterministic,
fail-closed context snapshot that is ready for the durable employee router.

**Production boundary:** `EmployeeDepartmentRuntime` remains the sole visible
employee composition owner. This phase prepares and wires Context before task
execution, but does not claim that the Phase 3 durable Inbox, execution attempt
Journal protocol, or `_run_acp_session` gateway is complete.

**Authoritative context order:** full Thread > recent group messages > L1 > L2.
When the budget is exceeded, remove data in the inverse importance order:
L2, then L1, then recent group messages, then the oldest unprotected Thread
messages. System constraints and the current user message are never trimmed.

## Evidence and API decisions

- Feishu's history API requires `container_id_type="thread"` together with a
  Feishu `thread_id` (`omt_...`). The local/root message ID (`om_...`) is a
  different identifier and must never be passed as that container.
- Prefer the `thread_id` captured from `im.message.receive_v1`. If it is absent,
  call Get Message for the root/current message and require exactly one result
  whose message, chat, root, and thread binding match the inbound scope.
- Fetch Thread pages in explicit `ByCreateTimeAsc` order. Fetch recent group
  messages in `ByCreateTimeDesc`, then restore deterministic ascending order
  before assembly. A page token must be present and advance whenever
  `has_more=True`.
- The SDK message model exposes `create_time`, `update_time`, `updated`,
  `deleted`, `message_position`, and `thread_message_position`. These fields,
  not arrival order, define revision and deterministic ordering.
- Get Message may reject deleted content. A deleted current message makes the
  snapshot unavailable; deleted historical messages remain tombstones and
  never expose stale body text.
- Official API references:
  - https://open.feishu.cn/document/server-docs/im-v1/message/get
  - https://open.larksuite.com/document/server-docs/im-v1/message/list
  - https://open.feishu.cn/document/im-v1/message/thread-introduction

## Non-negotiable safety rules

- Every message read uses the target employee application's credentials from
  `BotPrincipal + CredentialVault`. The Manager Bot client is never a fallback.
- `app_secret` may exist only inside the Vault resolution/client construction
  boundary. It must not enter logs, exceptions, Journal, identity files,
  command arguments, environment variables, cards, or ordinary IPC.
- Thread pagination, root/thread binding, current-message presence, ordering,
  revision, or content parsing uncertainty produces a stable
  `CONTEXT_UNAVAILABLE` result and zero task execution calls.
- Group recent messages are a lower-priority enrichment layer, but a configured
  group API read failure still produces `CONTEXT_UNAVAILABLE`; it is never
  silently presented as an empty/complete group layer and never authorizes
  fallback from a failed Thread. A successful empty result remains valid.
- Full Thread means full within the API-visible snapshot. If configured safety
  caps are reached while `has_more=True`, fail closed; never label a truncated
  fetch as `THREAD_FULL`.
- Domain objects remain frozen. No database, main WebSocket changes, engine
  changes, or `_run_acp_session` semantic changes are permitted in this phase.

## Target structure

- `src/autonomous/context/models.py`
  - frozen scope, message revision, resolved Thread, watermark, layer metrics,
    assembled snapshot, and stable unavailable reason codes.
- `src/autonomous/context/source.py`
  - employee-scoped source/factory protocols and strict page contracts.
- `src/autonomous/context/lark_source.py`
  - official SDK Get/List adapter, employee client factory, normalization,
    content parsing, response validation, and secret-safe errors.
- `src/autonomous/context/assembler.py`
  - pagination orchestration, snapshot boundary checks, deduplication,
    protected-message handling, and deterministic budget trimming.
- `src/autonomous/context/service.py`
  - registry/Vault/message-source/memory composition for one inbound message.
- `src/autonomous/context/__init__.py`
  - compatibility-preserving public exports only.
- `src/autonomous/provisioning/router.py`
  - consume only an immutable, authority-bound execution request in Phase 3;
    raw Channel payloads must not be allowed to manufacture this authority.
- `src/autonomous/provisioning/composition.py`
  - own the Context service/source factory and include its capability in
    readiness without weakening existing release gates.
- `src/config/settings.py`, `.env.example`
  - strict Thread/group/message/character/token/deadline/page settings.

---

## Task 1: Frozen contracts and strict configuration

**Tests first**

- Add `tests/autonomous/unit/test_employee_context_models.py`.
- Extend `tests/autonomous/contract/test_config_and_gate_contract.py`.
- Extend the `.env.example` coverage test if one exists; otherwise add a narrow
  contract test that parses the documented setting names.

**Required contracts**

- `EmployeeMessageScope` includes `tenant_key`, `agent_id`, `bot_principal_id`,
  `app_id`, `credential_ref`, `chat_id`, `thread_root_message_id`, optional
  `feishu_thread_id`, and `current_message_id`; all required identifiers reject
  blanks and inconsistent prefixes.
- `ContextMessage` includes message/chat/thread/root identity, sender ID type
  and tenant, create/update milliseconds, positions, content type, tombstone,
  `is_system`, and `is_current`.
- `MessageRevision` and `ThreadWatermark` include a deterministic digest over
  identity, create/update time, edit/delete state, and Thread position. The
  watermark represents the fetched source snapshot before budget trimming.
- `ContextUnavailableReason` is stable and machine-readable. At minimum cover
  scope, credentials, permission/visibility, root/thread binding, pagination,
  ordering, revision, current-message, content, deadline, and budget failures.
- `AssembledContext` records per-layer source/retained message and character
  counts, omission reason, trimming trace, and whether the group layer was
  unavailable. It never logs or serializes message plaintext as diagnostics.
- `ThreadContextConfig` is frozen and built from Settings.

**Settings**

- `autonomous_thread_context_max_messages`
- `autonomous_thread_context_max_chars`
- `autonomous_group_context_max_messages`
- `autonomous_context_max_tokens`
- `autonomous_thread_context_page_size` (1..50)
- `autonomous_group_context_page_size` (1..50)
- `autonomous_context_fetch_timeout_seconds`
- `autonomous_context_max_pages`

Defaults must preserve the design values where already specified, while
validators reject zero/negative budgets, page sizes outside the official API
range, non-finite ratios/timeouts, and combinations where protected content
cannot be represented.

**Verification**

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/autonomous/unit/test_employee_context_models.py \
  tests/autonomous/contract/test_config_and_gate_contract.py -q
uv --cache-dir /tmp/ghostap-uv-cache run ruff check \
  src/autonomous/context src/config/settings.py
```

Commit: `feat(autonomous): define thread context contracts`

---

## Task 2: Official employee-scoped Feishu message source

**Tests first**

- Add `tests/autonomous/contract/test_lark_thread_message_source.py` with an
  injected fake SDK client; no real tenant call is part of unit/contract tests.
- Add `tests/autonomous/security/test_employee_context_credentials.py` proving
  two employees use distinct app credentials and the secret is absent from
  errors, representations, logs, request metadata, argv, and environment.

**Implementation requirements**

- Build a fresh employee SDK client from `BotPrincipal` and
  `CredentialVault.resolve(credential_ref, agent_id, app_id)` with an explicit
  request timeout. Do not accept the Manager Bot client in the production
  factory type.
- Root resolution uses `GetMessageRequest`. Require success, non-null data, and
  exactly one matching item. Validate `message_id`, `chat_id`, `root_id`, and
  `thread_id`; persist both root message ID and Feishu Thread ID in the result.
- Thread List uses `container_id_type="thread"`, the resolved `omt_...` ID,
  explicit ascending order, bounded page size, and unchanged sort options.
- Group List uses `container_id_type="chat"`, the expected `oc_...` chat ID,
  explicit descending order, and bounded page size.
- Strictly validate SDK success, response shape, page-token progress, deadline,
  message identity/scope, integer millisecond timestamps, update >= create,
  and deterministic ordering. Reject duplicate IDs with conflicting revision
  or body.
- Parse supported content deterministically. Media may use stable non-secret
  placeholders; malformed/unknown non-deleted content fails closed. Tombstones
  contain no historical body.
- Map platform errors to stable reason codes without including SDK response
  text that may contain content or credentials.

**Verification**

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/autonomous/contract/test_lark_thread_message_source.py \
  tests/autonomous/security/test_employee_context_credentials.py -q
uv --cache-dir /tmp/ghostap-uv-cache run ruff check \
  src/autonomous/context tests/autonomous/contract/test_lark_thread_message_source.py
```

Commit: `feat(autonomous): add employee lark context source`

---

## Task 3: Deterministic snapshot assembly and budget policy

**Tests first**

- Replace the permissive scaffold cases in
  `tests/autonomous/unit/test_employee_thread_context.py` with explicit snapshot
  tests; keep compatibility tests only for public imports that remain valid.
- Add property-style parameterized cases for page/token/order/revision and
  budget boundaries without introducing an unpinned dependency.

**Required behavior**

- Capture a source boundary and assemble an immutable snapshot. Messages newer
  than the current triggering message belong to the next snapshot. Since the
  Thread API offers no transactional snapshot parameter, perform bounded
  repeated reads through that boundary and require matching revision digests;
  if stability cannot be demonstrated, return `CONTEXT_UNAVAILABLE`.
- Deduplicate within and across pages/layers. Same ID + same revision is one
  message; same ID + newer revision replaces the older version; conflicting
  equal revisions fail closed.
- Require the current message exactly once and not deleted. Mark it protected.
  Preserve protected system constraints separately from ordinary group/system
  chatter so they can never be trimmed.
- Record historical deletion tombstones without stale text. Edited messages use
  the latest API body while retaining create-order and revision metadata.
- Apply both character and token budgets. Trim whole units deterministically in
  this exact order: L2, L1, oldest group recent, oldest unprotected Thread.
  Protected content exceeding the hard budget is an explicit budget failure;
  it is never silently truncated.
- Recompute layers and metrics after trimming. The watermark remains the
  pre-trim source watermark.

**Verification**

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/autonomous/unit/test_employee_context_models.py \
  tests/autonomous/unit/test_employee_thread_context.py -q
uv --cache-dir /tmp/ghostap-uv-cache run ruff check src/autonomous/context
```

Commit: `feat(autonomous): enforce thread context snapshots`

---

## Task 4: Memory service and authorized pre-execution contract

**Tests first**

- Add `tests/autonomous/integration/test_employee_context_service.py` for
  Projected Registry/BotPrincipal/Vault/source/memory binding.
- Add a narrow `ContextPreparingExecutionPort` contract test proving Context
  success is required before the delegated execution port and every context
  failure produces zero delegated calls.
- Add `tests/autonomous/security/test_employee_context_acl.py` for tenant-bound
  L1 and membership/chat-bound L2 reads.

**Implementation requirements**

- Resolve employee and BotPrincipal from the Journal-backed projected registry;
  validate tenant, agent, app, credential ref, and chat membership before any
  message API call.
- Fix the canonical L1 read boundary so `tenant_key` is actually checked against
  projected document ownership before file access. Resolve the currently
  unreachable canonical/legacy conflict behavior explicitly rather than
  silently preferring a conflicting file.
- Read L2 through an ACL adapter around the existing Slock group-memory port,
  requiring tenant + employee membership + chat binding before file access.
  A chat/thread memory summary is not a substitute for full L2.
- Introduce `AuthorizedContextRequest`, which contains the frozen authority
  binding produced by Phase 3: tenant, agent, bot principal/app/generation,
  chat, root/thread/current message, and requester principal. Raw Channel event
  payloads cannot construct a trusted request by themselves.
- Introduce an immutable `EmployeeExecutionInput` containing the authorized
  request, selected tool/model/effort, and assembled Context snapshot. A
  `ContextPreparingExecutionPort` assembles exactly once and delegates only on
  success; it preserves typed `ContextUnavailableError` and makes zero delegate
  calls on failure.
- Do not wire ordinary ACTIVE Channel events into the current in-memory
  `EmployeeMessageRouter` during this task. It lacks durable event identity,
  app/generation/lifecycle binding, sender ACL, and an authority snapshot, so
  doing so would create an unsafe production bypass.
- Do not add the durable Inbox, execution-attempt anchoring, or real
  `_run_acp_session` implementation here. Those are Phase 3 tasks and must not
  be simulated with in-memory success flags.

**Verification**

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/autonomous/integration/test_employee_context_service.py \
  tests/autonomous/unit/test_router_and_response.py -q
uv --cache-dir /tmp/ghostap-uv-cache run ruff check \
  src/autonomous/context src/autonomous/provisioning/router.py
```

Commit: `feat(autonomous): gate employee execution on context`

---

## Task 5: Production composition, readiness, and recovery ownership

**Tests first**

- Extend `tests/autonomous/integration/test_employee_hire_composition.py` with
  context source/service construction, capability probes, reverse shutdown,
  restart, credential rotation, and fire cleanup cases.
- Extend the release manifest contract tests with explicit Thread Context
  assertions; do not mark them passed without evidence.

**Implementation requirements**

- `EmployeeDepartmentRuntime.from_settings()` constructs and owns the Context
  source factory/service after Journal and Vault are available. It exposes the
  service to the Phase 3 ingress composition without exposing the Vault secret.
- Readiness includes an employee-scoped Context capability/probe and reports a
  stable blocker when unavailable. Existing visible-limit, release evidence,
  anchor, sandbox, notifier, and main-Bot audit blockers remain intact.
- Shutdown order is admission/ingress, in-flight context work, employee context
  clients, employee channels, service/writer/Vault. No source call may outlive
  Vault closure.
- Recovery rebuilds bindings from Journal projections. Credential rotation or
  retirement invalidates cached employee clients; there is no shared-client
  fallback.
- Add explicit release-manifest gates for full pagination, root/thread binding,
  revision/edit/delete, deterministic trimming, context failure zero-dispatch,
  and main-Bot zero-send.

**Verification**

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/autonomous/integration/test_employee_hire_composition.py \
  tests/autonomous/contract/test_employee_release_manifest.py -q
uv --cache-dir /tmp/ghostap-uv-cache run ruff check \
  src/autonomous/context src/autonomous/provisioning src/config/settings.py
```

Commit: `feat(autonomous): compose employee thread context`

---

## Task 6: Failure injection, regression, and phase handoff

**Tests first**

- Add chaos cases for timeout, repeated token, new message during paging,
  edit/delete during paging, partial SDK response, restart, credential rotation,
  and source shutdown races.
- Add security checks for cross-tenant/chat/thread returns, Manager Bot client
  fallback, secret redaction, symlinked materialized memory, and oversized
  protected content.

**Required evidence**

- `CONTEXT_UNAVAILABLE` produces no task/ACP execution for every mandatory
  Thread failure mode.
- Full Thread, group dedup, watermark/revision, edit/delete, and budget order
  remain deterministic across replay/restart.
- Existing main Bot WS and Deep/Spec/Worktree/Workflow routing are unchanged.
- Update `.Memory/2026-07-13.md`, `.Memory/Abstract.md`, `docs/goals.md`, and the
  local SDD progress ledger with exact commits/tests and remaining Phase 3-9
  blockers. Do not raise `autonomous_visible_employee_limit`.

**Final phase verification**

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest tests/autonomous/ -q
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/test_slock_role_creation.py \
  tests/test_ws_client_routing.py \
  tests/test_contextvar_propagation.py \
  tests/test_project.py \
  tests/test_docs_references.py -q
uv --cache-dir /tmp/ghostap-uv-cache run ruff check src/autonomous/
uv --cache-dir /tmp/ghostap-uv-cache run python -m src.main --validate
git diff --check
```

Commit: `test(autonomous): close thread context phase`

## Phase completion boundary

This plan is complete only when Tasks 1-6 have fresh passing evidence and are
pushed to `dev`. It proves the production Thread Context dependency and
pre-execution gate. It does **not** prove durable employee ingress, the real
Slock gateway, employee-owned response cards, team/stop/fire semantics, data
producer cutover, external release trust, or real-tenant acceptance. Those
remain active in `docs/goals.md` Phases 3-9.
