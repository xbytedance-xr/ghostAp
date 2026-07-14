# Autonomous Employee Response Channel Plan

> **Status:** complete in local code and automated verification as of
> 2026-07-14. This plan does not authorize raising
> `autonomous_visible_employee_limit` above `0`.

**Goal:** Persist every employee task status snapshot before delivery, create
exactly one employee-owned main card with a stable UUID, update that same card
through the employee child, and recover pending delivery after restart without
ever falling back to the Manager Bot.

**Non-goals:** This phase does not implement team membership commands, `/stop`,
`/fire`, canonical data producer cutover, external release trust, or real-tenant
acceptance.

## Chosen delivery backend

The pinned `lark-channel-sdk==1.1.0` exposes a public
`CardStreamController`, but its `ensure_created` callback always creates the
message and it has no supported pre-bound message-ID constructor. Calling
`channel.stream()` would therefore bypass the stable UUID create contract.

Use the verified public employee-child APIs instead:

1. Parent anchors an Outbox create intent with a stable UUID derived from the
   `outbox_id`.
2. Parent asks the exact READY employee generation to call
   `channel.send(target, {"card": snapshot}, {"uuid": stable_uuid, ...})`.
3. Parent anchors the returned app/generation/connection/message binding.
4. Later snapshots are patched by that same child with the public
   `channel.update_card(message_id, snapshot)` API over a new bounded IPC frame.

The parent never imports SDK-private streaming helpers. The child owns all
employee network calls. Main-Bot delivery is not a fallback path.

## Frozen invariants

- Journal is the lifecycle SSOT. Card snapshots live in an employee-keyed
  encrypted BlobStore; Journal stores only hashes, versions, safe coordinates,
  and authenticated Blob references.
- Domain records are frozen. One `(tenant, agent, task/attempt)` has one stable
  `outbox_id`; the create UUID is a deterministic UUIDv5 of that ID.
- Snapshot versions increase by exactly one. States are
  `queued -> running -> completed|failed|canceled|action_required`.
- Terminal version is immutable. Late or duplicate progress after terminal is
  rejected before Blob publication and Journal commit.
- Create and patch are external Effects. PREPARED and EXECUTING must be
  anchored before the child call. A stable UUID makes create safe to retry; an
  unknown patch remains retryable because it replaces the same message with the
  same immutable snapshot version.
- The bound app ID, employee generation, connection ID, message ID, target chat,
  and reply coordinates must match the current projected employee authority.
  Stale generations cannot deliver or overwrite.
- Delivery failure never changes the execution terminal state. It leaves a
  pending/retryable Outbox item with secret-free error code and attempt time.
- Every employee response uses the employee child. Main-Bot send audit count
  must remain zero; no generic CardSession notification fallback is injected.
- Recovery replays Outbox and resumes the oldest pending create/patch per
  employee. At most one delivery is in flight per employee; different employees
  may progress independently.
- Shutdown stops Outbox admission, drains delivery workers, then closes employee
  Channels and the Outbox BlobStore before Journal/Vault. If delivery cannot
  drain within grace, dependent resources remain open.

## Task 0: Contracts, UI preview, and capability tests

**Tests first**

- Replace mutable `OutboxEntry` with frozen exact-schema models for snapshot,
  binding, effect, and projection records.
- Add reducers proving monotonic snapshot versions, terminal fencing, stable
  UUIDv5 identity, exact employee coordinates, and secret-field rejection.
- Add `ux/employee-response-card.html` showing queued, running, completed,
  failed, canceled, and action-required states at desktop and narrow widths.
- Extend the real employee Channel process tests: stable UUID reaches
  `channel.send`; a new UPDATE frame invokes public `channel.update_card` on the
  pre-bound message; app/generation/connection receipt mismatches fail closed.

## Task 1: Journal-backed encrypted Durable Outbox

**Tests first**

- Implement encrypted snapshot publication before `outbox.snapshot_appended`.
- Cover concurrent duplicate enqueue, restart replay, missing/corrupt Blob,
  orphan quarantine, terminal late-progress rejection, and tombstone GC.
