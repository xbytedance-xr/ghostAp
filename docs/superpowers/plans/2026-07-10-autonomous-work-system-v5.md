# GhostAP Autonomous Work System v5 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Slock's in-memory execution core with the v5 durable autonomous work system and migrate its production routes, data, and collaboration surface onto the shared kernel.

**Architecture:** Use a strangler migration. Build a journal-backed single-controller kernel first, expose Manager-only One-shot execution second, migrate Slock into compatibility projections third, and add multi-worker, trigger, and Feishu enhancement layers only after their safety prerequisites pass. Effective autonomy is derived from verified gate attestations; missing anchors or sandbox evidence forces Assist.

**Tech Stack:** Python 3.11+, asyncio, pydantic-settings, `lark-oapi==1.6.5`, `lark-channel-sdk==1.1.0`, JSONL transaction frames, HMAC-SHA256, `fcntl.flock`, content-addressed blobs, bubblewrap when available, pytest/pytest-asyncio, ruff.

## Global Constraints

- Use `uv` exclusively; never invoke pip or conda.
- Single machine, single Supervisor, single Manager control instance, no database.
- Journal events are the only source of truth; snapshots and JSON views are rebuildable.
- No external model, real data, or write tool is permitted while any P0/shared-kernel gate is open.
- Assist performs no writes; R4 is always denied.
- Model calls go through ModelBroker and tool calls go through ToolBroker.
- No Effect dispatch occurs before PREPARED and EXECUTING frames are fsynced and anchored.
- No Run reaches a terminal state while an unresolved Effect or undisposed committed Effect exists.
- All behavior changes follow red-green-refactor and include focused regression tests.
- Existing SMART, ordinary programming, Deep, Spec, Worktree, and Workflow routing must not regress.
- Long-term memory stays disabled until governance gates pass.
- VC entry stays hidden when gray capability is unavailable.
- Final integration is committed with repository commit-message conventions and pushed to `origin/dev`.

---

## File Structure

The current 1,005-line `models.py` and disconnected components are split into focused packages:

```text
src/autonomous/
  bootstrap.py                 production composition root
  config.py                    deployment/effective-mode contracts
  coordinator.py               lifecycle orchestration
  planner.py                   GoalSpec/Plan production planner
  domain/
    ids.py                     ID and canonical hashing
    enums.py                   shared states and risk values
    goals.py                   Goal/Run/Trigger aggregates
    plans.py                   Plan/Step/Attempt/criteria
    effects.py                 intent/effect/finalization aggregates
    control.py                 principal/grant/approval/decision
    reporting.py               report/outbox/progress types
    employees.py               employee/bot/worker lifecycle
  journal/
    frame.py                   transaction-frame encoding and validation
    writer.py                  single writer, fsync and lock
    anchor.py                  monotonic anchor protocol/providers
    blob_store.py              encrypted content-addressed blobs
    projections.py             replay materializer and snapshots
    journal.py                 compatibility facade
  policy/
    authorization.py           control and dispatch authorization
    policy_engine.py           risk/standing-order evaluation
    budget_manager.py          journal aggregate reservations
    kill_switch.py             journal-backed stop state
  broker/
    capability_registry.py     immutable descriptors/canonicalization
    dispatch_gate.py           linearized dispatch and epochs
    model_broker.py            model ledger and cost reservation
    tool_broker.py             effect dispatch/recovery
  scheduler/
    scheduler.py               journal-backed queue/lease/fencing
    activities.py              durable control-plane activities
    triggers.py                schedule/standing trigger service
  runtime/
    runtime.py                 structured turn protocol
    runner.py                  worker/oracle sandbox
    worker.py                  fixed worker entrypoint
  verifier/
    verifier.py                criterion compiler and attestations
    oracle_runner.py           fixed deterministic runner entrypoint
  reporter/
    reporter.py                durable outbox
    finalization.py            effect disposition saga
  supervisor/
    supervisor.py              startup/recovery/process lifecycle
    reconciler.py              lease/effect/outbox/trigger reconciliation
    cleanup.py                 narrow cleanup execution
  manager/
    admission.py               durable inbox/admission
    handler.py                 command use cases
    cards.py                   canonical Feishu cards
    feishu_adapter.py          durable ingress and delivery
  migration/
    slock_importer.py          idempotent legacy importer
    slock_compat.py            command/card compatibility aliases
  acceptance/
    manifest.py                gate schema and evaluator
    metrics.py                 percentile/Wilson/zero-event calculations
    evidence.py                immutable evidence artifacts
scripts/
  ghostap_stop.py
  autonomous_acceptance.py
tests/autonomous/
  contract/
  unit/
  integration/
  chaos/
  security/
  acceptance/
```

### Task 1: Freeze Safety Configuration and Acceptance Contracts

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/config/settings.py`
- Modify: `.env.example`
- Create: `src/autonomous/config.py`
- Create: `src/autonomous/acceptance/manifest.py`
- Create: `src/autonomous/acceptance/__init__.py`
- Create: `tests/autonomous/contract/test_config_and_gate_contract.py`
- Create: `tests/autonomous/contract/test_acceptance_manifest.py`
- Create: `tests/autonomous/acceptance/manifest.json`

**Interfaces:**
- Produces: `AutonomousDeploymentMode`, `SafetyGateStatus`, `EffectiveAutonomy`, `derive_effective_autonomy(settings, attestations)`.
- Produces: `AcceptanceManifest.load(path)`, `AcceptanceManifest.evaluate(evidence)`.
- Consumes: v5 effective-mode and all 77 section-18 gate IDs.

- [ ] **Step 1: Write failing dependency and effective-mode tests**

```python
def test_write_modes_fail_closed_without_anchor_and_sandbox(settings):
    settings.autonomous_deployment_mode = "manager_only"
    status = derive_effective_autonomy(settings, {})
    assert status.mode is EffectiveAutonomy.ASSIST
    assert {"anchor", "worker_sandbox", "oracle_sandbox"} <= set(status.blockers)


def test_locked_lark_dependencies():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    assert "lark-oapi==1.6.5" in project["project"]["dependencies"]
    assert "lark-channel-sdk==1.1.0" in project["project"]["dependencies"]
```

- [ ] **Step 2: Run tests and verify RED**

Run: `uv run pytest tests/autonomous/contract/test_config_and_gate_contract.py -q`

Expected: FAIL because the config types and pinned dependencies do not exist.

- [ ] **Step 3: Implement the contract**

```python
class AutonomousDeploymentMode(str, Enum):
    OFF = "off"
    SHADOW_READ = "shadow_read"
    MANAGER_ONLY = "manager_only"


class EffectiveAutonomy(str, Enum):
    OFF = "off"
    SHADOW_READ = "shadow_read"
    ASSIST = "assist"
    SUPERVISED = "supervised"
    BOUNDED_AUTONOMOUS = "bounded_autonomous"


@dataclass(frozen=True)
class SafetyGateStatus:
    mode: EffectiveAutonomy
    blockers: tuple[str, ...]
    attestations: Mapping[str, bool]
