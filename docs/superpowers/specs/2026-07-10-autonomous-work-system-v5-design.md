# GhostAP Autonomous Work System v5 Design

## Status

- Source of truth: `GhostAp 数字员工自主工作系统重构方案 v5`, revision 7.
- Goal: replace Slock's in-memory execution core with a durable, verifiable,
  recoverable autonomous work system while preserving compatible user-facing
  collaboration capabilities.
- Decision mode: the user asked the agent to automatically accept recommended
  grill-me decisions. The recommendations below are therefore approved unless
  contradicted by the v5 source document.
- Baseline audit: the untracked `src/autonomous/` package is an isolated
  prototype. It is not production-wired, has 4 Critical and 10 High core
  findings, 10 safety P0 findings, 9 migration blockers, and covers none of the
  77 atomic section-18 acceptance gates completely.

## Goal Snapshot

- **Goal:** a user gives a goal to Manager once; GhostAP then plans, executes,
  verifies, recovers, and reports within explicit authorization.
- **Success:** the five-layer architecture is production-wired; Manager is the
  normal control entry; Slock becomes a compatibility and collaboration
  projection over the same journal; One-shot, Scheduled, and Standing goals
  share one durable runtime; all automatable v5 gates pass; environment and
  soak gates produce real evidence instead of assumed success.
- **Constraints:** `uv` only; no database; one machine, one Supervisor, one
  Manager control instance; Journal is the only fact source; no unbounded full
  auto; model and tool calls use mandatory brokers; tests precede behavior
  changes.
- **Non-goals:** multi-machine HA, Kubernetes replicas, partition-safe strong
  consistency, disk-loss RPO=0, and universal external-system exactly-once.

## Why the Existing Prototype Is Replaced

The current prototype has useful vocabulary but unsafe runtime semantics:

- Scheduler expects a fake `write_event()` interface that the real Journal
  does not implement.
- Goals, runs, leases, approvals, effects, and reports are held in memory and
  disappear after restart.
- Tool effects are sent before a durable PREPARED record.
- Runtime accepts arbitrary model and tool callbacks, bypassing brokers.
- Verifier executes a shell in the controller identity and workspace.
- Manager commands are not registered in Feishu routing.
- Existing Slock remains the only production execution path.
- The 42 tests bypass the real lifecycle and lint reports 74 errors.

The implementation may reuse enum names, serialization formats, and isolated
algorithms, but no existing component is treated as a trusted production
boundary until its v5 contract is proven.

## Chosen Approach

### Recommended: Strangler Migration

Build one shared autonomous kernel beside the old Slock path, route Manager
operations to it first, migrate Slock data and commands into read-only
projections, then remove legacy execution paths after cutover gates pass.

Advantages:

- No second writable fact source.
- One-shot Phase 1 can be verified before Scheduled/Standing activation.
- Existing team, role, task-board, discussion, and card UX can be preserved as
  projections instead of discarded.
- Rollback means returning aliases to read-only legacy views, not reconciling
  two independently mutated runtimes.

Rejected alternatives:

1. **Patch the current prototype in place and immediately redirect Slock.**
   This exposes known P0 races and restart data loss.
2. **Big-bang rewrite and delete Slock.** This risks losing team/role/task
   history and makes route regressions hard to isolate.

## Product Invariants

1. Manager is the normal user control plane for create, modify, approve,
   inspect, pause, resume, cancel, retry, accept, reject, and reconcile.
2. Feishu messages and mirror groups are projections, never facts.
3. `SUCCEEDED` requires criterion coverage, immutable evidence, artifact or
   remote resource identity, known limitations, and a Verifier attestation.
4. Assist performs no writes. Supervised and Bounded Autonomous remain inside
   grants, risk, budget, resource, data-flow, and epoch envelopes.
5. Any missing production anchor, verified sandbox, or shared-kernel gate
   forces offline Assist. It must not silently degrade to a broader mode.
