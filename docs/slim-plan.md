# GhostAP Slim Plan

Date: 2026-05-16

## Baseline

- Branch: `harness`
- Dirty changes before slimming: none
- Rollback tag: `pre-slim-baseline`
- Protected history: `.Memory/` is not a cleanup target

Commands:

- `uv run python -m pytest tests/ -q`
  - Baseline result: `6575 passed, 1 failed`
  - Failure: `tests/test_validate_mode.py::TestValidateNoTombstoneTrigger::test_validate_does_not_trigger_tombstone`
  - Root cause: nested `uv run python -m src.main --validate` exceeded its 30s subprocess timeout under full-suite load
  - Single-test confirmation: same test passed in `26.57s`
- `uv run python -m pytest --cov=src --cov-report=term-missing tests/`
  - Baseline result: `6575 passed, 1 failed`
  - Coverage: `78.42%`, above the configured `65%` threshold
  - Same nested-uv validate timeout failed
- `uv run ruff check src tests`
  - Baseline result: failed with `1722` existing lint findings
  - Pyflakes-only view: `637` findings
  - This is recorded as pre-existing lint debt, not a deletion signal by itself

## Cleanup Scope

- Remove stale one-off plan/spec noise from non-runtime documentation paths.
- Keep `.Memory/` intact.
- Clear Backlog B019 by exposing `CARD_DELIVERY_API_TIMEOUT` in `.env.example`, README, and `--validate`.
- Treat shim cleanup as evidence-driven:
  - Delete only production-zero shims.
  - Migrate live production callers before deleting old module names.
  - Keep runtime/public compatibility entries when deletion would break callers.

## Rollback

Use the local tag if the slim pass needs to be reverted:

```bash
git diff pre-slim-baseline
```

Do not reset automatically; inspect the diff and revert only the intended slim changes.