```

Settings include state paths, Manager ACL, anchor provider, sandbox requirement, memory/VC feature flags, queue limits, and compatibility mode. Defaults are `off`, `legacy`, memory disabled, and write mode blocked.

- [ ] **Step 4: Freeze the 77-gate manifest**

Each JSON record contains `id`, `phase`, `source_lines`, `owner`, `selector`, `threshold`, `evidence_level`, `environment`, and `status`. Tests assert IDs DS-01..08, FM-01..21, FI-01..32, MD-01..07, AR-01..05, MG-01..04 are present exactly once.

- [ ] **Step 5: Update and verify dependencies**

Run: `uv add "lark-oapi==1.6.5" "lark-channel-sdk==1.1.0"`

Expected: `uv.lock` resolves both exact versions.

- [ ] **Step 6: Run focused verification**

Run: `uv run pytest tests/autonomous/contract -q && uv run ruff check src/autonomous/config.py src/autonomous/acceptance tests/autonomous/contract`

Expected: PASS and no lint findings.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/config/settings.py .env.example src/autonomous/config.py src/autonomous/acceptance tests/autonomous
git commit -m "feat(autonomous): freeze safety and acceptance contracts"
```

### Task 2: Implement the Single-Writer Journal, Anchor, and Blob Store

**Files:**
- Create: `src/autonomous/journal/frame.py`
- Create: `src/autonomous/journal/writer.py`
- Create: `src/autonomous/journal/anchor.py`
- Create: `src/autonomous/journal/blob_store.py`
- Modify: `src/autonomous/journal/journal.py`
- Modify: `src/autonomous/journal/__init__.py`
- Create: `tests/autonomous/unit/test_journal_frame.py`
- Create: `tests/autonomous/unit/test_journal_writer.py`
- Create: `tests/autonomous/unit/test_blob_store.py`
- Create: `tests/autonomous/chaos/test_journal_blob_crash_boundaries.py`

**Interfaces:**
- Produces: `TransactionFrame`, `JournalEvent`, `JournalWriter.commit(events, expected_versions)`.
- Produces: `AnchorProvider.read()` and `compare_and_swap(...)`.
- Produces: `BlobStore.stage_and_publish(payload, labels, key_ref)`.
- Consumes: `SafetyGateStatus` to reject unanchored write transactions.

- [ ] **Step 1: Write RED tests for split writers, metadata tampering, and tail recovery**

```python
def test_second_writer_cannot_open_same_journal(tmp_path):
    first = JournalWriter.open(tmp_path, anchor=MemoryAnchor())
    with pytest.raises(WriterLockError):
        JournalWriter.open(tmp_path, anchor=MemoryAnchor())
    first.close()


def test_sequence_tampering_is_detected(tmp_path):
    writer = make_writer(tmp_path)
    writer.commit([event("goal.created", "goal_1")], {})
    mutate_frame_field(tmp_path, "sequence", 999)
    with pytest.raises(JournalIntegrityError):
        JournalWriter.open(tmp_path, anchor=writer.anchor)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_journal_frame.py tests/autonomous/unit/test_journal_writer.py -q`

Expected: FAIL because complete frame validation and writer locking do not exist.

- [ ] **Step 3: Implement complete frame encoding**

```python
@dataclass(frozen=True)
class TransactionFrame:
    schema_version: int
    tx_id: str
    sequence: int
    writer_epoch: int
    aggregate_versions: Mapping[str, int]
    previous_hash: str
    events: tuple[JournalEvent, ...]
    checksum: str
    hmac_digest: str
    committed: bool
```

Encode one newline-delimited physical record containing a byte length and commit marker. Hash/HMAC covers all fields except their own digest values.

- [ ] **Step 4: Implement writer fence and anchor**

Use `fcntl.flock(LOCK_EX | LOCK_NB)`, strict sequence validation, `flush`, file `fsync`, parent directory `fsync`, and CAS of `(sequence, frame_hash)`. Anchor failure returns `DURABLE_NOT_ANCHORED` and closes write dispatch.

- [ ] **Step 5: Implement BlobStore protocol**

Use temporary file, authenticated encryption provider abstraction, file fsync, content-hash rename, directory fsync, and immutable `BlobRef`. Tests inject failure after every boundary and assert no committed frame references a missing blob.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/unit/test_journal_frame.py tests/autonomous/unit/test_journal_writer.py tests/autonomous/unit/test_blob_store.py tests/autonomous/chaos/test_journal_blob_crash_boundaries.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/journal tests/autonomous/unit/test_journal* tests/autonomous/unit/test_blob_store.py tests/autonomous/chaos/test_journal_blob_crash_boundaries.py
git commit -m "feat(autonomous): add fenced journal and blob store"
```

### Task 3: Split Domain Aggregates and Enforce State Machines

**Files:**
- Create: `src/autonomous/domain/ids.py`
- Create: `src/autonomous/domain/enums.py`
- Create: `src/autonomous/domain/goals.py`
- Create: `src/autonomous/domain/plans.py`
- Create: `src/autonomous/domain/effects.py`
- Create: `src/autonomous/domain/control.py`
- Create: `src/autonomous/domain/reporting.py`
- Create: `src/autonomous/domain/employees.py`
- Create: `src/autonomous/domain/state_machine.py`
- Create: `src/autonomous/domain/__init__.py`
- Replace: `src/autonomous/models.py`
- Create: `tests/autonomous/unit/test_domain_serialization.py`
- Create: `tests/autonomous/unit/test_state_machines.py`
- Create: `tests/autonomous/unit/test_finalization_guards.py`

**Interfaces:**
- Produces immutable aggregate records and `transition(current, event, context)`.
- Produces stable canonical hashes and full v5 Run/Plan/Step/Effect states.
- Maintains compatibility re-exports from `src.autonomous.models`.

- [ ] **Step 1: Write illegal-transition and unresolved-effect RED tests**

```python
def test_run_cannot_succeed_with_unresolved_effect():
    run = Run(state=RunState.VERIFYING)
    context = RunTransitionContext(unresolved_effect_ids=("effect_1",))
    with pytest.raises(TransitionRejected):
        transition_run(run, RunEvent.VERIFICATION_PASSED, context)