6. Old card callbacks, approvals, leases, model responses, and trigger events
   cannot act on new epochs.
7. No unresolved Effect permits a terminal Run.

## Architecture

```text
Feishu / CLI emergency / Schedule / Event
                   |
          DurableIngressAdapter
                   |
          JournalWriter + BlobStore
                   |
           ProjectionRepository
                   |
       Admission -> Planner -> Compiler
                   |
            DurableScheduler
                   |
          AutonomousCoordinator
            /              \
       ModelBroker       ToolBroker
            \              /
            Sandboxed Worker
                   |
        Verifier / OracleRunner
                   |
      Finalization / Reconciler
                   |
          Durable Outbox
                   |
         Manager / mirror views
```

Horizontal controls:

- ControlAuthorizationGate
- DispatchGate
- BudgetLedger
- CapabilityRegistry
- ResourceBarrierRegistry
- KillSwitch and CleanupGate
- Supervisor, writer epoch, leader lock, health state
- Audit, metrics, acceptance manifest

## Deployment Modes

| Mode | Preconditions | Allowed behavior |
|---|---|---|
| `off` | none | Legacy routes only; no autonomous state changes |
| `shadow_read` | valid Journal | Replay and compare projections; no autonomous dispatch |
| `assist` | valid Journal and Manager ACL | Goal definition, plan, review, deterministic read-only verification |
| `supervised` | trusted anchor, sandbox, brokers, all P0 gates | R0/R1 automatic; R2 approval; R3/R4 denied |
| `bounded_autonomous` | supervised gates plus valid StandingOrder/Grant | R0-R2 inside envelope; R3 each-time approval; R4 denied |

`supervised` and `bounded_autonomous` cannot be selected by configuration
alone. Startup derives an effective mode from gate attestations.

## Single Fact Source

### Transaction Frame

Each physical frame contains:

- magic and schema version
- byte length and commit marker
- transaction ID, strict sequence, writer epoch, timestamp
- expected and resulting aggregate versions
- previous frame hash
- typed events with payload hashes and Blob references
- checksum and HMAC over the complete envelope
- anchor state

The writer:

1. obtains a cross-process leader lock and `flock`;
2. validates the full chain and external high-water mark;
3. appends one complete frame;
4. flushes and fsyncs the file and containing directory;
5. advances `(sequence, frame_hash)` through `AnchorProvider.compare_and_swap`;
6. opens external dispatch only after anchoring.

Only an incomplete physical tail may be truncated. A validly encoded but
invalid middle frame, sequence gap, HMAC failure, or anchor rollback starts
read-only quarantine.

### Blob Store

Sensitive payloads, model output, ToolResult, Evidence, and report bodies are
stored as tenant/run envelope-encrypted content-addressed blobs:

1. write a temporary encrypted blob;
2. fsync;
3. atomically publish by ciphertext hash;
4. fsync directory;
5. reference it in a Journal frame.

The Journal stores labels, sizes, hashes, purpose, and key references, not
plaintext. Orphan blobs are collected after a retention window. A referenced
missing blob is corruption and fails closed.

### Projections

Typed projections for goals, runs, plans, steps, attempts, effects, budgets,
approvals, decisions, subscriptions, employees, inbox, outbox, and reports are
rebuilt only from Journal events. Snapshot files are performance caches with
their source sequence and hash; deleting them cannot lose facts.

## Domain and State Machines

### Aggregates

- Principal and tenant mapping
- GoalDefinition and GoalRevision
- TriggerSubscription and ScheduleCursor
- Run and root run lineage
- PlanVersion and GoalCriteriaCoverage
- Step, Attempt, Activity, Lease
- ProposalRequest, ActionIntent, EffectInstance
- Evidence, VerificationAttestation, Finalization
- StandingOrder, CapabilityGrant, authorization records
- BudgetLedger and Reservation
- EmployeeDefinition, BotPrincipal, WorkerRuntime
- DecisionRequest, Report, OutboxDelivery

