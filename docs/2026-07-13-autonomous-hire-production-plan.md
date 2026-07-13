# Autonomous Visible Employee Hire Production Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use subagent-driven development with test-driven development; every production behavior starts with a focused failing test and ends with fresh verification.

**Goal:** Implement the in-process production `/hire` control plane: an anchored Journal/Vault Saga, one fresh-interpreter Channel child per employee, exact Slash reconciliation, restricted activation verification, restart recovery, and a fail-closed real-tenant acceptance gate.

**Architecture:** Journal remains the only lifecycle fact source and CredentialVault remains the only secret store. `ProductionEmployeeHireService` durably admits a stable intent before an asynchronous activity invokes official Lark SDKs; every external step records anchored PREPARED and EXECUTING frames. Channel transport alone runs in one fresh interpreter per employee; Slash and verification run through typed ports in the main service. Production composition is available but refuses admission unless all local readiness checks and signed tenant evidence pass.

**Tech Stack:** Python 3.13, `uv`, pytest, `lark-oapi==1.7.1`, `lark-channel-sdk==1.1.0`, AES-GCM CredentialVault, authenticated Journal frames.

## Goal Snapshot

- Goal: make the real `/hire` path recoverable and production-shaped inside GhostAP without a separate service.
- Success criteria: stable durable intent, Vault-only secret, recoverable external effects, unique employee child process, exact Slash desired state, real employee `/status` round trip before ACTIVE, and an executable tenant acceptance workflow.
- Constraints: keep `autonomous_visible_employee_limit=0`; never claim Agent Department production readiness without tenant evidence; use official Lark SDKs; never put secrets in argv, env, Journal, logs, cards, or projections.
- Non-goals: raising the visible limit, claiming 1/10/50 Bot soak results without running them, replacing the main Bot WebSocket, or completing unrelated `/fire`, durable task inbox/outbox, and Thread Context work.

## Global Constraints

- Use `uv`; never pip or conda.
- Journal is the only employee/Saga fact source; all state objects are frozen and reconstructed by replay.
- PREPARED and EXECUTING frames must both be fsynced and anchored before an external call.
- Unknown non-queryable app-registration outcomes enter ACTION_REQUIRED and are never blindly retried.
- Credential material is accepted only by CredentialVault and a one-shot Channel bootstrap pipe.
- One employee has at most one live Channel generation and one fresh-interpreter child.
- Slash reconciliation is GET, diff, POST/PATCH/DELETE, GET, exact verification.
- READY_PENDING_VERIFICATION accepts only the bound requester in employee DM with a single-use nonce and exact `/status`.
- `autonomous_visible_employee_limit` remains `0`; local and tenant evidence may not silently override it.

## Task 1: Durable hire aggregate, anchor, and production service

**Files:**
- Create: `src/autonomous/provisioning/hire_state.py`
- Create: `src/autonomous/provisioning/hire_service.py`
- Modify: `src/autonomous/provisioning/hire_port.py`
- Modify: `src/autonomous/journal/anchor.py`
- Modify: `src/config/settings.py`
- Test: `tests/autonomous/unit/test_employee_hire_service.py`
- Test: `tests/autonomous/chaos/test_hire_saga_recovery.py`

**Interfaces:**
- Produce `DurableHireState`, `HirePhase`, `HireEffectState`, and `HireProjection.rebuild(frames)`.
- Produce `ProductionEmployeeHireService.start_hire(request)`, `recover()`, `readiness()`, and `close()`.
- Produce a file-backed CAS anchor and strict, redacted Journal HMAC settings; MemoryAnchor remains test/offline only.
- Extend `EmployeeHireRequest` with authoritative `tenant_key` and frozen role/profile/persona data while keeping handler callers explicit.

- [ ] Add RED tests proving limit zero, absent release evidence, absent keyring, and absent real anchor reject before SDK calls.
- [ ] Add RED tests proving stable tenant/message intent IDs, name reservation, repeated admission idempotency, and anchored return.
- [ ] Add RED tests for PREPARED/EXECUTING ordering, Vault-before-binding, deterministic orphan receipt adoption, and secret scanning.
- [ ] Implement frozen replay reducers and a serialized commit boundary that keeps the canonical workforce projection cursor current.
- [ ] Implement registration callback bridging, Journal/Vault steps, ACTION_REQUIRED recovery, and bounded async lifecycle.
- [ ] Run the new unit/chaos files plus Journal, Vault, and employee projection regressions.