def test_replan_cannot_weaken_criterion_hash():
    old = criterion("tests pass")
    new = dataclasses.replace(old, description="some tests pass")
    with pytest.raises(CriterionMutationError):
        assert_replan_criteria_compatible([old], [new])
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_state_machines.py tests/autonomous/unit/test_finalization_guards.py -q`

Expected: FAIL because states are enums without guarded transitions.

- [ ] **Step 3: Implement aggregate fields and states**

Include tenant/principal isolation, root run lineage, retry/revision relations, trigger cursor health, complete Step/Attempt states, full Effect machine, resource quarantine, report and employee Sagas.

- [ ] **Step 4: Implement pure transition functions**

Every function returns `(updated_aggregate, transition_record, emitted_events)` and checks expected aggregate version, epochs, ownership, criterion hashes, unresolved effects, and allowed source state.

- [ ] **Step 5: Preserve import compatibility**

`src/autonomous/models.py` imports and re-exports domain types only; it contains no aggregate logic.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/unit/test_domain_serialization.py tests/autonomous/unit/test_state_machines.py tests/autonomous/unit/test_finalization_guards.py -q && uv run ruff check src/autonomous/domain src/autonomous/models.py`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/domain src/autonomous/models.py tests/autonomous/unit/test_domain_serialization.py tests/autonomous/unit/test_state_machines.py tests/autonomous/unit/test_finalization_guards.py
git commit -m "refactor(autonomous): enforce aggregate state machines"
```

### Task 4: Build Replay Projections and Durable Inbox

**Files:**
- Create: `src/autonomous/journal/projections.py`
- Create: `src/autonomous/journal/snapshots.py`
- Replace: `src/autonomous/manager/admission.py`
- Create: `tests/autonomous/unit/test_projections.py`
- Create: `tests/autonomous/unit/test_inbox.py`
- Create: `tests/autonomous/integration/test_inbox_recovery.py`
- Create: `tests/autonomous/chaos/test_ingress_ack_boundaries.py`

**Interfaces:**
- Produces: `ProjectionRepository.rebuild(journal)` and typed query methods.
- Produces: `DurableInbox.accept(event)` and `consume_trigger(...)`.
- Consumes: Journal transaction APIs and domain transitions.

- [ ] **Step 1: Write restart and replay RED tests**

```python
def test_goal_run_and_inbox_survive_restart(runtime_dir):
    system = create_test_store(runtime_dir)
    event_id = system.inbox.accept(user_goal_event())
    goal_id, run_id = system.admission.create_one_shot_from_event(event_id)
    system.close()
    restored = create_test_store(runtime_dir)
    assert restored.projections.goal(goal_id) is not None
    assert restored.projections.run(run_id) is not None
    assert restored.projections.inbox(event_id).processed is True
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_projections.py tests/autonomous/unit/test_inbox.py -q`

Expected: FAIL because current lists/dicts are not replayed.

- [ ] **Step 3: Implement projections**

Reducers are pure and exhaustive over event schema. Snapshot publication uses file fsync, replace, directory fsync, and records source sequence/hash.

- [ ] **Step 4: Implement durable ingress**

User dedup key is `tenant + chat_id + message_id + source_type`. Trigger consumption atomically records accepted event, proposal decision, run/dedup, occurrence tombstone, and cursor advance.

- [ ] **Step 5: Verify crash points**

Run: `uv run pytest tests/autonomous/integration/test_inbox_recovery.py tests/autonomous/chaos/test_ingress_ack_boundaries.py -q`

Expected: 100 replays create one logical event and one Run.

- [ ] **Step 6: Commit**

```bash
git add src/autonomous/journal/projections.py src/autonomous/journal/snapshots.py src/autonomous/manager/admission.py tests/autonomous/unit/test_projections.py tests/autonomous/unit/test_inbox.py tests/autonomous/integration/test_inbox_recovery.py tests/autonomous/chaos/test_ingress_ack_boundaries.py
git commit -m "feat(autonomous): persist inbox and replay projections"
```

### Task 5: Implement Principals, Authorizations, Policy, and Budget

**Files:**
- Create: `src/autonomous/policy/authorization.py`
- Replace: `src/autonomous/policy/policy_engine.py`
- Replace: `src/autonomous/policy/budget_manager.py`
- Modify: `src/autonomous/policy/__init__.py`
- Create: `tests/autonomous/unit/test_control_authorization.py`
- Create: `tests/autonomous/unit/test_policy_engine.py`
- Create: `tests/autonomous/unit/test_budget_ledger.py`
- Create: `tests/autonomous/security/test_approval_nonce.py`

**Interfaces:**
- Produces `ControlAuthorizationGate.authorize(principal, operation, resource)`.
- Produces typed activation/derived/effect/model authorizations.
- Produces `BudgetManager.reserve/settle/release` as Journal transactions.

- [ ] **Step 1: Write cross-user, replayed approval, and budget RED tests**

```python
def test_non_owner_cannot_pause_goal(system, alice, mallory):
    goal = system.create_goal(alice)
    with pytest.raises(AuthorizationDenied):
        system.control.authorize(mallory, "goal.pause", goal)


def test_budget_rejects_nan_negative_and_concurrent_oversell(system):
    ledger = system.budgets.create(limit={"tool_calls": 1})
    for amount in (-1, float("nan")):
        with pytest.raises(InvalidBudgetAmount):
            system.budgets.reserve(ledger.id, "tool_calls", amount)
    assert exactly_one_concurrent_reservation_succeeds(system, ledger.id)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_control_authorization.py tests/autonomous/unit/test_budget_ledger.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement canonical Principal and default-deny control gate**

All reads and writes include tenant/owner/ACL checks. Manager's `is_admin` is derived from settings and principal mapping, never trusted from callback payload.

- [ ] **Step 4: Implement immutable authorization envelopes**

Bind full canonical payload/render hashes, recipients/resources/data labels, versions/epochs, budget, expiry, and one-time nonce. Consume nonce and create dependent authorization records in the same frame.

- [ ] **Step 5: Implement Journal budget aggregate**

Validate finite positive amounts, version-CAS all changes, restore entries by ID, and conservatively settle unknown billing.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/unit/test_control_authorization.py tests/autonomous/unit/test_policy_engine.py tests/autonomous/unit/test_budget_ledger.py tests/autonomous/security/test_approval_nonce.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/policy tests/autonomous/unit/test_control_authorization.py tests/autonomous/unit/test_policy_engine.py tests/autonomous/unit/test_budget_ledger.py tests/autonomous/security/test_approval_nonce.py
git commit -m "feat(autonomous): add durable authorization and budget gates"
```

### Task 6: Implement Capability Registry, Resource Barriers, and Tool Dispatch

**Files:**
- Create: `src/autonomous/broker/capability_registry.py`
- Create: `src/autonomous/broker/dispatch_gate.py`
- Replace: `src/autonomous/broker/tool_broker.py`
- Modify: `src/autonomous/broker/__init__.py`
- Create: `tests/autonomous/unit/test_capability_registry.py`
- Create: `tests/autonomous/unit/test_effect_state_machine.py`
- Create: `tests/autonomous/integration/test_effect_idempotency.py`
- Create: `tests/autonomous/chaos/test_remote_success_local_crash.py`
- Create: `tests/autonomous/chaos/test_revocation_race.py`

**Interfaces:**
- Produces immutable `CapabilityDescriptor@version` and canonicalization.
- Produces `DispatchGate.dispatch(authorized_effect, adapter)`.
- Produces ToolBroker query/reconcile/compensate operations.

- [ ] **Step 1: Write concurrent duplicate and crash RED tests**

```python
@pytest.mark.asyncio
async def test_concurrent_same_intent_has_one_physical_send(system, fake_remote):
    first, second = await asyncio.gather(
        system.tools.dispatch(request("same semantic action")),
        system.tools.dispatch(request("same semantic action")),
    )
    assert fake_remote.send_count == 1
    assert first.effect_instance_id == second.effect_instance_id


def test_restart_reconciles_remote_success_without_resend(crash_harness):
    crash_harness.crash_after_remote_success()
    restored = crash_harness.restart()
    restored.reconcile()
    assert restored.remote.send_count == 1
    assert restored.effect.state is EffectState.COMMITTED
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/integration/test_effect_idempotency.py tests/autonomous/chaos/test_remote_success_local_crash.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement descriptor and canonicalizer**

Descriptors contain all v5 fields. Canonicalizers are tested for aliases, defaults, order, Unicode normalization, numeric representation, and adversarial input. Registry rejects mutable or digest-mismatched adapters.

- [ ] **Step 4: Implement linearized dispatch**

PREPARED and EXECUTING/active-dispatch transactions are anchored before one send. Kill/revocation closes the gate before epoch changes. Implicit SDK retry is disabled.

- [ ] **Step 5: Implement UNKNOWN reconciliation and barriers**

