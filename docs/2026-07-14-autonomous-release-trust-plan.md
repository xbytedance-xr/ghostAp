# Autonomous Phase 8 Release Trust Plan

## Outcome

Phase 8 replaces the permanent config-only release rejection with a fail-closed
production trust path. It does not raise `autonomous_visible_employee_limit` and
does not manufacture tenant evidence. Runtime admission becomes possible only
when an external deployment authority consumes a fresh, complete employee
release attestation for the running workload.

## Frozen security decisions

1. `.env`, local evidence files, and application-owned public keys cannot grant
   release authority. They are claims, not trust roots.
2. The production trust boundary is a root-owned Unix domain socket broker. The
   client rejects symlinks, non-sockets, non-root ownership, writable socket
   modes, and non-root `SO_PEERCRED` peers.
3. The broker owns independent-QA signature verification, immutable
   build/workload provenance, monotonic attestation consumption, external
   witness anchoring, and renewable recovery capability. Every response is
   nonce-, binding-, checkpoint-, workload-, ledger-, witness-, and time-bound.
4. A short-lived release lease is renewed before expiry. Renewal failure is
   tolerated only while the current lease remains valid; expiry closes new hire,
   ingress, context, and outbox admission and requires a clean restart after the
   external authority is restored.
5. Main-Bot zero-send evidence is captured at the shared Feishu IM transport
   boundary, before create/reply/patch dispatch. The audit contains only safe
   metadata and uses an independent HMAC Journal plus monotonic anchor.
   Unknown-tenant sends are counted conservatively for every tenant window.
6. Phase 9 remains the owner of real staging/production execution and 1/10/50
   Bot soak. No local fake may satisfy those gates.

## TDD tasks

### Task 1: external release broker contract

- Add strict request/lease models and a root-owned Unix socket client.
- Test socket type/ownership/mode/peer UID, framing limits, nonce replay,
  binding/checkpoint mismatch, expired leases, non-monotonic ledgers and
  recovery renewal.
- Add a runtime evaluator that first validates the local evidence bundle against
  the attested checkpoint, then asks the broker to consume the attestation.

### Task 2: runtime lease lifecycle

- Inject a release trust provider into production composition; keep the no-
  provider path closed.
- Retain the lease/session in the runtime, renew it from the existing monitor,
  and close all new admission when the capability expires.
- Preserve restart recovery only while an externally authorized lease is valid.

### Task 3: main-Bot outbound audit

- Add a dedicated Journal-backed audit service with an independent anchor.
- Inject it into every `BaseHandler` `FeishuIMClient` and record create, reply,
  file reply, and patch attempts before network dispatch.
- Compose the audit before the employee runtime and inject its verified query
  port into activation verification.

### Task 4: verification and handoff

- Run release/audit contract tests, employee composition and WS routing tests,
  all Autonomous tests, shared Feishu handler tests, Ruff, config validation,
  docs references and `git diff --check`.
- Apply repeated `grill-me` review findings, update `docs/goals.md` and project
  Memory, commit with the repository convention, and push `dev`.
- Inspect the live environment for an authorized broker, tenant evidence, and
  host sandbox support. If absent, leave visibility at zero and record only the
  exact Phase 9 external blockers.

## Implemented broker protocol

The client uses one JSON object plus newline per root-authenticated Unix socket
connection. Every request includes protocol version 1, a fresh 256-bit nonce,
the caller PID, lease/binding/checkpoint coordinates where applicable, and an
exact operation schema. The broker must compare the request PID with its own
peer credentials and reject a workload that does not match immutable deployment
provenance.

Implemented operations are:

- `consume_release_attestation`: independently verify the QA signature and
  trust root, bind the evidence checkpoint to the running workload, consume it
  once in the external monotonic ledger, and return a short release lease.
- `renew_recovery_capability`: renew the same lease lineage while advancing the
  external witness; changing workload, checkpoint, consumption ID, or ledger
  sequence is rejected.
- `read_journal_anchor` / `compare_and_swap_journal_anchor`: provide monotonic
  CAS for the employee Journal and main-Bot audit scopes. Witness sequence is
  globally monotonic across scopes, not merely monotonic within one file.
- `record_main_bot_send_attempt`: append safe tenant/target hashes and operation
  metadata to the broker's cross-replica audit before the SDK network call.
- `count_main_bot_send_attempts`: return a complete cross-replica count and
  current audit/witness sequence; incomplete or regressed results fail closed.

The application never sends message contents, raw chat/message IDs, app secrets,
tokens, Vault references, or signing keys to the broker.

## Current result

The application-side Phase 8 contract is complete and fail-closed. Full
Autonomous validation is `1811 passed, 2 skipped, 1 warning`; the two skips are
the opt-in real-tenant selector and unavailable host user namespaces. The live
workspace has no configured broker socket, evidence bundle, release attestation,
complete release binding, real-tenant opt-in, or verified worker sandbox.
Consequently Phase 9 remains externally blocked and
`autonomous_visible_employee_limit` remains zero.