## Task 2: Fresh-interpreter employee Channel supervisor

**Files:**
- Create: `src/autonomous/provisioning/channel_protocol.py`
- Create: `src/autonomous/provisioning/channel_worker.py`
- Create: `src/autonomous/supervisor/employee_channels.py`
- Test: `tests/autonomous/contract/test_employee_channel_contract.py`
- Test: `tests/autonomous/integration/test_employee_channel_process.py`
- Test: `tests/autonomous/security/test_employee_channel_isolation.py`

**Interfaces:**
- Produce `EmployeeChannelSupervisor.start(agent_id, app_id, credential_ref, generation, on_event) -> ChannelProcessStatus`.
- Produce `stop(agent_id)`, `recover(desired)`, `status(agent_id)`, and `close()`.
- Child emits versioned NDJSON READY/EVENT/HEALTH/ERROR frames and consumes STOP/SEND frames; stale generations are rejected in the parent.

- [ ] Add RED contract tests for `sys.executable` fresh exec, no secret in argv/env, explicit FD allowlist, and SDK event handler signatures.
- [ ] Add RED process tests for distinct PIDs, one-shot credential transfer, READY timeout, clean stop, crash detection, and generation fencing.
- [ ] Implement the child worker using `lark_channel.FeishuChannel.connect_until_ready()` and synchronous non-blocking reconnect shims.
- [ ] Implement parent process ownership, pipe/socket lifecycle, backoff metadata, and secret-safe status.
- [ ] Run contract, integration, security, and existing Channel tests.

## Task 3: Official SDK Slash reconciliation

**Files:**
- Modify: `src/autonomous/provisioning/slash_commands.py`
- Create: `src/autonomous/provisioning/slash_lark.py`
- Test: `tests/autonomous/contract/test_slash_lark_contract.py`
- Test: `tests/autonomous/integration/test_slash_reconciliation.py`

**Interfaces:**
- Produce `SlashCommandReconciler.reconcile() -> VerifiedSlashState` with canonical spec/observed hashes.
- Produce `LarkSlashCommandAPI` using `lark.Client.arequest(BaseRequest)` and tenant access tokens owned by the employee app.

- [ ] Add RED tests for canonical names, description/usage drift PATCH, missing POST, extra DELETE, final GET mismatch, second-run zero mutation, and redacted failures.
- [ ] Add RED SDK request tests fixing method, URI, token type, path/body, and strict response decoding.
- [ ] Implement GET/diff/mutate/GET verification and stable desired/observed hashes.
- [ ] Run new Slash tests plus existing Slash/Channel regression tests.

## Task 4: Verification router and Saga integration

**Files:**
- Create: `src/autonomous/provisioning/verification.py`
- Modify: `src/autonomous/provisioning/hire_service.py`
- Test: `tests/autonomous/integration/test_employee_activation_gate.py`
- Test: `tests/autonomous/integration/test_hire_recovery.py`

**Interfaces:**
- Produce `VerificationRouter.issue(intent_id, agent_id, requester_id, ttl)` and `handle(event) -> VerificationResult`.
- Consume verified Channel identity/readiness and verified Slash hashes from Tasks 2 and 3.

- [ ] Add RED tests for wrong principal/tenant/app/generation, non-DM, non-status, expired/replayed nonce, and ordinary task denial.
- [ ] Add RED tests proving ACTIVE is impossible without identity, Channel, Slash, employee send, and main-Bot-send-count-zero attestations.
- [ ] Implement single-use durable verification challenge and exact employee-owned status response.
- [ ] Integrate CONFIGURING, VALIDATING, READY_PENDING_VERIFICATION, ACTIVE, and ACTION_REQUIRED transitions.
- [ ] Replay crash points and run integration regressions.

## Task 5: Production composition and `/hire` wiring