Query uses pinned adapter version and declared consistency window. Unsupported unknown actions enter manual reconciliation and retain the resource barrier.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/unit/test_capability_registry.py tests/autonomous/unit/test_effect_state_machine.py tests/autonomous/integration/test_effect_idempotency.py tests/autonomous/chaos/test_remote_success_local_crash.py tests/autonomous/chaos/test_revocation_race.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/broker tests/autonomous/unit/test_capability_registry.py tests/autonomous/unit/test_effect_state_machine.py tests/autonomous/integration/test_effect_idempotency.py tests/autonomous/chaos/test_remote_success_local_crash.py tests/autonomous/chaos/test_revocation_race.py
git commit -m "feat(autonomous): enforce durable effect dispatch"
```

### Task 7: Implement Durable Scheduler and Activities

**Files:**
- Replace: `src/autonomous/scheduler/scheduler.py`
- Create: `src/autonomous/scheduler/activities.py`
- Modify: `src/autonomous/scheduler/__init__.py`
- Create: `tests/autonomous/unit/test_scheduler.py`
- Create: `tests/autonomous/unit/test_activities.py`
- Create: `tests/autonomous/chaos/test_lease_fencing.py`
- Create: `tests/autonomous/chaos/test_activity_recovery.py`

**Interfaces:**
- Produces atomic queue/lease/retry/dead-letter transitions.
- Produces `ActivityExecutor` for Admission, Planning, Verification, Reconciliation, and Reporting.
- Consumes ProjectionRepository and JournalWriter.

- [ ] **Step 1: Write duplicate lease and restart RED tests**

```python
@pytest.mark.asyncio
async def test_same_step_never_receives_two_leases(system):
    leases = await asyncio.gather(
        system.scheduler.acquire(step_id, "worker_1"),
        system.scheduler.acquire(step_id, "worker_2"),
    )
    assert sum(lease is not None for lease in leases) == 1


def test_stale_fencing_token_cannot_dispatch_after_restart(system):
    old = system.scheduler.acquire_sync(step_id, "worker_1")
    restored = system.restart()
    with pytest.raises(StaleLease):
        restored.tools.dispatch_sync(request(fencing_token=old.fencing_token))
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_scheduler.py tests/autonomous/chaos/test_lease_fencing.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement Journal-backed scheduling**

Acquire/renew/release/expire use aggregate-version transactions. Fencing tokens derive from writer epoch plus monotonic lease sequence. Backoff, retry, deadline, pressure, and dead letter survive restart.

- [ ] **Step 4: Implement durable control activities**

Activities have input hash, state, attempt, lease, heartbeat, checkpoint BlobRef, and effect ledger. Reconciler can resume every control-plane phase.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/autonomous/unit/test_scheduler.py tests/autonomous/unit/test_activities.py tests/autonomous/chaos/test_lease_fencing.py tests/autonomous/chaos/test_activity_recovery.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autonomous/scheduler tests/autonomous/unit/test_scheduler.py tests/autonomous/unit/test_activities.py tests/autonomous/chaos/test_lease_fencing.py tests/autonomous/chaos/test_activity_recovery.py
git commit -m "feat(autonomous): persist leases and control activities"
```

### Task 8: Implement Model Broker, Structured Runtime, and Sandboxed Runners

**Files:**
- Create: `src/autonomous/broker/model_broker.py`
- Replace: `src/autonomous/runtime/runtime.py`
- Create: `src/autonomous/runtime/runner.py`
- Create: `src/autonomous/runtime/worker.py`
- Modify: `src/autonomous/runtime/__init__.py`
- Create: `src/autonomous/verifier/oracle_runner.py`
- Create: `tests/autonomous/unit/test_model_broker.py`
- Create: `tests/autonomous/unit/test_runtime.py`
- Create: `tests/autonomous/integration/test_runtime_brokers.py`
- Create: `tests/autonomous/security/test_runner_isolation.py`
- Create: `tests/autonomous/chaos/test_context_overflow.py`

**Interfaces:**
- Produces ModelCall ledger and `ModelBroker.call(authorization, prompt_ref)`.
- Produces `AgentRuntime.execute_attempt(attempt_id)`.
- Produces `SandboxRunner.probe()` and fixed worker/oracle invocations.

- [ ] **Step 1: Write broker bypass, timeout process-group, and context RED tests**

```python
def test_runtime_constructor_requires_model_and_tool_brokers():
    with pytest.raises(TypeError):
        AgentRuntime(model_fn=fake_model, tool_executor=fake_tool)


def test_oracle_timeout_kills_descendants(runner, tmp_path):
    marker = tmp_path / "escaped"
    result = runner.run(oracle_argv(marker), timeout=0.05)
    time.sleep(0.3)
    assert result.timed_out
    assert not marker.exists()
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_runtime.py tests/autonomous/security/test_runner_isolation.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement ModelCall state machine**

Reserve max cost, stage dispatch, record active dispatch, reject stale response generations, settle provider usage, and conservatively settle unknown billing.

- [ ] **Step 4: Implement Runtime turn protocol**

Input/output/checkpoint blobs use BlobStore and Journal references. Schema-invalid output fails closed. SUBMIT_OUTPUT emits verification activity; REPLAN_REQUEST emits replan and never reports completion.

- [ ] **Step 5: Implement and probe runner isolation**

Use fixed executables, process groups, rlimits, empty HOME, allowlisted env, read-only baseline, writable overlay, no network via bubblewrap where supported, and close inherited FDs. Probe failure updates the safety gate and forces Assist.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/unit/test_model_broker.py tests/autonomous/unit/test_runtime.py tests/autonomous/integration/test_runtime_brokers.py tests/autonomous/security/test_runner_isolation.py tests/autonomous/chaos/test_context_overflow.py -q`

Expected: PASS; local platform may mark write-mode sandbox attestation false, but tests prove fail-closed derivation.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/broker/model_broker.py src/autonomous/runtime src/autonomous/verifier/oracle_runner.py tests/autonomous/unit/test_model_broker.py tests/autonomous/unit/test_runtime.py tests/autonomous/integration/test_runtime_brokers.py tests/autonomous/security/test_runner_isolation.py tests/autonomous/chaos/test_context_overflow.py
git commit -m "feat(autonomous): add brokered runtime and sandbox runners"
```

### Task 9: Implement Independent Verification, Finalization, and Durable Reports

**Files:**
- Replace: `src/autonomous/verifier/verifier.py`
- Replace: `src/autonomous/reporter/reporter.py`
- Create: `src/autonomous/reporter/finalization.py`
- Modify: `src/autonomous/reporter/__init__.py`
- Create: `tests/autonomous/unit/test_verifier.py`
- Create: `tests/autonomous/unit/test_finalization.py`
- Create: `tests/autonomous/unit/test_reporter_outbox.py`
- Create: `tests/autonomous/integration/test_outbox_recovery.py`
- Create: `tests/autonomous/security/test_oracle_tampering.py`

**Interfaces:**
- Produces deterministic Command/Resource/Data/Schema/Review/Human Oracles.
- Produces signed `VerificationAttestation`.
- Produces `FinalizationService.request_terminal(run_id, state)`.
- Produces durable Outbox delivery/retry/dead-letter.

- [ ] **Step 1: Write Oracle tampering, terminal guard, and restart RED tests**