Every transition records from/event/guard/to, owner activity, aggregate
version, durable side effects, audit identity, and acknowledgement barrier.
Invalid transitions fail closed.

### Epochs

- `definition_version`: goal, criterion, trigger, or policy content
- `admission_epoch`: new Run and trigger admission
- `revocation_epoch`: immediate authorization revocation for existing Runs
- `run_control_epoch`: Run pause, resume, and cancel
- `plan_epoch`: active Plan generation
- `kill_epoch`: global, goal, employee, and capability stop
- `writer_epoch`: Supervisor/Journal ownership

Model and tool dispatch validates all relevant epochs, lease ownership,
fencing token, authorization, budget reservation, and current gate state.

## Authorization and Policy

`ControlAuthorizationGate` resolves an incoming actor to a canonical Principal
using tenant plus union/user ID and per-app open ID mapping. All list, query,
create, modify, control, approve, accept, and download operations default deny.

Authorization records bind:

- tenant, principal, owner, allowed deciders, required role
- goal/run/plan/step/attempt identities and all epochs
- capability version, adapter hash, schema hash, risk
- complete canonical arguments, content, recipients, resources, and data flow
- budget reservation, policy version, expiry, nonce
- canonical payload and render hashes

Nonce consumption, budget reservation, authorization snapshot, and Effect
PREPARED occur in one frame.

## Capability and Effect Execution

Capabilities are immutable, content-addressed descriptors containing version,
input/output schema, principal types, risk, business operation ID,
canonicalizer version, resource key, provider idempotency details, query
consistency, negative observation window, compensation, verifier, persistent
execution metadata, adapter hash, and schema hash.

The semantic identity is:

```text
root_run_lineage
+ business_operation_id
+ canonical_resource_key
+ canonical_semantic_arguments
+ occurrence_scope
```

`ActionIntent` is stable across plan revisions. `EffectInstance` adds an
atomic execution sequence. A normal retry reuses the same sequence; an
intentional repeat requires `repeat=true`, a new approval, and a new sequence.

Effect states implement the complete v5 machine, including
ABORTED_NO_DISPATCH, UNKNOWN_EFFECT, RECONCILING, RETRY_AUTHORIZED,
FAILED_SAFE, MANUAL_RECONCILIATION, compensation states, and accepted
abandonment. PREPARED, EXECUTING, UNKNOWN, reconciliation, retry, and
compensation states hold a durable resource barrier.

Physical sending is linearized:

1. create/reuse intent, reserve budget, authorize, and persist PREPARED;
2. enter DispatchGate and revalidate every guard;
3. persist and anchor EXECUTING plus active dispatch;
4. perform exactly one adapter send with implicit retries disabled;
5. persist COMMITTED, FAILED_SAFE, or UNKNOWN;
6. settle conservatively and release the gate.

## Runtime and Isolation

### Agent Runtime

Each turn receives Goal, active Plan, StepContract, Attempt, verified Evidence,
capability allowlist, budget, deadline, prior ToolResults, and checkpoint. It
may return only TOOL_PROPOSAL, REQUEST_CONTEXT, SUBMIT_OUTPUT, REPLAN_REQUEST,
or BLOCKED.

Model calls always use ModelBroker. Tool proposals always use ToolBroker.
`SUBMIT_OUTPUT` enters Verifier; `REPLAN_REQUEST` is not completion.

ContextSnapshot preserves source and artifact hashes, model window, selection
rules, compressor version, and token count. Plan-specific context is
invalidated after replan while shared Evidence follows lineage.

### Sandboxed Runners

Worker and Oracle use fixed signed runner entrypoints, separate low-privilege
identities or verified namespace isolation, empty HOME, no inherited secrets,
read-only trusted baseline, writable overlay/scratch, no default network,
closed inherited file descriptors, process group/cgroup limits, bounded
output, and whole-group timeout termination.