**Files:**
- Create: `src/autonomous/provisioning/composition.py`
- Modify: `src/feishu/handler_context.py`
- Modify: `src/feishu/ws_client.py`
- Modify: `src/thread/manager.py`
- Modify: `src/thread/__init__.py`
- Modify: `src/tasking/scheduler.py`
- Modify: `src/feishu/handlers/slock.py`
- Test: `tests/autonomous/integration/test_employee_hire_composition.py`
- Test: `tests/test_slock_role_creation.py`

**Interfaces:**
- Produce `EmployeeDepartmentRuntime.from_settings(...)`, `hire_service`, `recover()`, `readiness()`, and `close()`.
- Carry authoritative event-header `tenant_key` through TaskSpec/ContextVar into `EmployeeHireRequest`; never trust card payload tenancy.

- [ ] Add RED tests proving production context injection when and only when gates pass, tenant propagation, recovery-before-admission, and shutdown ordering.
- [ ] Add RED tests proving default limit zero remains fail-closed and legacy/NullJournal employee paths are unreachable.
- [ ] Implement a single composition owner for Journal, projection, Vault, registrar, Channel supervisor, Slash, verification, and service.
- [ ] Wire FeishuWSClient construction/close and preserve existing `/new-role`, Deep, Spec, Worktree, Workflow, and main Bot transport behavior.
- [ ] Run targeted routing/card/config tests and all Autonomous tests.

## Task 6: Real-tenant acceptance workflow and release gate

**Files:**
- Create: `src/autonomous/acceptance/employee_release.py`
- Create: `scripts/validate_employee_tenant.py`
- Create: `tests/autonomous/acceptance/employee_release_manifest.json`
- Create: `tests/autonomous/contract/test_employee_release_gate.py`
- Create: `tests/autonomous/acceptance/test_real_tenant_employee_hire.py`

**Interfaces:**
- Produce an append-only, hash-verified evidence bundle and fail-closed evaluator for staging tenant, production tenant, identity isolation, media paths, Slash CRUD/rebuild, restart/reconnect, secret scan, and 1/10/50 Bot soak.
- Real-tenant tests skip with an explicit missing-environment reason; skipped/pending evidence never passes the release gate.

- [ ] Add RED tests proving missing, malformed, wrong-environment, stale, or partial evidence keeps release unavailable and limit zero.
- [ ] Implement the operator CLI and evidence schema without embedding credentials or tokens in artifacts.
- [ ] Add live selectors that require explicit environment opt-in and capture redacted evidence.
- [ ] Run contract tests and demonstrate the current environment remains PENDING rather than falsely PASSED.

## Task 7: Convergence, documentation, and final verification

**Files:**
- Modify: `.Memory/2026-07-13.md`
- Modify: `.Memory/Abstract.md`
- Modify: `.Memory/Backlog.md` only for non-blocking medium/low findings.

- [ ] Run focused tests after every task, then `uv run python -m pytest tests/autonomous/ -q` and scoped Ruff.
- [ ] Run config validation and `git diff --check`; run broader routing/card tests because composition touches shared startup and context.
- [ ] Perform stateless product, architecture/security, engineering, and QA reviews; fix every material issue and require two consecutive clean rounds.
- [ ] Record changes, reasons, verification evidence, remaining tenant evidence, and the unchanged visible limit in project memory.

## Parallelization and conflict boundaries

- After Task 1 freezes service/effect interfaces, Tasks 2 and 3 are independent and may run in parallel; they must not edit `hire_service.py`, settings, Feishu routing, or each other's files.
- Task 4 is serial because it consumes both Channel and Slash evidence and owns activation semantics.
- Task 5 is serial because it changes shared startup, TaskSpec, request context, and shutdown ordering.
- Task 6 can prepare its pure evidence evaluator in parallel with Task 4, but live selectors and composition integration wait for Task 5.
- Review agents are read-only unless explicitly dispatched a bounded fix; the main agent integrates shared contracts.

## Completion boundary

Code completion does not equal production release. This plan is complete only when automated implementation and recovery tests pass and the real-tenant workflow exists. Agent Department remains not production-ready, and `autonomous_visible_employee_limit` remains `0`, until staging and production tenant evidence plus the 1/10/50 Bot soak are actually recorded and verified.