- Keep task terminal facts and delivery facts separate. Outbox failure cannot
  rewrite `completed`, `failed`, `canceled`, or `action_required`.

## Task 2: Employee child delivery effects

**Tests first**

- Anchor create PREPARED/EXECUTING before `send(... uuid ...)`; validate receipt
  before binding `message_id`.
- Anchor patch PREPARED/EXECUTING before UPDATE IPC; bind exact snapshot version
  and digest. Commit delivery only after an exact employee receipt.
- Retry create with the same UUID and patch with the same message/version after
  timeout, parent restart, child crash, reconnect, and unknown response.
- Reject stale generation, wrong app/message/connection, malformed SDK result,
  main-Bot port injection, and secret-bearing errors.

## Task 3: Runtime composition and execution integration

**Tests first**

- `EmployeeDepartmentRuntime` owns Outbox storage, delivery coordinator, and
  bounded worker after Inbox/attempt recovery.
- `EmployeeDispatchCoordinator.finalize_attempt()` appends a terminal card
  snapshot only after atomic execution history/terminal commit; queued/running
  snapshots use explicit gateway lifecycle hooks and never fabricate ACP token
  deltas.
- Recovery delivers pending terminal cards without rerunning ACP. Shutdown order
  includes Outbox admission/worker before Channels/data/Journal/Vault.
- Execution readiness requires Outbox storage and employee update capability.

## Task 4: Evidence, chaos, and handoff

- Freeze exact selectors for stable UUID create, one-card binding, employee
  patch identity, terminal fencing, restart eventual delivery, and Manager Bot
  send-count zero.
- Kill at every Blob/Journal/effect/IPC/receipt boundary. Prove one logical card,
  monotonic terminal snapshots, no cross-employee delivery, and no secret leaks.
- Run focused tests, all Autonomous tests, shared routing regressions, Ruff,
  config validation, docs references, and `git diff --check`.
- Keep evidence local/non-promotable. Real tenant identity, 60-second delivery
  SLO, desktop/mobile rendering, and external build trust remain later release
  gates.

## Completion boundary

Phase 4 is complete only when the runtime owns and recovers the Durable Outbox,
all six status states render through exactly one employee-owned card, terminal
fencing and main-Bot-zero-send tests pass, and all review findings are closed.
It still does not prove team/stop/fire, data producer cutover, external release
trust, or real-tenant acceptance.

## Completion evidence

- `src/autonomous/outbox/` now owns frozen exact snapshot/binding/effect
  contracts, encrypted Blob publication, Journal projection/replay,
  terminal fencing, stable UUIDv5 create identity, effect recovery, and safe
  superseded-snapshot GC.
- The employee child IPC exposes bounded `UPDATE_CARD`; the worker calls only
  the public `FeishuChannel.update_card(message_id, card)` method. Parent-side
  receipts are fenced by app, generation, connection, and pre-bound message.
- `EmployeeDispatchCoordinator` appends queued/running/terminal snapshots via
  an explicit lifecycle port. Terminal notification runs only after the atomic
  history + attempt terminal + Router terminal frame has anchored. Recovery
  reconciles the post-anchor/pre-Outbox crash window from encrypted history
  without rerunning ACP.
- `EmployeeDepartmentRuntime` owns Outbox storage, delivery, recovery, worker,
  readiness, and shutdown order. The delivery coordinator has no main-Bot
  port or fallback path. The obsolete in-memory provisioning response module
  was removed.
- Repeated `grill-me` reviews closed secret aliases, mutable creation time,
  projection cloning, wrong-message receipts, reconnect rebinding, active
  effect overtaking, and pre-delivery GC.
- Verification: focused Phase 4/affected set `212 passed`, final data/recovery
  set `133 passed`; fresh Autonomous
  `1723 passed, 2 skipped, 1 warning in 397.51s`; shared routing/WS/doc set
  `193 passed`; Autonomous Ruff, configuration validation, and diff check all
  pass. The two skips remain external real-tenant/sandbox gates.
