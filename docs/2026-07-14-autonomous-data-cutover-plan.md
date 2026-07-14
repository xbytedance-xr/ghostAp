# Autonomous Employee Data Cutover Plan

Date: 2026-07-14

## Objective

Finish `docs/goals.md` Phase 7 by making Journal + encrypted Blob storage the
only production data authority for canonical employees while preserving legacy
Slock virtual-role behavior.

## Verified starting point

- The employee Gateway already commits one terminal history record atomically
  with the terminal attempt and Router disposition.
- The Gateway calls `SlockEngine.run_agent_session()` directly, so the normal
  employee path does not execute the legacy `_execute_agent()` history/memory
  mutation block.
- A canonical `employee_v1` identity can still be passed to legacy execution
  entry points unless those paths reject it explicitly.
- Data authority projection exists, but production writes do not enforce it.
- History ACL currently treats any non-empty `chat_id` as membership and has an
  invalid row comparison between `chat_id` and `principal_id`.
- There is no typed ACL-gated memory query facade or durable employee Channel
  `/history` and `/memory` control handling.
- Runtime recovery rebuilds the data projection but not all verified history and
  document materializations.

## Implementation sequence

1. Add failing regression tests for canonical/legacy writer isolation, trusted
   membership ACL, memory full-versus-summary rules, authority fencing, durable
   controls, and restart materialization rebuild.
2. Add an independent `EmployeeDataAuthorityGuard` and one-way canonical
   cutover event. Require canonical authority immediately before every data
   mutation; reject stale epochs.
3. Prevent `employee_v1` identities from entering legacy Slock mutation paths.
   Keep legacy identities unchanged.
4. Replace boolean-like ACL inference with a trusted membership resolver. Add a
   typed history read facade and a memory read facade that authorizes before any
   materialized file or encrypted Blob read.
5. Handle exact `/history` and `/memory` only after durable employee Inbox ACK,
   before Router admission, and deliver replies through the employee Outbox.
6. Expose the same authoritative read service to main-Bot admin-DM handlers
   without changing legacy `/memory` behavior.
7. Make runtime recovery validate live encrypted Blobs and rebuild both history
   and canonical employee documents before declaring the data plane ready.
8. Run focused tests, grill the diff, run the full Autonomous and shared Slock/
   WS suites, update `docs/goals.md` and `.Memory`, commit, and push `dev`.

## Safety contracts

- Authorization derives only from transport-authenticated principal, tenant,
  receiving app, chat type, and workforce projection membership.
- Full L1 is visible only to an administrator in the main-Bot P2P chat.
- Employee Channel `/memory` returns only chat/thread-scoped summaries.
- Read authorization happens before plaintext materialization or Blob access.
- Missing or stale data authority fails closed; it never falls back to legacy
  files for a canonical employee.
- Restart failure on missing/tampered live Blob or materialization blocks the
  employee data runtime.
