# Adaptive Spec Review Roles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement adaptive Spec review roles with concurrent execution, dynamic task-specific reviewers, suggestion aggregation, and two-pass convergence.

**Architecture:** Add role data models and planning in `src/spec_engine/review_roles.py`, run roles through a new adaptive pipeline in `src/spec_engine/adaptive_review.py`, aggregate evidence-gated suggestions in `src/spec_engine/review_aggregation.py`, and register an `adaptive_roles` review strategy. Keep the existing fixed software roles as converted role specs for programming tasks and preserve `multi_perspective` as fallback.

**Tech Stack:** Python dataclasses, existing Spec Engine review artifacts, existing ephemeral ACP review sessions, pytest.

---

### Task 1: Role Models And Planner

**Files:**
- Create: `src/spec_engine/review_roles.py`
- Test: `tests/test_adaptive_review_roles.py`

- [x] Add tests for fixed programming roles, writing roles, research roles, caps, and dependency cycle fallback.
- [x] Implement `ReviewRoleSpec`, `RolePlan`, fixed-role conversion, heuristic task-kind detection, deterministic role generation, role caps, and dependency batching.
- [x] Run `uv run python -m pytest tests/test_adaptive_review_roles.py -q`.

### Task 2: Role Worker And Aggregator

**Files:**
- Create: `src/spec_engine/adaptive_review.py`
- Create: `src/spec_engine/review_aggregation.py`
- Modify: `src/engine_base.py`
- Test: `tests/test_adaptive_review_pipeline.py`

- [x] Add tests proving roles in the same batch run concurrently, dependency layers run in order, evidence-less blockers downgrade to observations, and aggregated guidance prefixes role names.
- [x] Extend `PerspectiveReview` with optional `role_id`, `role_display_name`, `role_category`, and `blocking` metadata.
- [x] Implement JSON role review prompt parsing, tolerant fallback parsing, role worker execution, aggregation, and conversion back to `ReviewResult`.
- [x] Run `uv run python -m pytest tests/test_adaptive_review_pipeline.py -q`.

### Task 3: Strategy Wiring

**Files:**
- Modify: `src/spec_engine/review_strategy.py`
- Modify: `src/spec_engine/review.py`
- Modify: `src/spec_engine/engine.py`
- Modify: `src/config/settings.py`
- Test: `tests/test_adaptive_review_strategy.py`

- [x] Add settings for adaptive strategy, role caps, dependencies, evidence gate, and pass streak.
- [x] Register `AdaptiveRoleReviewStrategy`.
- [x] Make SpecEngine select the configured review strategy instead of always calling the old orchestrator path.
- [x] Ensure adaptive strategy uses `ReviewArtifacts` and falls back to fixed roles if role planning fails.
- [x] Run `uv run python -m pytest tests/test_adaptive_review_strategy.py -q`.

### Task 4: Two-pass Convergence

**Files:**
- Modify: `src/spec_engine/models.py`
- Modify: `src/spec_engine/engine.py`
- Test: `tests/test_adaptive_review_convergence.py`

- [x] Add persisted pass streak and role/suggestion hashes to `SpecProject`.
- [x] Update successful-cycle finalization to require the configured consecutive pass streak before success.
- [x] Reset streak when a blocking role set changes or a blocking suggestion hash changes.
- [x] Run `uv run python -m pytest tests/test_adaptive_review_convergence.py -q`.

### Task 5: Compatibility And Full Validation

**Files:**
- Modify: `.Memory/2026-05-12.md`
- Modify: `.Memory/Abstract.md`

- [x] Run focused review suites.
- [x] Run `uv run python -m pytest tests/ -q`.
- [x] Run `git diff --check`.
- [x] Update `.Memory` with implementation and validation.
- [ ] Commit and push.