```python
def test_executor_cannot_replace_frozen_oracle_baseline(system):
    artifact = malicious_artifact_that_rewrites_tests()
    attestation = system.verifier.verify(artifact, frozen_criterion())
    assert attestation.result is VerificationResult.EXECUTION_DEFECT


def test_terminal_and_report_delivery_share_frame(system):
    sequence = system.finalization.request_terminal(run_id, RunState.SUCCEEDED)
    frame = system.journal.frame(sequence)
    assert event_types(frame) >= {"run.succeeded", "report.created", "outbox.created"}
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_verifier.py tests/autonomous/unit/test_finalization.py tests/autonomous/unit/test_reporter_outbox.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement criterion compiler and sufficiency review**

Free text cannot automatically pass. Insufficient Oracles become Human. R2+, executable, and security criteria require deterministic Oracle evidence.

- [ ] **Step 4: Implement Finalization Saga**

Require a disposition for every committed Effect and zero unresolved Effects. Compensation is its own Effect. Accepted abandonment creates resource quarantine.

- [ ] **Step 5: Implement durable Outbox**

Persist destination Principal, payload hash/BlobRef, provider idempotency key and receipt, state, retry deadline, alternate channel, and dead letter. Recovery resumes pending deliveries.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/unit/test_verifier.py tests/autonomous/unit/test_finalization.py tests/autonomous/unit/test_reporter_outbox.py tests/autonomous/integration/test_outbox_recovery.py tests/autonomous/security/test_oracle_tampering.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/verifier src/autonomous/reporter tests/autonomous/unit/test_verifier.py tests/autonomous/unit/test_finalization.py tests/autonomous/unit/test_reporter_outbox.py tests/autonomous/integration/test_outbox_recovery.py tests/autonomous/security/test_oracle_tampering.py
git commit -m "feat(autonomous): verify and finalize runs durably"
```

### Task 10: Implement Supervisor Recovery, Kill, and Cleanup

**Files:**
- Replace: `src/autonomous/supervisor/supervisor.py`
- Replace: `src/autonomous/supervisor/reconciler.py`
- Create: `src/autonomous/supervisor/cleanup.py`
- Replace: `src/autonomous/policy/kill_switch.py`
- Create: `scripts/ghostap_stop.py`
- Modify: `restart.sh`
- Create: `tests/autonomous/unit/test_supervisor.py`
- Create: `tests/autonomous/unit/test_reconciler.py`
- Create: `tests/autonomous/chaos/test_supervisor_recovery.py`
- Create: `tests/autonomous/chaos/test_process_external_kill.py`
- Create: `tests/autonomous/chaos/test_cleanup_recovery.py`

**Interfaces:**
- Produces `AutonomousSupervisor.start/stop/status`.
- Produces `Reconciler.run_once`.
- Produces process-external `uv run python scripts/ghostap_stop.py`.

- [ ] **Step 1: Write restart and active-dispatch Kill RED tests**

```python
def test_supervisor_restart_rebuilds_and_leases_within_60_seconds(harness):
    harness.kill9()
    status = harness.restart()
    assert status.first_legal_lease_at - status.started_at <= 60


def test_external_stop_persists_when_supervisor_event_loop_hangs(harness):
    harness.hang_supervisor_loop()
    harness.run_ghostap_stop("--all")
    assert harness.normal_dispatch_gate_closed()
    assert harness.restart_mode() is EffectiveAutonomy.ASSIST
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/chaos/test_supervisor_recovery.py tests/autonomous/chaos/test_process_external_kill.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement startup state machine**

Use the exact ten-step recovery order from the design. Preserve read-only status and emergency Kill queries when Journal or disk is unhealthy.

- [ ] **Step 4: Implement process supervision**

Store process handles, drain output, use process groups, wait on termination, enforce heartbeat/resource limits, and translate dead workers to orphan Attempts.

- [ ] **Step 5: Implement process-external stop and cleanup**

Close the latch before network actions, disable service relaunch, terminate process groups, persist cleanup queue through fenced writer takeover, and resume the same Cleanup Effect after crash.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/unit/test_supervisor.py tests/autonomous/unit/test_reconciler.py tests/autonomous/chaos/test_supervisor_recovery.py tests/autonomous/chaos/test_process_external_kill.py tests/autonomous/chaos/test_cleanup_recovery.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/supervisor src/autonomous/policy/kill_switch.py scripts/ghostap_stop.py restart.sh tests/autonomous/unit/test_supervisor.py tests/autonomous/unit/test_reconciler.py tests/autonomous/chaos/test_supervisor_recovery.py tests/autonomous/chaos/test_process_external_kill.py tests/autonomous/chaos/test_cleanup_recovery.py
git commit -m "feat(autonomous): supervise recovery and emergency stop"
```

### Task 11: Implement Planner, Compiler, and One-Shot Coordinator

**Files:**
- Create: `src/autonomous/planner.py`
- Replace: `src/autonomous/manager/plan_compiler.py`
- Create: `src/autonomous/coordinator.py`
- Create: `src/autonomous/runtime/capabilities.py`
- Create: `tests/autonomous/unit/test_planner.py`
- Create: `tests/autonomous/unit/test_plan_compiler.py`
- Create: `tests/autonomous/integration/test_one_shot_pipeline.py`
- Create: `tests/autonomous/chaos/test_one_shot_lifecycle_kill_matrix.py`

**Interfaces:**
- Produces `Planner.define_goal` and `Planner.create_plan`.
- Produces `PlanCompiler.compile(plan, authorization_envelope, effect_ledger)`.
- Produces `AutonomousCoordinator.submit/activate/run/revise/retry`.

- [ ] **Step 1: Write real lifecycle RED test**

```python
@pytest.mark.integration
def test_manager_one_shot_runs_through_all_layers(system):
    goal = system.coordinator.submit(goal_request("create hello.txt and verify it"))
    system.coordinator.activate(goal.goal_id, approved_activation(goal))
    terminal = system.run_until_terminal(goal.goal_id)
    assert terminal.state is RunState.SUCCEEDED
    assert terminal.verification.all_passed
    assert terminal.report.delivery_state is DeliveryState.DELIVERED
    assert system.effects.physical_send_count == 1
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/integration/test_one_shot_pipeline.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement Planner and compiler**

Goal Define generates deliverables, scope, constraints, deadline, criterion IDs/hashes, data/tool envelope, budget, risk, and notification policy. Compiler validates every v5 Plan rule and semantic Effect reuse/repeat.

- [ ] **Step 4: Implement coordinator**

Drive Observe/Define/Plan/Act/Verify/Report through durable Activities. Failures retry, replan, block with DecisionRequest, or terminate with evidence. Goal revision never overwrites history.

- [ ] **Step 5: Add lifecycle crash matrix**

Crash after every fsync boundary from ingress to delivery. Restart must retain accepted work, reject stale leases, avoid duplicate Effects, and eventually report.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/unit/test_planner.py tests/autonomous/unit/test_plan_compiler.py tests/autonomous/integration/test_one_shot_pipeline.py tests/autonomous/chaos/test_one_shot_lifecycle_kill_matrix.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/planner.py src/autonomous/manager/plan_compiler.py src/autonomous/coordinator.py src/autonomous/runtime/capabilities.py tests/autonomous/unit/test_planner.py tests/autonomous/unit/test_plan_compiler.py tests/autonomous/integration/test_one_shot_pipeline.py tests/autonomous/chaos/test_one_shot_lifecycle_kill_matrix.py
git commit -m "feat(autonomous): run one-shot goals end to end"
```

### Task 12: Implement Manager Commands, Cards, and Production Wiring

