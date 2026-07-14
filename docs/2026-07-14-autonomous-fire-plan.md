# Autonomous `/fire` Phase 6 Implementation Plan

## Goal

Replace the legacy in-memory `FireSaga` production path with a Journal-backed,
fail-closed employee retirement workflow.

## Safety contract

- `/fire` is accepted only from a configured administrator in the main Bot DM.
- Admission commits `employee.state_changed=retiring` and
  `employee.ingress.closed` before any cleanup is dispatched.
- Cleanup is one-way: retirement may remove memberships but can never add one.
- Every external or destructive action is anchored as PREPARED and EXECUTING
  before dispatch, then verified before COMMITTED.
- Unknown outcomes remain RETIRING/ACTION_REQUIRED and are never reported as
  success or blindly replayed.
- Vault destruction and archive happen only after execution, Slash, Channel and
  membership cleanup are disposed.
- The archive manifest records hashes, cleanup evidence and
  `external_app_disposition=manual_deletion_required`. GhostAP never claims it
  deleted the Open Platform application.

## Work items

1. Add replayable per-employee ingress closure.
2. Add immutable fire request/state/effect projection.
3. Implement the anchored retirement service and recovery rules.
4. Add verified Slash cleanup, Channel stop, membership remove-only cleanup,
   credential destruction and atomic archive adapters.
5. Route `/fire <employee> [--drain]` through the production service.
6. Add unit, integration, recovery and handler contract tests.
7. Run targeted and full Autonomous validation, update goals and project
   memory, then commit and push to `dev`.
