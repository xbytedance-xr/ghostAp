# Employee-Scoped TraeX Home Implementation Plan

**Goal:** Restore persistent TraeX employees while keeping each employee's memory,
state, constraints, and provider login projection isolated.

**Architecture:** The manager configuration names only the source TraeX login home.
At dispatch, GhostAP validates `cli/auth.json`, copies it into
`<employee>/runtime/trae-home/cli/auth.json`, copies the employee's canonical
workspace constraints into `<employee>/runtime/trae-home/AGENTS.md`, and sets that
private directory as `TRAE_HOME`. Other backends never receive `TRAE_HOME`.

## Completed work

- [x] Reproduced the log failure and isolated it to missing TraeX login/model state
  under the employee home.
- [x] Added RED contract tests for per-employee auth and constraint projection.
- [x] Added a frozen, secret-free provider-file descriptor to employee environment
  material.
- [x] Added no-follow, owner/mode, size, JSON, atomic-write, and fsync protections.
- [x] Wired the configured source auth home through the production WebSocket runtime.
- [x] Limited projection and `TRAE_HOME` to TraeX employees; covered a non-TraeX
  backend.
- [x] Started employee ACP sessions in each employee's persistent workspace while
  preserving project-root startup for non-employee Slock sessions.
- [x] Added retirement cleanup so copied auth never enters employee archives.
- [x] Kept all work directly on `dev`.

## Remaining verification and delivery

- [x] Run the expanded Autonomous suite, Ruff, config validation, and diff checks.
- [x] Run a real no-prompt TraeX ACP probe with an employee-private `TRAE_HOME`.
- [ ] Update `.Memory`, commit and push `dev`, restart and inspect new logs.
- [ ] Delete every local and remote branch except `dev` after confirming the push.
