# Codex Model Effort Selection Implementation Plan

**Goal:** Restore dropdown-based Codex model/Effort selection and apply the selected Effort through the official ACP adapter in normal and Workflow modes.

**Architecture:** Probe Codex model capabilities from the adapter's session config options, retain one model option with attached Effort capability metadata, and let the shared cascade renderer expand those capabilities into UI variants. Persist the existing composite `model/effort` selection and split it only at the Codex ACP boundary, where model and reasoning Effort are applied as separate config options.

**Tech Stack:** Python 3.11, ACP SDK 0.11, Feishu CardKit 2.0 JSON, pytest, uv.

## Global Constraints

- Use only `uv`.
- Do not restore Codex local model-cache fallback.
- Keep Traex variant semantics unchanged.
- Keep Worktree/Slock button lists at one button per model.
- Add regression tests before production changes.
- Update `.Memory/2026-07-10.md` and `.Memory/Abstract.md`.

---

### Task 1: Codex Selection Value Contract

**Files:**
- Create: `src/acp/model_selection.py`
- Modify: `src/acp/providers/__init__.py`
- Test: `tests/test_acp_model_normalization.py`

**Interfaces:**
- Produces: `split_codex_model_selection(value: Optional[str]) -> tuple[Optional[str], Optional[str]]`
- Produces: `compose_codex_model_selection(model_id: str, effort: Optional[str]) -> str`

- [x] Write failing tests for bare, `high`, `max`, `ultra`, and provider-qualified model values.
- [x] Run `uv run python -m pytest tests/test_acp_model_normalization.py -q` and confirm the new tests fail.
- [x] Implement the parser/formatter and preserve the composite value above the official Codex ACP boundary.
- [x] Re-run the test and confirm it passes.

### Task 2: Adapter Capability Probe

**Files:**
- Modify: `src/ttadk/models.py`
- Modify: `src/acp/helper.py`
- Test: `tests/test_acp_model_probe_timeout.py`

**Interfaces:**
- `ACPModelOption.reasoning_efforts: tuple[str, ...]`
- `ACPModelOption.adapted_reasoning_effort: Optional[str]`
- Codex probing returns one option per model with exact per-model Effort metadata.

- [x] Write a fake ACP connection test where models support different Effort sets.
- [x] Verify RED: the current probe only returns base models and discards `thought_level`.
- [x] Implement config-option lookup, per-model switching, and exact Effort extraction.
- [x] Verify model probe caching still copies the new metadata safely.
- [x] Re-run the probe test file.

### Task 3: Shared Cascade and Workflow Metadata

**Files:**
- Modify: `src/card/render/model_cascade.py`
- Modify: `src/card/builders/system.py`
- Modify: `src/worktree_engine/tool_discovery.py`
- Modify: `src/feishu/handlers/workflow.py`
- Test: `tests/test_model_cascade.py`
- Test: `tests/test_workflow_selection_controller.py`

**Interfaces:**
- `build_model_groups()` expands `reasoning_efforts` into internal `standard/<effort>` variants.
- Existing suffix inference remains the fallback for Traex.

- [x] Write failing tests showing Codex `max` is Effort, `ultra` is ordered, and normal/Workflow cards expose the Effort selector.
- [x] Verify RED against the current renderer.
- [x] Implement capability expansion and metadata passthrough.
- [x] Verify Worktree/Slock-facing discovery still returns one dictionary per model.
- [x] Re-run cascade and Workflow selection tests.

### Task 4: Apply Model and Effort

**Files:**
- Modify: `src/acp/session.py`
- Modify: `src/acp/sync_adapter.py`
- Test: `tests/test_switch_model.py`
- Test: `tests/test_acp_sync_adapter.py`
- Test: `tests/test_acp_model_normalization.py`

**Interfaces:**
- `ACPSession.set_config_option(config_id: str, value: str) -> bool`
- `SyncACPSession.set_model("model/effort") -> bool` applies both config options for Codex.

- [x] Write failing startup and online-switch tests for model then Effort ordering.
- [x] Write failing tests for model rejection and Effort rejection.
- [x] Verify RED.
- [x] Generalize the low-level config-option RPC and implement Codex composite application.
- [x] Verify GREEN and preserve legacy model switching for non-Codex providers.

### Task 5: Verification, Review, and Delivery

**Files:**
- Modify: `.Memory/2026-07-10.md`
- Modify: `.Memory/Abstract.md`
- Keep: `docs/2026-07-10-codex-effort-selection-design.md`
- Keep: `docs/2026-07-10-codex-effort-selection-plan.md`

- [x] Run targeted ACP/cascade/Workflow tests.
- [x] Run `uv run python -m pytest tests/test_acp*.py -q`.
- [x] Run `uv run python -m pytest tests/test_workflow_*.py -q`.
- [x] Run `uv run ruff check` on touched Python files.
- [x] Run `uv run python -m src.main --validate`.
- [x] Run `uv run python -m pytest tests/test_docs_references.py -q`.
- [x] Run `git diff --check`.
- [x] Perform stateless product/architecture/engineering/QA/UX review rounds until two consecutive rounds have no material suggestions.
- [x] Update project Memory with root cause, design, verification, and residual risks.
- [ ] Commit with the repository commit-message convention and push `dev` to `origin/dev`.