If the platform cannot prove file and network isolation, effective mode is
Assist. Existing `SandboxExecutor` command filtering is not accepted as this
proof.

## Planning, Verification, and Finalization

Planner may reference only registered capability versions. Compiler checks
DAGs, schemas, I/O compatibility, Principal/Grant satisfiability, resource
conflicts, criterion and Oracle coverage, budget feasibility, authorization
envelope, and semantic Effect conflicts.

Criteria have stable IDs and hashes. Replanning cannot weaken them. A Goal
Revision is required to modify acceptance.

Verifier runs deterministic Oracles in a disposable independent runner.
R2+, executable, and security results require deterministic proof. Model review
is supplementary. Insufficient free-text criteria become Human Oracles.

Finalization owns all Effect dispositions. A Run reaches SUCCEEDED, FAILED,
CANCELED, or EXPIRED only after every Effect is retained, compensated,
accepted by an authorized administrator, or proven failed-safe. Terminal state
and Outbox delivery creation share one frame.

## Scheduler, Recovery, and Kill

Scheduler queue, lease, retry, deadline, dead letter, and fencing state are
Journal aggregates. Acquisition and renewal are versioned atomic transitions.
Old fencing tokens can never dispatch.

Startup order:

1. acquire leader lock and increment writer/Supervisor epoch;
2. load Kill and external anchor, defaulting to read-only on inconsistency;
3. verify Journal and Blob references;
4. rebuild projections;
5. orphan old Activities and Attempts;
6. reconcile active dispatch and Effect uncertainty;
7. rebuild cursor, retry, deadline, outbox, and barriers;
8. start cleanup and brokers;
9. start Manager channel;
10. start eligible workers.

Kill closes normal DispatchGate first, increments kill epoch, revokes leases,
stops new model/tool authorization, terminates worker process groups, marks
possibly sent requests UNKNOWN, persists cleanup work, and survives restart.
`ghostap-stop` is a minimal process-external command. Cleanup uses narrow
resource-bound grants and the same Journal, never a second fact source.

## Manager and Feishu Integration

Manager command namespace uses explicit `/goal ...` subcommands to avoid the
existing global `/status` conflict:

- `/goal create`
- `/goal list`
- `/goal show`
- `/goal modify`
- `/goal activate`
- `/goal pause`
- `/goal resume`
- `/goal cancel`
- `/goal health`
- `/run start`
- `/run list`
- `/run show`
- `/run retry`
- `/run cancel`
- `/approval list|approve|reject`
- `/decision list|resolve`
- `/report failed|resend`
- `/employee hire|list|show|retire`

Cards render canonical untruncated action content, visible Unicode controls,
evidence, resource state, effect disposition options, and stale/expired state.
Callbacks carry only opaque IDs and one-time nonces; current Principal, ACL,
epoch, and canonical hashes are revalidated server-side.

The Manager pending center is reconstructed from Journal and remains usable
when prior cards expire or delivery enters dead letter.

## Slock Migration

### Compatibility Mapping

- team/chat/owner -> Manager destination and optional collaboration mirror
- role/persona/tool/model -> EmployeeDefinition
- task board -> Goal/Run/Step/Attempt projection
- escalation/clarification -> DecisionRequest
- discussion/council -> optional review Activity and mirror events
- task chain DAG algorithm -> Planner helper, not scheduler state
- memory -> read-only imported Evidence until memory governance passes
- cards -> Journal-backed renderers and translated callback aliases

### Cutover Modes

1. `legacy`: current behavior, autonomous off.
2. `shadow_read`: import and compare projections; legacy remains writer.
3. `manager_only`: Manager is writer; Slock commands are read-only aliases or
   forward a goal to Manager.
4. `disabled`: legacy execution and passive activation removed.

The importer is idempotent, versioned, dry-runnable, journaled, and produces a
hash/count report. No dual writes are allowed. Old destructive card callbacks
expire rather than executing against new state.