**Files:**
- Replace: `src/autonomous/manager/handler.py`
- Create: `src/autonomous/manager/cards.py`
- Create: `src/autonomous/manager/feishu_adapter.py`
- Create: `src/autonomous/bootstrap.py`
- Modify: `src/main.py`
- Modify: `src/feishu/handler_context.py`
- Modify: `src/feishu/handlers/__init__.py`
- Create: `src/feishu/handlers/autonomous.py`
- Modify: `src/feishu/router.py`
- Modify: `src/feishu/dispatcher.py`
- Modify: `src/feishu/action_registry.py`
- Modify: `src/card/actions/dispatch.py`
- Modify: `src/feishu/ws_client.py`
- Create: `ux/autonomous_manager_cards.html`
- Create: `tests/autonomous/contract/test_manager_command_surface.py`
- Create: `tests/autonomous/integration/test_feishu_manager_routes.py`
- Create: `tests/autonomous/integration/test_manager_pending_center.py`

**Interfaces:**
- Produces Manager command namespace and canonical cards.
- Produces `AutonomousRuntimeContainer` injected into HandlerContext.
- Consumes coordinator, control gate, Journal projections, and durable outbox.

- [ ] **Step 1: Create and review the HTML preview**

Preview create/activation, progress, approval, decision, final report, failed delivery, and reconciliation cards. Production renderers must match approved hierarchy and use existing card pipeline boundaries.

- [ ] **Step 2: Write route and command RED tests**

```python
def test_goal_command_reaches_autonomous_handler(ws_client):
    ws_client.process_message("/goal create improve reliability")
    ws_client._autonomous_handler.handle_command.assert_called_once()
    ws_client._slock_handler.handle_slock_command.assert_not_called()


def test_manager_surface_has_all_normal_operations(handler):
    assert REQUIRED_COMMANDS <= set(handler.command_names)
    assert not handler.has_placeholder_commands()
```

- [ ] **Step 3: Verify RED**

Run: `uv run pytest tests/autonomous/contract/test_manager_command_surface.py tests/autonomous/integration/test_feishu_manager_routes.py -q`

Expected: FAIL.

- [ ] **Step 4: Implement command use cases and cards**

Support create/modify/activate/pause/resume/cancel/health, run start/list/show/retry/cancel, approvals, decisions, reports, employee lifecycle, evidence, accept/reject/rework, compensate, reconcile, and stale-card recovery.

- [ ] **Step 5: Wire production composition**

`Application` starts AutonomousSupervisor before Feishu; HandlerContext receives the container; Feishu callbacks synchronously persist durable ingress before acknowledgement; shutdown closes the container after channel stop.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/contract/test_manager_command_surface.py tests/autonomous/integration/test_feishu_manager_routes.py tests/autonomous/integration/test_manager_pending_center.py tests/test_feishu_dispatcher.py tests/test_ws_client_routing.py tests/test_action_dispatch_mapping.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/manager src/autonomous/bootstrap.py src/main.py src/feishu src/card/actions/dispatch.py ux/autonomous_manager_cards.html tests/autonomous/contract/test_manager_command_surface.py tests/autonomous/integration/test_feishu_manager_routes.py tests/autonomous/integration/test_manager_pending_center.py
git commit -m "feat(feishu): wire autonomous Manager control plane"
```

### Task 13: Migrate Slock Data and Convert Legacy Routes to Compatibility Projections

**Files:**
- Create: `src/autonomous/migration/slock_importer.py`
- Create: `src/autonomous/migration/slock_compat.py`
- Create: `src/autonomous/migration/__init__.py`
- Modify: `src/slock_engine/gateway.py`
- Modify: `src/slock_engine/manager.py`
- Modify: `src/slock_engine/collaboration_orchestrator.py`
- Modify: `src/slock_engine/slash_commands.py`
- Modify: `src/feishu/slock_dispatch.py`
- Modify: `src/feishu/handlers/slock.py`
- Modify: `src/feishu/dispatcher.py`
- Modify: `src/feishu/ws_client.py`
- Create: `tests/autonomous/integration/test_slock_importer.py`
- Create: `tests/autonomous/integration/test_slock_compat_routes.py`
- Create: `tests/autonomous/integration/test_no_dual_fact_source.py`
- Modify: `tests/test_slock_task_chain.py`

**Interfaces:**
- Produces idempotent `SlockImporter.scan/plan/apply/verify`.
- Produces read-only legacy command translations.
- Consumes Manager coordinator and projections.

- [ ] **Step 1: Write dry-run, idempotency, and no-dual-write RED tests**

```python
def test_import_twice_creates_one_employee_and_goal(importer, legacy_fixture):
    first = importer.apply(importer.plan(legacy_fixture))
    second = importer.apply(importer.plan(legacy_fixture))
    assert first.created_count > 0
    assert second.created_count == 0
    assert importer.verify().hashes_match


def test_manager_only_mode_never_writes_legacy_task_board(system):
    system.send_legacy("/task list")
    system.send_legacy("/slock create report")
    assert legacy_write_log(system) == []
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/integration/test_slock_importer.py tests/autonomous/integration/test_no_dual_fact_source.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement importer**

Map groups, agents, task boards, plans, discussions, decisions, and read-only memory with legacy IDs, schema version, source hashes/counts, Journal migration events, dry-run, and verification report.

- [ ] **Step 4: Implement compatibility modes**

Legacy remains unchanged in `legacy`; `shadow_read` compares projections; `manager_only` forwards `/slock <goal>` and reads status aliases from Journal; `disabled` removes passive auto-activation.

- [ ] **Step 5: Fix legacy correctness before reuse**

`TIMED_OUT` and `SKIPPED` do not satisfy successful DAG dependencies. Add regression assertions.

- [ ] **Step 6: Verify**

Run: `uv run pytest tests/autonomous/integration/test_slock_importer.py tests/autonomous/integration/test_slock_compat_routes.py tests/autonomous/integration/test_no_dual_fact_source.py tests/test_slock_task_chain.py tests/test_slock_runtime_restore.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/migration src/slock_engine src/feishu/slock_dispatch.py src/feishu/handlers/slock.py src/feishu/dispatcher.py src/feishu/ws_client.py tests/autonomous/integration/test_slock_importer.py tests/autonomous/integration/test_slock_compat_routes.py tests/autonomous/integration/test_no_dual_fact_source.py tests/test_slock_task_chain.py
git commit -m "refactor(slock): migrate execution to autonomous kernel"
```

### Task 14: Implement Employee Lifecycle and Multi-Worker Collaboration

**Files:**
- Create: `src/autonomous/employees.py`
- Create: `src/autonomous/collaboration.py`
- Modify: `src/autonomous/planner.py`
- Modify: `src/autonomous/coordinator.py`
- Modify: `src/autonomous/supervisor/supervisor.py`
- Create: `tests/autonomous/unit/test_employee_lifecycle.py`
- Create: `tests/autonomous/unit/test_collaboration_planner.py`
- Create: `tests/autonomous/integration/test_multi_worker_manager_only.py`
- Create: `tests/autonomous/chaos/test_worker_failure_reassignment.py`

**Interfaces:**
- Produces Logical/Visible/Ephemeral Employee definitions and Sagas.
- Produces multi-Step assignment/review/verification separation.
- Consumes durable scheduler and worker supervisor.

- [ ] **Step 1: Write lifecycle and Manager-only multi-worker RED tests**

