# Phase 3 Task 2 Implementation Report

## Status

DONE

## Scope

- Implemented Journal-backed employee Ingress projection and service.
- Added a dedicated encrypted Ingress BlobStore owner using the employee data
  keyring provider without sharing the Data BlobStore.
- Did not implement Channel bridge, Router, or ACP integration.
- Did not create FI-29 or claim production readiness.

## RED Evidence

Initial required files and true RED:

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/autonomous/unit/test_employee_durable_inbox.py \
  tests/autonomous/chaos/test_employee_ingress_recovery.py -q
```

```text
2 failed in 0.77s
projection/service module specs were absent
```

Behavior RED after the empty module existence cycle:

```text
2 collection errors: IngressProjectionState was absent
```

Review regression RED:

```text
test_blob_readback_mismatch_quarantines_publish_before_journal failed:
published Blob remained live after authenticated readback mismatch
```

## GREEN Evidence

Ingress implementation and existing contract:

```text
84 passed in 2.33s
```

Ingress plus adjacent Journal/Data regression:

```text
185 passed in 4.43s
```

EI-IPC-01 exact selector:

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest -s \
  tests/autonomous/chaos/test_employee_ingress_recovery.py::\
test_ipc_ack_only_after_anchored_acceptance -q
```

```text
EI-IPC-01 elapsed_seconds=0.014952 bound_seconds=1.5 anchored_sequence=1
1 passed in 1.02s
```

Quality gates:

```text
ruff: All checks passed!
git diff --check: passed
```

## Coverage

- True concurrent duplicate admission: 32 callers, one acceptance/Blob/frame.
- Stable restart replay across a new channel generation and connection.
- Trusted fallback action correlation; missing or mismatched correlation rejects.
- Semantic and sender/chat/action provenance conflicts reject without new frame.
- AES-GCM labels/AAD bind schema, tenant, employee, envelope, dedup identity,
  and semantic digest; Journal contains no normalized payload or key material.
- Missing/corrupt nonterminal Blob closes employee admission on restart.
- Blob publish/readback failure creates no acceptance; readback orphan is quarantined.
- Journal fsync and anchor failures create no applied acceptance and quarantine Blob.
- Live-set orphan quarantine preserves accepted records and isolates unreferenced Blob.
- Terminal disposition durably tombstones before GC while acceptance metadata remains.
- Ingress service is the sole idempotent close owner of its dedicated BlobStore.
- EI-IPC-01 uses a spawned process, Pipe, FileAnchor, real Journal fsync, and an
  exact 1.5-second ACK bound.

## Self-Review

- Round 1 found the Blob readback mismatch orphan gap; regression was written
  first, observed failing, then fixed by the common quarantine boundary.
- Round 2 re-read only the current goal and implementation and found no material
  correctness, architecture, engineering, or QA issue.

## Commit

- Baseline: `0b022e6fd92784040351208b4029c4c125183072`
- Subject: `feat(autonomous): persist employee ingress before ack`
- Exact resulting SHA is reported by Git after this report-containing commit.

## Concerns

- EI-IPC-01 is local implementation evidence only. Its collectability and bound
  result do not satisfy FI-29, real-tenant acceptance, or production readiness.
- Per parent instruction, Task 2 ran focused and adjacent Journal/Data suites,
  not the full repository suite; the main thread owns full-suite aggregation.
- Pytest conftest reported 18 already-running Node processes after selectors;
  no Task 2 test spawned Node, and the warning did not affect test results.

## Review Block Resolution

The Task 2 review identified two Important crash-consistency defects after the
initial commit `1703b1e`.

RED evidence:

```bash
uv --cache-dir /tmp/ghostap-uv-cache run python -m pytest \
  tests/autonomous/chaos/test_employee_ingress_recovery.py \
  -k 'restart_quarantines_blob_left or restart_retries_gc or invalid_disposition' -q
```

```text
4 failed, 10 deselected
- pre-commit process exit left an unreferenced Blob live across restart
- post-tombstone process exit left the physical Blob live across restart
- invalid disposition state and reason were anchored before reducer validation
```

Fixes:

- The sole Ingress owner now reconciles its complete BlobStore live-set during
  every projection rebuild/startup and strictly quarantines every unreferenced
  Blob. Tombstoned records are not live Blob references.
- Terminal GC now retries physical quarantine for already-tombstoned records
  when the Blob is still present; it does not emit a second tombstone frame.
- `record_disposition()` constructs a complete validated `IngressDisposition`
  draft before creating or committing the Journal event. Invalid state/reason
  leaves Journal bytes, anchor, writer health, and replay unchanged.

GREEN evidence:

```text
review regressions: 4 passed, 10 deselected in 0.85s
Task 2 plus adjacent Journal/Data: 189 passed in 4.39s
ruff: All checks passed!
git diff --check: passed
```