## Scheduled and Standing Goals

Trigger adapters declare delivery semantics, replay, cursor/watermark, gap
detection, maximum recovery window, ACK control, and heartbeat. Only durable
ACK or authoritative replay sources are eligible.

Occurrence creation atomically records Inbox acceptance, proposal decision,
Run creation/dedup, occurrence tombstone, and cursor/watermark advance.
Hard limits cover queued Runs, hourly/daily creation, misfire count,
consecutive failures, notifications, and disk.

Trigger health produces GoalHealthSnapshot. Replay-window loss blocks writes
and creates a Goal-level DecisionRequest.

## Employee Lifecycle and Feishu Enhancements

Logical Worker is default. Visible employees are optional and capacity is
configured, never hard-coded. Provisioning and retirement are durable Sagas.
App secrets use Keychain/KMS references.

The locked Feishu baseline is `lark-oapi==1.6.5` and
`lark-channel-sdk==1.1.0`, verified from installed packages and `uv.lock`.
Meeting entry remains hidden when the tenant is not in gray and is always R3.

## Acceptance and Evidence

The repository will contain a machine-readable manifest for all 77 atomic
section-18 gates plus Phase 0-4 exits. Each entry records owner, test selector,
threshold, evidence artifact, environment prerequisites, and status.

Evidence levels:

1. **Unit/contract:** deterministic local behavior.
2. **Integration:** real process, Journal, broker, runner, and route chain.
3. **Chaos/security:** lifecycle kill points, races, replay, disk and sandbox.
4. **Tenant E2E:** real Feishu/model/provider behavior.
5. **Soak/statistical:** required sample size and time window.

Long-running thresholds such as 30 days cannot be marked passed by a short
unit test. The implementation supplies scenario runners, metric definitions,
Wilson/percentile calculations, signed manifests, and resumable evidence
storage. The gate remains pending until real samples satisfy it.

The global write gate consumes this manifest:

- any P0 or shared-kernel fault failure -> offline Assist only;
- missing memory governance -> memory disabled;
- missing VC gray capability -> meeting entry hidden;
- benchmark threshold below target -> no autonomous-complete claim.

## Test Strategy

1. Contract tests freeze domain transitions, commands, 77 gate IDs, metrics,
   and production wiring.
2. Unit tests cover Journal, Blob, projections, authorization, budget, state
   machines, compiler, brokers, scheduler, runtime, verifier, outbox,
   reconciler, and finalization.
3. Process integration tests run the complete One-shot lifecycle without
   mocking Journal, scheduler, runtime, brokers, Effect, verification, or
   finalization.
4. Chaos tests inject crashes at every durable boundary, 100-event replay,
   remote-success/local-crash, stale lease, ENOSPC, kill/revocation races,
   cleanup recovery, adapter upgrade, and old backup.
5. Security tests exercise prompt injection, DLP, canonical rendering,
   Principal/nonce abuse, network/file/secret escape, fork bomb, OOM, and
   Oracle tampering.
6. Manager-only E2E covers all three Goal types and every normal operation.
7. Benchmark runners enforce sample sizes, domains, difficulty, hidden
   manifests, percentiles, Wilson bounds, and zero-event confidence bounds.

## Implementation Order

1. Phase 0 safety baseline and effective-mode gate.
2. Journal, Blob, projections, state machines, authorization, and budget.
3. Scheduler, ModelBroker, ToolBroker, Effect reconciliation, and sandbox.
4. One-shot Manager + Logical Worker closed loop.
5. Slock importer, aliases, employees, multi-step and multi-worker planning.
6. Scheduled/Standing triggers and health.
7. Visible employee, comment, Web integration points, VC capability hiding.
8. Full acceptance manifest, chaos/security runs, two clean mloop reviews.

No later phase may bypass an earlier safety boundary for convenience.
