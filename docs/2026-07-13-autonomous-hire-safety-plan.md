# Autonomous `/hire` Safety and SDK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use test-driven development and run each task's focused verification before the next task.

**Goal:** Stop `/hire` from creating legacy virtual roles, add the official one-click app-registration capability, and make Agent Department readiness fail closed until concrete production dependencies exist.

**Architecture:** `/new-role` remains the Slock virtual-role command. `/hire` keeps the existing tool/model selection UX but ends at a typed employee-hire port; without an injected production department service it returns an explicit unavailable result and never mutates the legacy registry. A standalone `LarkAppRegistrar` wraps official `lark_oapi.aregister_app` with the versioned minimal manifest, while bootstrap reports only probed component readiness.

**Tech Stack:** Python 3.13, `lark-oapi==1.7.1`, pydantic settings, pytest, uv, ruff.

## Global Constraints

- Use `uv`; never pip or conda.
- Journal/Vault/Channel/Slash remain mandatory before a visible employee can become active.
- `/hire` must never fall back to `AgentRegistry.legacy()` or emit a virtual-role success.
- `app_secret` must not enter logs, exceptions, cards, Journal, or identity projections.
- `autonomous_visible_employee_limit` remains `0` until real tenant release evidence exists.
- Do not modify Deep, Spec, Worktree, Workflow, main Bot WebSocket transport, or `_run_acp_session`.

## Task 1: Official one-click registration capability

**Files:** `pyproject.toml`, `uv.lock`, `src/autonomous/provisioning/lark_app.py`, `tests/autonomous/contract/test_lark_app_registration.py`, `tests/autonomous/unit/test_lark_app_registrar.py`.

- [x] Add a RED contract requiring SDK version `1.7.1` and `app_preset/addons/create_only/app_id` in both registration signatures.
- [x] Add RED adapter tests proving exact minimal manifest, `create_only=True`, synchronous link/status callback forwarding, strict credential result validation, and secret-safe errors.
- [x] Upgrade the locked dependency and implement `LarkAppRegistrar.register()` over `aregister_app`.
- [x] Verify both new test files and scoped Ruff.

## Task 2: `/hire` routing must be truthful

**Files:** `src/card/builders/system.py`, `src/card/builder.py`, `src/feishu/handler_context.py`, `src/feishu/handlers/slock.py`, `tests/test_slock_role_creation.py`.

- [x] Add RED tests proving `global_hire` survives tool/model cards, final selection preserves it, unavailable department service fails closed, and no legacy registry mutation occurs.
- [x] Extend the tool card with opaque `value_extra`; add an optional typed `employee_hire_service` dependency.
- [x] Route parsed global hires to `employee_hire_service.start_hire(...)`; preserve `/new-role` behavior and reject missing service explicitly.
- [x] Verify Slock role, model cascade, action mapping, and card builder regressions.

## Task 3: Truthful department readiness

**Files:** `src/autonomous/provisioning/bootstrap.py`, `tests/autonomous/unit/test_fire_and_bootstrap.py`.

- [x] Add RED tests proving dormant mode is not healthy and nonzero limits cannot become healthy without all injected component probes.
- [x] Replace constant readiness assignment with named probe results; collect redacted error categories and expose a dormant state.
- [x] Verify bootstrap tests, all Autonomous tests, docs references, config validation, Ruff, and diff check.

## Completion boundary

This plan completes the immediate correctness/security slice, not the full Agent Department goal. Full completion still requires durable Hire/Fire Saga composition, employee Channel child processes, real Slash reconciliation, durable employee outbox, production Supervisor recovery, fault injection, and real test/production tenant evidence.
