# Autonomous Phase 5: Membership and Durable Stop Plan

## Scope

Close `docs/goals.md` Phase 5 without enabling visible employees:

- Journal-backed team membership for canonical `employee_v1` employees.
- Production `/role add <employee>` and `/role remove <employee>` through the
  main Manager Bot and official Feishu group-member APIs.
- Reconciliation of add/remove results with the target employee Bot's own
  `is_in_chat` authority.
- Durable `/stop` command handling outside the blocked ACP dispatch loop.
- A single terminal winner when cancel and ACP completion race.

Legacy virtual Slock roles remain on the existing registry path. Canonical
employees never fall back to legacy membership writes.

## Official Feishu Contract

- Add and remove use
  `POST/DELETE /open-apis/im/v1/chats/:chat_id/members` with
  `member_id_type=app_id` and the employee application's App ID.
- Mutations for one chat are serialized because Feishu documents concurrent
  group-member mutations as liable to return `232019`.
- The normal member-list API cannot verify Bot membership because it filters
  bots. Reconciliation therefore uses the target employee credential with
  `GET /open-apis/im/v1/chats/:chat_id/members/is_in_chat`.
- Main Bot permission or presence failures are explicit failures and never
  become local membership success.

## Membership Model

Add an `autonomous.membership` bounded context with immutable records and a
replay projection keyed by `(tenant_key, chat_id, agent_id)`.

Membership states:

```text
ABSENT -> ADDING -> ACTIVE
ACTIVE -> REMOVING -> ABSENT
ADDING/REMOVING -> DEGRADED
```

Each external mutation uses a stable effect identity and monotonic states:

```text
PREPARED -> EXECUTING -> COMMITTED
                      -> ACTION_REQUIRED
```

`COMMITTED` and `employee.membership_changed` are anchored in the same Journal
frame. `ACTION_REQUIRED` marks the membership `DEGRADED`; Router health denies
new work until reconciliation proves the remote state. A successful remove
closes ingress/outbox authority immediately through the projected member list.

The service owns a per-chat lock. It validates tenant, ACTIVE visible employee,
principal/App ID binding, requester admin/team-owner authority, and activated
Slock chat before preparing an effect.

## Handler Cutover

- Inject the runtime membership facade into `HandlerContext`.
- `/role add` checks permission before listing employees or mutating state.
- Canonical employees are resolved from the projected registry and use the
  durable membership service.
- `/role remove` removes only the current chat membership. It never deletes the
  global employee or its credential.
- Legacy roles continue to use the existing registry behavior.
- Known remote rejection, unknown outcome, and missing service return distinct
  fail-closed messages; no optimistic success card is emitted.

## Durable Stop Model

Add attempt events:

```text
employee.execution_attempt.cancel_requested
employee.execution_attempt.terminal
```

The cancel request contains the stable command acceptance, requester, and a
monotonic cancel epoch. It is anchored before any session cancellation.

The employee Channel parent emits an internal `durableIngressAccepted`
notification only after Inbox anchoring and ACK publication. Runtime inspects
the authenticated encrypted payload. Exact `/stop` commands enter a dedicated
control path instead of the normal Router queue, so a blocked ACP call cannot
starve cancellation.

Authorization is rechecked against:

- configured department administrators;
- current Slock team owner; or
- the original attempt requester.

Gateway permit state is serialized with cancellation:

- if the permit has not called Slock, cancel revokes it and ACP is never run;
- if it is running, cancel invokes the existing Slock agent/session stop path;
- if terminal already committed, `/stop` reports already terminal;
- if cancel anchors first, terminal finalization is forced to `CANCELED`, even
  if a late ACP result says completed or failed;
- `ACTION_REQUIRED` is terminal and cannot be rewritten by `/stop`.

The stop command Inbox record receives a durable disposition. User-visible
command outcomes are appended to the employee Durable Outbox; no main-Bot send
fallback is added.

## Recovery

- Membership projection rebuilds entirely from Journal.
- `EXECUTING` membership effects are reconciled with employee `is_in_chat`;
  confirmed desired state commits, otherwise remains degraded. A recovered
  `PREPARED` effect never replays or claims an external call and moves directly
  to `ACTION_REQUIRED`.
- A replayed cancel request without terminal state is finalized as canceled
  without rerunning ACP.
- Existing crash semantics remain: a dispatch-committed attempt without cancel
  or terminal is `ACTION_REQUIRED`, never automatically rerun.

## Test Order

1. Membership model/projection exact-schema and monotonic tests.
2. Lark adapter contract tests for `app_id`, strict responses, and employee
   `is_in_chat` verification.
3. Membership service tests for authorization, serialization, unknown results,
   replay, drift, add/remove idempotency, and remove authority closure.
4. Handler tests proving canonical/legacy separation and no global deletion.
5. Gateway stop tests for pre-call cancel, in-flight cancel, terminal-first,
   cancel-first, duplicate stop, unauthorized stop, and ACTION_REQUIRED.
6. Runtime ingress-control integration and restart tests.
7. Autonomous suite, shared Slock/WS regressions, Ruff, validation, and diff
   checks.

## Release Constraint

Phase 5 completion does not change `autonomous_visible_employee_limit=0`.
Production release still depends on Phases 6-9 and real-tenant evidence.