```python
def test_logical_employee_is_default_and_uses_no_bot_quota(system):
    employee = system.employees.hire(template="coder")
    assert employee.worker_type is WorkerType.LOGICAL
    assert employee.bot_principal_id is None


def test_user_interacts_only_with_manager_during_multi_worker_run(system):
    result = system.manager.submit(complex_goal())
    assert result.worker_count >= 2
    assert set(result.user_conversations) == {"manager"}
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_employee_lifecycle.py tests/autonomous/integration/test_multi_worker_manager_only.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement durable lifecycle Sagas**

Provisioning records orphan apps and secret failures; retirement blocks assignment, drains Runs, revokes grants, removes channels/groups, rotates credentials, and archives governed memory/audit.

- [ ] **Step 4: Implement collaboration**

Planner assigns Employees by capability/grant/budget. Reviewer/Verifier identities differ from executor. Mirror groups receive events only.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/autonomous/unit/test_employee_lifecycle.py tests/autonomous/unit/test_collaboration_planner.py tests/autonomous/integration/test_multi_worker_manager_only.py tests/autonomous/chaos/test_worker_failure_reassignment.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autonomous/employees.py src/autonomous/collaboration.py src/autonomous/planner.py src/autonomous/coordinator.py src/autonomous/supervisor/supervisor.py tests/autonomous/unit/test_employee_lifecycle.py tests/autonomous/unit/test_collaboration_planner.py tests/autonomous/integration/test_multi_worker_manager_only.py tests/autonomous/chaos/test_worker_failure_reassignment.py
git commit -m "feat(autonomous): add durable employee collaboration"
```

### Task 15: Implement Scheduled and Standing Goals

**Files:**
- Create: `src/autonomous/scheduler/triggers.py`
- Create: `src/autonomous/scheduler/health.py`
- Modify: `src/autonomous/manager/admission.py`
- Modify: `src/autonomous/coordinator.py`
- Modify: `src/autonomous/manager/handler.py`
- Create: `tests/autonomous/unit/test_trigger_contracts.py`
- Create: `tests/autonomous/unit/test_schedule_cursor.py`
- Create: `tests/autonomous/integration/test_scheduled_goals.py`
- Create: `tests/autonomous/integration/test_standing_goals.py`
- Create: `tests/autonomous/chaos/test_trigger_atomicity.py`
- Create: `tests/autonomous/chaos/test_trigger_flood.py`
- Create: `tests/autonomous/chaos/test_trigger_gap.py`

**Interfaces:**
- Produces TriggerAdapter contract, deterministic occurrence keys, cursor/watermark.
- Produces GoalHealthSnapshot and gap DecisionRequests.
- Consumes Admission limits, policy, and Scheduler.

- [ ] **Step 1: Write atomicity, replay, and flood RED tests**

```python
def test_trigger_replay_100_times_creates_one_run_and_effect(system):
    for _ in range(100):
        system.triggers.deliver(event("stable-id"))
    assert system.runs.count() == 1
    assert system.effects.physical_send_count == 1


def test_trigger_flood_never_exceeds_goal_queue_limit(system):
    system.triggers.deliver_many(10_000)
    assert system.runs.queued_count <= system.settings.autonomous_goal_queue_limit
    assert system.goal.state in {GoalState.ACTIVE, GoalState.PAUSED}
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/chaos/test_trigger_atomicity.py tests/autonomous/chaos/test_trigger_flood.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement eligible trigger contracts**

Reject sources lacking durable ACK or authoritative replay. Persist desired/observed subscription, cursor, watermark, heartbeat, gap and recovery window.

- [ ] **Step 4: Implement policies and limits**

Support misfire run-all/skip/latest, overlap forbid/queue/parallel, queued/hourly/daily/misfire/failure/disk limits, proposal merge/cooldown, notification caps, and source degradation.

- [ ] **Step 5: Verify**

Run: `uv run pytest tests/autonomous/unit/test_trigger_contracts.py tests/autonomous/unit/test_schedule_cursor.py tests/autonomous/integration/test_scheduled_goals.py tests/autonomous/integration/test_standing_goals.py tests/autonomous/chaos/test_trigger_atomicity.py tests/autonomous/chaos/test_trigger_flood.py tests/autonomous/chaos/test_trigger_gap.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autonomous/scheduler/triggers.py src/autonomous/scheduler/health.py src/autonomous/manager/admission.py src/autonomous/coordinator.py src/autonomous/manager/handler.py tests/autonomous/unit/test_trigger_contracts.py tests/autonomous/unit/test_schedule_cursor.py tests/autonomous/integration/test_scheduled_goals.py tests/autonomous/integration/test_standing_goals.py tests/autonomous/chaos/test_trigger_atomicity.py tests/autonomous/chaos/test_trigger_flood.py tests/autonomous/chaos/test_trigger_gap.py
git commit -m "feat(autonomous): run scheduled and standing goals"
```

### Task 16: Implement Feishu Enhancement Boundaries and Feature Hiding

**Files:**
- Create: `src/autonomous/feishu/provisioning.py`
- Create: `src/autonomous/feishu/comments.py`
- Create: `src/autonomous/feishu/meetings.py`
- Create: `src/autonomous/feishu/mirrors.py`
- Create: `src/autonomous/feishu/__init__.py`
- Modify: `src/autonomous/employees.py`
- Modify: `src/autonomous/manager/cards.py`
- Create: `tests/autonomous/unit/test_feishu_capability_visibility.py`
- Create: `tests/autonomous/integration/test_visible_employee_provisioning.py`
- Create: `tests/autonomous/integration/test_collaboration_mirror.py`

**Interfaces:**
- Produces adapters for visible app provisioning, comments, mirror groups, and meetings.
- Consumes Capability Registry, policy, Employee Saga, and Reporter.

- [ ] **Step 1: Write VC hiding and capacity RED tests**

```python
def test_meeting_entry_hidden_when_gray_probe_fails(manager_ui):
    manager_ui.capabilities.meeting_join = unavailable("ErrNotInGray")
    assert "meeting" not in manager_ui.available_actions()


def test_visible_employee_capacity_is_configured_not_hard_coded(system):
    system.settings.autonomous_visible_employee_limit = 2
    system.employees.hire(visible=True)
    system.employees.hire(visible=True)
    with pytest.raises(CapacityExceeded):
        system.employees.hire(visible=True)
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/unit/test_feishu_capability_visibility.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement adapters**

Use locked official APIs, typed capability discovery, R3 meeting approval, durable provisioning/compensation, no stored plaintext secret, and mirror-only event projection.

- [ ] **Step 4: Verify**

Run: `uv run pytest tests/autonomous/unit/test_feishu_capability_visibility.py tests/autonomous/integration/test_visible_employee_provisioning.py tests/autonomous/integration/test_collaboration_mirror.py -q`

Expected: PASS; real tenant probes are recorded separately and unavailable features remain hidden.

- [ ] **Step 5: Commit**

```bash
git add src/autonomous/feishu src/autonomous/employees.py src/autonomous/manager/cards.py tests/autonomous/unit/test_feishu_capability_visibility.py tests/autonomous/integration/test_visible_employee_provisioning.py tests/autonomous/integration/test_collaboration_mirror.py
git commit -m "feat(autonomous): add gated Feishu enhancements"
```

### Task 17: Build Acceptance, Metrics, Chaos, and Security Gates

**Files:**
- Create: `src/autonomous/acceptance/metrics.py`
- Create: `src/autonomous/acceptance/evidence.py`
- Create: `scripts/autonomous_acceptance.py`
- Create: `tests/autonomous/contract/test_metric_contract.py`
- Create: `tests/autonomous/acceptance/test_manifest_coverage.py`
- Create: `tests/autonomous/acceptance/test_public_one_shot_benchmark.py`
- Create: `tests/autonomous/acceptance/test_scheduled_benchmark.py`
- Create: `tests/autonomous/acceptance/test_standing_benchmark.py`
- Create: `tests/autonomous/security/test_prompt_injection.py`
- Create: `tests/autonomous/security/test_memory_disabled.py`
- Create: `tests/autonomous/security/test_disk_and_backup_gates.py`
- Create: `tests/autonomous/acceptance/manifests/public-one-shot.json`
- Create: `tests/autonomous/acceptance/manifests/scheduled.json`
- Create: `tests/autonomous/acceptance/manifests/standing.json`
- Create: `tests/autonomous/acceptance/manifests/hidden.lock.json`

**Interfaces:**
- Produces metric calculations and immutable run evidence.
- Produces `uv run python scripts/autonomous_acceptance.py evaluate`.
- Consumes all test selectors and section-18 thresholds.

- [ ] **Step 1: Write metric and coverage RED tests**

```python
def test_wilson_gate_uses_lower_bound():
    result = one_shot_success_gate(successes=90, total=100)
    assert result.point_estimate >= 0.90
    assert result.wilson_lower_bound >= 0.85


def test_every_manifest_gate_has_evidence_or_explicit_pending_reason(manifest):
    for gate in manifest.gates:
        assert gate.evidence or gate.pending_reason
```

- [ ] **Step 2: Verify RED**

Run: `uv run pytest tests/autonomous/contract/test_metric_contract.py tests/autonomous/acceptance/test_manifest_coverage.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement calculations and evidence**

Implement nearest-rank P95/P99 with minimum 100 samples, Wilson lower bound, zero-event 95% upper bound, solvable/BLOCKED/Human denominators, RTO definition, and signed evidence metadata.

- [ ] **Step 4: Implement acceptance CLI**

Commands list gates, run local selectors, import real tenant/soak artifacts, evaluate thresholds, emit JSON/Markdown reports, and output `WRITE_DISABLED_REQUIRED=true` when any shared-kernel gate fails.

- [ ] **Step 5: Add task-set manifests**

Public One-shot covers five domains and difficulty layers. Scheduled declares 30 goals × 30 occurrences. Standing declares 1,000 events with at least 50% negatives. Hidden manifest stores only hashes/owners.

- [ ] **Step 6: Verify local gates**

Run: `uv run pytest tests/autonomous/contract tests/autonomous/unit tests/autonomous/integration tests/autonomous/chaos tests/autonomous/security -q`

Expected: PASS for local gates. Tenant and 30-day gates remain `pending` until real evidence exists and cannot be reported as passed.

- [ ] **Step 7: Commit**

```bash
git add src/autonomous/acceptance scripts/autonomous_acceptance.py tests/autonomous
git commit -m "test(autonomous): enforce v5 acceptance and safety gates"
```

### Task 18: Retire the Prototype, Update Documentation, and Perform Completion Audit

**Files:**
- Delete: `tests/test_autonomous.py`
- Modify: `src/autonomous/__init__.py`
- Modify: `.Memory/2026-07-10.md`
- Modify: `.Memory/Abstract.md`
- Modify: `AGENTS.md` only if new persistent failure-derived rules are required
- Create: `docs/autonomous-v5-operations.md`
- Create: `docs/autonomous-v5-migration.md`
- Create: `docs/autonomous-v5-acceptance.md`
- Modify: `README.md`
- Modify: `.superpowers/sdd/progress.md`

**Interfaces:**
- Produces operations, migration, gate, rollback, and evidence documentation.
- Removes the misleading 42-test monolith after focused suites cover all contracts.

- [ ] **Step 1: Verify no prototype-only production gaps remain**

Run:

```bash
rg -n "No pending decisions|write_event\\(|model_fn: Callable|tool_executor: Callable" src/autonomous
rg -n "from src\\.autonomous|autonomous" src/main.py src/feishu src/slock_engine
```

Expected: no placeholders or fake Journal interface; production references exist.

- [ ] **Step 2: Run formatting and focused tests**

Run:

```bash
uv run ruff check src/autonomous tests/autonomous
uv run pytest tests/autonomous -q
uv run python -m src.main --validate
```

Expected: PASS.

- [ ] **Step 3: Run shared routing and Slock migration regression**

Run:

```bash
uv run pytest \
  tests/test_feishu_dispatcher.py \
  tests/test_ws_client_routing.py \
  tests/test_action_dispatch_mapping.py \
  tests/test_slock_task_chain.py \
  tests/test_slock_runtime_restore.py \
  tests/test_project_isolation.py -q
```

Expected: PASS.

- [ ] **Step 4: Run the full repository suite**

Run: `uv run pytest tests/ -q`

Expected: zero failures. Any unrelated pre-existing failure is recorded with proof and is not silently fixed.

- [ ] **Step 5: Generate the prompt-to-artifact completion report**

Run: `uv run python scripts/autonomous_acceptance.py evaluate --output docs/autonomous-v5-acceptance.md`

The report maps every v5 phase, requirement, command, gate, and deliverable to files, tests, tenant artifacts, or an explicit pending environment/soak requirement. No proxy green status is accepted.

- [ ] **Step 6: Run two stateless mloop review rounds**

Each round gives only Goal Snapshot plus current repository state to Product, Architecture, Engineering, QA/Security, and UX roles. Any material suggestion resets clean rounds and is fixed/tested before re-review.

- [ ] **Step 7: Update project memory accurately**

Replace the premature “complete implementation/42 tests” entry with actual changes, reasons, commands, counts, and remaining tenant/soak risks. Add a concise Abstract line.

- [ ] **Step 8: Commit final docs and cleanup**

```bash
git add -A
git commit -m "docs(autonomous): document v5 operations and evidence"
```

- [ ] **Step 9: Verify branch and push**

```bash
git status --short
git log --oneline origin/dev..HEAD
git push origin dev
git status --short --branch
```

Expected: push succeeds, branch tracks `origin/dev`, and the worktree is clean.

---

## Plan Self-Review

### Spec Coverage

- Phase 0: Tasks 1, 2, 8, 10, 16, 17.
- Phase 1: Tasks 2-12.
- Phase 2: Tasks 13-14.
- Phase 3: Task 15.
- Phase 4: Task 16.
- Manager-only operations: Task 12.
- Slock migration/retirement: Task 13.
- All 77 section-18 gates and metrics: Tasks 1 and 17.
- Final prompt-to-artifact completion audit: Task 18.

### Placeholder Scan

The plan contains no TBD/TODO/“implement later” steps. Environment-dependent tenant and 30-day evidence is explicitly modeled as pending evidence, not silently counted as passed.

### Type Consistency

- Journal uses `JournalWriter.commit`.
- All state reads use `ProjectionRepository`.
- All model/tool execution uses `ModelBroker`/`ToolBroker`.
- All external writes use `DispatchGate`.
- Terminal transitions use `FinalizationService`.
- Manager and compatibility routes use `AutonomousCoordinator`.
